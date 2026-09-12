"""Fill `Food.embedding` / `CompositeFood.embedding` (PRD §7.2, §7.6, Chunk 9a).

Nothing on the request path reads these columns yet - Chunk 9b's hybrid
resolution does, gated by `SEMANTIC_RESOLUTION_ENABLED`. Running this early
is still the right order: the vectors have to exist before the flag can be
turned on, and this is the slow, billable part.

    python manage.py backfill_food_embeddings --dry-run
    python manage.py backfill_food_embeddings

Resumable by construction: each batch is selected by "has no vector, or has
one from a different model", so an interrupted run just carries on, and
changing `EMBEDDING_MODEL` re-embeds only what is now stale rather than
truncating the column. Re-running a finished backfill is a no-op.

**Open Food Facts is excluded by default**, but not for the reason you might
assume. Measured against the live catalog (2026-09-12): 2,070,267 OFF rows vs
14,615 generic (USDA + INDB). The *token* cost of embedding all of OFF is
around $0.60 - not the obstacle. The obstacles are wall time (~8,000 provider
round trips) and storage: 2.07M x 1536 float4 is ~12.7 GB of vector data
before the HNSW index adds its own graph on top. Plus it is the least valuable
source to embed - `_SOURCE_PRIORITY` already ranks it last precisely so plain
text lands on generic data. Pass `--source open_food_facts` to include it,
knowingly, and check the database's storage headroom first.

The default run is cheap: ~14.6k rows is roughly 175k tokens, well under a
cent at text-embedding-3-small's list price. `--dry-run` reports the row count
and an estimate without calling the provider or writing anything.
"""
from __future__ import annotations

import time
from typing import Dict, List, Sequence, Tuple

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from llm import LLMCallError, LLMConfigurationError, embed_batch
from meals import repository
from meals.embeddings import composite_embedding_text, food_embedding_text

_SOURCE_CHOICES = ["usda", "indb", "caloryx_curated", "open_food_facts"]
# §8's own ranking, minus OFF - see the module docstring.
_DEFAULT_SOURCES = ["caloryx_curated", "indb", "usda"]

# Rough tokens-per-row for the dry-run estimate only. Catalog names are short
# ("Milk, whole, 3.25% milkfat"); this is deliberately generous so the
# estimate errs high rather than under-quoting a bill.
_ESTIMATED_TOKENS_PER_ROW = 12


