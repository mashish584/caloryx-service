"""Map the retired `Profile.preferredUnits` onto `weightUnit` + `heightUnit`.

A single METRIC/IMPERIAL flag cannot express kg + ft/in, so it was replaced by
one unit per measurement type (§5.2). The new columns carry defaults, which means
`prisma db push` silently lands every existing IMPERIAL user on KG/CM - run this
straight after the push, before dropping the old column.

    python manage.py backfill_unit_preferences --dry-run
    python manage.py backfill_unit_preferences
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from common.db import get_client
from engine.enums import HeightUnit, UnitSystem, WeightUnit

# Total over UnitSystem; `tests/test_units.py` asserts that stays true.
UNIT_SYSTEM_TO_PAIR = {
    UnitSystem.METRIC.value: (WeightUnit.KG.value, HeightUnit.CM.value),
    UnitSystem.IMPERIAL.value: (WeightUnit.LB.value, HeightUnit.FT_IN.value),
}


class Command(BaseCommand):
    help = "Backfill weightUnit / heightUnit from the legacy preferredUnits column."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing.",
        )

    def handle(self, *args, **options) -> None:
        client = get_client()
        # Only IMPERIAL rows can be wrong: METRIC maps onto the column defaults.
        rows = client.profile.find_many(
            where={"preferredUnits": UnitSystem.IMPERIAL.value}
        )

        if not rows:
            self.stdout.write(self.style.SUCCESS("nothing to backfill"))
            return

        dry_run = options["dry_run"]
        updated = skipped = 0

        for profile in rows:
            pair = UNIT_SYSTEM_TO_PAIR.get(profile.preferredUnits)
            if pair is None:
                self.stderr.write(
                    "profile {}: unknown preferredUnits {!r}, skipped".format(
                        profile.id, profile.preferredUnits
                    )
                )
                skipped += 1
                continue

            weight_unit, height_unit = pair
            if (profile.weightUnit, profile.heightUnit) == (weight_unit, height_unit):
                continue

            self.stdout.write(
                "profile {} {} -> weight={} height={}".format(
                    profile.id, profile.preferredUnits, weight_unit, height_unit
                )
            )
            if not dry_run:
                client.profile.update(
                    where={"id": profile.id},
                    data={"weightUnit": weight_unit, "heightUnit": height_unit},
                )
            updated += 1

        verb = "would update" if dry_run else "updated"
        self.stdout.write(
            self.style.SUCCESS(
                "{} {} profile(s), {} skipped".format(verb, updated, skipped)
            )
        )
