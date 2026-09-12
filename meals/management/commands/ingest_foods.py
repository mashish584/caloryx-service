"""Bulk-import a nutrition database into the local `Food` catalog (PRD §8).

All three sources are read from local bulk-download files - nothing here (or
anywhere on the logging path) calls an external API.

USDA FoodData Central (CC0) - download the CSV zips for "SR Legacy",
"Foundation Foods" and "FNDDS" (Survey) from
https://fdc.nal.usda.gov/download-datasets, unzip, and run once per dir:

    python manage.py ingest_foods --source usda --path ~/data/FoodData_Central_sr_legacy_food_csv_2018-04

INDB (Indian Nutrient Databank) - save INDB.xlsx's sheet as CSV:

    python manage.py ingest_foods --source indb --path ~/data/INDB.csv

Open Food Facts (ODbL) - the full export, gzipped. Every country by default
(millions of products - start with --limit, and consider --min-scans to keep
only products people actually scan); --countries narrows it:

    python manage.py ingest_foods --source off --path ~/data/en.openfoodfacts.org.products.csv.gz \\
        --min-scans 5 [--countries en:india en:united-states]

Add --dry-run to parse and validate without writing. Re-running is safe:
existing `(source, sourceRef)` rows are skipped, or refreshed in place with
--update-existing. Attribution for USDA/OFF/INDB belongs in the nutrition-
source sheet (§5.2.1).
"""
from __future__ import annotations

from collections import Counter
from typing import Iterator, List

from django.core.management.base import BaseCommand, CommandError

from meals import repository
from meals.ingest import FoodRecord, validate
from meals.ingest.indb import parse_indb
from meals.ingest.off import parse_off
from meals.ingest.usda import parse_usda

_BATCH_SIZE = 500


class Command(BaseCommand):
    help = "Import USDA FDC / INDB / Open Food Facts bulk data into the Food catalog."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--source", required=True, choices=["usda", "indb", "off"])
        parser.add_argument("--path", required=True, help="Unzipped FDC dir, INDB CSV, or OFF .csv(.gz)")
        parser.add_argument(
            "--countries",
            nargs="+",
            default=[],
            help="OFF only: keep products sold in these countries_tags, e.g. en:india (default: all countries).",
        )
        parser.add_argument("--min-scans", type=int, default=0, help="OFF only: minimum unique scans.")
        parser.add_argument("--limit", type=int, default=0, help="Stop after N valid records (0 = all).")
        parser.add_argument("--dry-run", action="store_true", help="Parse and validate only; write nothing.")
        parser.add_argument(
            "--update-existing",
            action="store_true",
            help="Upsert row-by-row, refreshing foods that already exist (slower).",
        )

    def handle(self, *args, **options) -> None:
        records = self._parse(options)
        # Only crowdsourced data gets the energy-consistency check - see validate().
        check_energy = options["source"] == "off"

        skipped: Counter = Counter()
        valid = written = 0
        batch: List[FoodRecord] = []
        try:
            for record in records:
                reason = validate(record, check_energy=check_energy)
                if reason is not None:
                    skipped[reason] += 1
                    continue
                valid += 1
                batch.append(record)
                if len(batch) >= _BATCH_SIZE:
                    written += self._write(batch, options)
                    batch = []
                if options["limit"] and valid >= options["limit"]:
                    break
            written += self._write(batch, options)
        except FileNotFoundError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write("valid: {}".format(valid))
        for reason, count in sorted(skipped.items()):
            self.stdout.write("skipped ({}): {}".format(reason, count))
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("dry run - nothing written"))
            return
        self.stdout.write(self.style.SUCCESS("written: {}".format(written)))
        if written:
            version = repository.bump_catalog_version()
            self.stdout.write("catalog version is now {}".format(version))

    def _parse(self, options) -> Iterator[FoodRecord]:
        source, path = options["source"], options["path"]
        if source == "usda":
            return parse_usda(path)
        if source == "indb":
            return parse_indb(path)
        return parse_off(path, countries=options["countries"], min_scans=options["min_scans"])

    def _write(self, batch: List[FoodRecord], options) -> int:
        if not batch or options["dry_run"]:
            return 0
        if options["update_existing"]:
            for record in batch:
                repository.upsert_food(record)
            return len(batch)
        return repository.bulk_insert_foods(batch)
