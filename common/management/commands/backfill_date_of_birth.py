"""Reconstruct `Profile.dateOfBirth` for rows that predate the column.

Age used to be stored as an integer, frozen at whatever the user typed. It is now
derived from a real birth date on every read (§9), but existing rows only ever
supplied the integer - so their date has to be reconstructed from it.

These reconstructions are approximations: an age of 30 could mean any day in a
365-day window. That is a one-off migration artifact for old rows, not the
ongoing design; every profile written from now on carries a date the user
actually picked. Run this after `prisma db push` and before dropping `age`.

    python manage.py backfill_date_of_birth --dry-run
    python manage.py backfill_date_of_birth
"""
from __future__ import annotations

from datetime import date

from django.core.management.base import BaseCommand

from common.db import get_client


def dob_from_age(age: int, on: date) -> date:
    """The birth date implied by `age` as declared on `on`."""
    try:
        return on.replace(year=on.year - age)
    except ValueError:
        # 29 February declared in a year whose target is not a leap year.
        return on.replace(year=on.year - age, day=28)


class Command(BaseCommand):
    help = "Backfill dateOfBirth from the legacy age column."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing.",
        )

    def handle(self, *args, **options) -> None:
        client = get_client()
        rows = client.profile.find_many(where={"dateOfBirth": None})

        if not rows:
            self.stdout.write(self.style.SUCCESS("nothing to backfill"))
            return

        dry_run = options["dry_run"]
        updated = skipped = 0

        for profile in rows:
            age = getattr(profile, "age", None)
            if age is None:
                self.stderr.write("profile {}: no age, skipped".format(profile.id))
                skipped += 1
                continue

            # `updatedAt`, not `createdAt`: the whole profile is re-sent on every
            # upsert, so this is when that age was last declared.
            declared_on = profile.updatedAt.date()
            dob = dob_from_age(age, declared_on)

            self.stdout.write(
                "profile {} age {} on {} -> {}".format(
                    profile.id, age, declared_on, dob
                )
            )
            if not dry_run:
                client.profile.update(
                    where={"id": profile.id}, data={"dateOfBirth": dob}
                )
            updated += 1

        verb = "would reconstruct" if dry_run else "reconstructed"
        self.stdout.write(
            self.style.SUCCESS(
                "{} {} approximate birth date(s), {} skipped".format(
                    verb, updated, skipped
                )
            )
        )
        self.stdout.write(
            "these are derived from a reported age and are accurate to within a year"
        )