class Command(BaseCommand):
    help = "Embed Food / CompositeFood rows for semantic resolution (§7.2, §7.6)."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--source",
            nargs="+",
            choices=_SOURCE_CHOICES,
            default=_DEFAULT_SOURCES,
            help="Food sources to embed (default: everything except open_food_facts).",
        )
        parser.add_argument(
            "--target",
            choices=["all", "foods", "composites"],
            default="all",
            help="Which catalog to backfill (default: all).",
        )
        parser.add_argument(
            "--model",
            default="",
            help="Override settings.EMBEDDING_MODEL for this run.",
        )
        parser.add_argument("--limit", type=int, default=0, help="Stop after N rows (0 = all).")
        parser.add_argument(
            "--batch-size",
            type=int,
            default=0,
            help="Rows per provider call (0 = settings.EMBEDDING_MAX_BATCH).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be embedded; call nothing, write nothing.",
        )

    def handle(self, *args, **options) -> None:
        model = options["model"] or settings.EMBEDDING_MODEL
        batch_size = options["batch_size"] or settings.EMBEDDING_MAX_BATCH
        dry_run = options["dry_run"]
        limit = options["limit"]
        sources = [source.upper() for source in options["source"]]

        if batch_size > settings.EMBEDDING_MAX_BATCH:
            raise CommandError(
                "--batch-size {} exceeds EMBEDDING_MAX_BATCH={}".format(
                    batch_size, settings.EMBEDDING_MAX_BATCH
                )
            )

        self._preflight(model, dry_run)

        total_written = 0
        total_tokens = 0
        for target in ("foods", "composites"):
            if options["target"] not in ("all", target):
                continue
            written, tokens = self._backfill(
                target, model, sources, batch_size, limit, dry_run
            )
            total_written += written
            total_tokens += tokens

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run - nothing was written."))
            return

        cost_micros = int(round(total_tokens * settings.EMBEDDING_INPUT_COST_PER_1M_MICROS / 1_000_000))
        self.stdout.write(
            self.style.SUCCESS(
                "Embedded {} rows with {} ({} tokens, ~${:.2f}).".format(
                    total_written, model, total_tokens, cost_micros / 1_000_000
                )
            )
        )
        if total_written:
            self.stdout.write(
                "Next: `python manage.py ensure_vector_index` if the ANN indexes "
                "aren't built yet (or a `prisma db push` dropped them)."
            )

    # -- preflight ----------------------------------------------------------

    def _preflight(self, model: str, dry_run: bool) -> None:
        """Every way this can be misconfigured, checked before the first paid
        call rather than discovered a few thousand embeddings in."""
        if not model:
            raise CommandError(
                "EMBEDDING_MODEL is unset - set it in .env, or pass --model."
            )

        if not repository.vector_extension_installed():
            raise CommandError(
                "The pgvector extension is not installed on this database. It is declared "
                "in prisma/schema.prisma's datasource, so `prisma db push --schema "
                "prisma/schema.prisma` will create it."
            )

        for table in ("Food", "CompositeFood"):
            width = repository.embedding_column_width(table)
            if width is None:
                raise CommandError(
                    '"{}".embedding does not exist - run `prisma db push --schema '
                    "prisma/schema.prisma` first.".format(table)
                )
            if width != settings.EMBEDDING_DIMENSIONS:
                raise CommandError(
                    '"{}".embedding is vector({}) but EMBEDDING_DIMENSIONS={}. Postgres '
                    "rejects any other width, so fix one of the two before running: the "
                    "column width is declared in prisma/schema.prisma.".format(
                        table, width, settings.EMBEDDING_DIMENSIONS
                    )
                )

        if not dry_run and not settings.OPENAI_API_KEY:
            raise CommandError("OPENAI_API_KEY must be set to call the embeddings API.")

    # -- the backfill itself ------------------------------------------------

    def _backfill(
        self,
        target: str,
        model: str,
        sources: Sequence[str],
        batch_size: int,
        limit: int,
        dry_run: bool,
    ) -> Tuple[int, int]:
        pending = (
            repository.count_foods_needing_embedding(model, list(sources))
            if target == "foods"
            else repository.count_composites_needing_embedding(model)
        )
        planned = min(pending, limit) if limit else pending
        label = "foods ({})".format(", ".join(sources)) if target == "foods" else "composites"

        if not planned:
            self.stdout.write("No {} need embedding - nothing to do.".format(target))
            return 0, 0

        self.stdout.write("{}: {} row(s) to embed with {}.".format(label, planned, model))
        if dry_run:
            self.stdout.write(
                "  ~{} tokens, ~${:.2f} at EMBEDDING_INPUT_COST_PER_1M_MICROS={}.".format(
                    planned * _ESTIMATED_TOKENS_PER_ROW,
                    planned
                    * _ESTIMATED_TOKENS_PER_ROW
                    * settings.EMBEDDING_INPUT_COST_PER_1M_MICROS
                    / 1_000_000
                    / 1_000_000,
                    settings.EMBEDDING_INPUT_COST_PER_1M_MICROS,
                )
            )
            return 0, 0

        written = 0
        tokens = 0
        started = time.monotonic()
        while written < planned:
            take = min(batch_size, planned - written)
            rows = (
                repository.fetch_foods_needing_embedding(model, list(sources), limit=take)
                if target == "foods"
                else repository.fetch_composites_needing_embedding(model, limit=take)
            )
            if not rows:
                # The predicate found nothing despite the count above - another
                # run took these rows, or the catalog changed underneath us.
                # Stopping is correct; re-running picks up whatever is left.
                break

            texts = [self._text_for(target, row) for row in rows]
            try:
                response = embed_batch(texts)
            except LLMConfigurationError as exc:
                raise CommandError(str(exc)) from exc
            except LLMCallError as exc:
                # Everything written before this point stays written and the
                # predicate excludes it, so the fix is to re-run - no partial
                # state to clean up, and no reason to burn the rest of the
                # catalog against what may be a provider-wide outage.
                raise CommandError(
                    "Embedding call failed after {} row(s): {}. Re-run to resume.".format(
                        written, exc
                    )
                ) from exc

            updates: List[Tuple[str, List[float]]] = [
                (row["id"], vector) for row, vector in zip(rows, response.vectors)
            ]
            if target == "foods":
                repository.set_food_embeddings(updates, model)
            else:
                repository.set_composite_embeddings(updates, model)

            written += len(updates)
            tokens += response.prompt_tokens
            self.stdout.write("  {}/{} ({}s elapsed)".format(written, planned, int(time.monotonic() - started)))

        return written, tokens

    def _text_for(self, target: str, row: Dict) -> str:
        if target == "foods":
            return food_embedding_text(row["name"], row.get("brand"))
        return composite_embedding_text(row["name"], row.get("aliases") or [])
