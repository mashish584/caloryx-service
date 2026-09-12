"""Create the pgvector ANN indexes (PRD §7.2, Chunk 9a).

Separate from `prisma db push` because Prisma has no Hnsw index type to
declare - its `type:` accepts Hash/Gist/Gin/SpGist/Brin only - so unlike
`Food_name_trgm_idx` these cannot live in schema.prisma. The consequence is
that a later `prisma db push` may see them as drift and drop them; re-running
this is the fix, and it is idempotent (`CREATE INDEX IF NOT EXISTS`).

    python manage.py ensure_vector_index

Safe to run before any embeddings exist - HNSW needs no training pass over
existing rows, which is why it was chosen over IVFFlat for a column that
Chunk 9a leaves mostly NULL. On a large, already-embedded catalog the build
is not instant; run it once after the backfill rather than between batches.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from meals import repository


class Command(BaseCommand):
    help = "Create the HNSW indexes on Food.embedding / CompositeFood.embedding if absent."

    def handle(self, *args, **options) -> None:
        if not repository.vector_extension_installed():
            raise CommandError(
                "The pgvector extension is not installed on this database. It is declared "
                "in prisma/schema.prisma's datasource, so `prisma db push --schema "
                "prisma/schema.prisma` will create it."
            )
        existing = repository.ensure_vector_indexes()
        self.stdout.write(self.style.SUCCESS("Vector indexes present: {}".format(", ".join(sorted(existing)))))
