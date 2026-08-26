"""Fill in `Plan.safetyFloorKcal` / `Plan.requestedAdjustmentKcal` for rows
written before those columns existed.

Both are nullable purely so the schema change can land on a populated database.
A resumed plan needs the floor to render its §6.2 clamp message, so run this once
after `prisma db push` and the nulls are gone for good; every write from
`upsert_plan` onwards sets them.

    python manage.py backfill_plan_rationale --dry-run
    python manage.py backfill_plan_rationale
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from common.db import get_client
from engine.config import DEFAULT_CONFIG, config_from_row
from onboarding.services import derive_stored_rationale

_INCLUDE = {"profile": True, "engineConfig": True}


class Command(BaseCommand):
    help = "Backfill safetyFloorKcal / requestedAdjustmentKcal on existing plans."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing.",
        )

    def handle(self, *args, **options) -> None:
        client = get_client()
        rows = client.plan.find_many(
            where={
                "OR": [
                    {"safetyFloorKcal": None},
                    {"requestedAdjustmentKcal": None},
                ]
            },
            include=_INCLUDE,
        )

        if not rows:
            self.stdout.write(self.style.SUCCESS("nothing to backfill"))
            return

        dry_run = options["dry_run"]
        updated = skipped = 0

        for plan in rows:
            profile = getattr(plan, "profile", None)
            if profile is None:
                # Plan rows cascade-delete with their profile, so this means the
                # include failed rather than an orphan. Leave it for a human.
                self.stderr.write("plan {}: no profile, skipped".format(plan.id))
                skipped += 1
                continue

            # Prefer the config that actually produced the numbers; a plan built
            # on the compiled defaults has no engineConfigId to point at.
            row = getattr(plan, "engineConfig", None)
            config = config_from_row(row) if row else DEFAULT_CONFIG
            floor, requested = derive_stored_rationale(profile, config)

            data = {}
            if plan.safetyFloorKcal is None:
                data["safetyFloorKcal"] = floor
            if plan.requestedAdjustmentKcal is None:
                data["requestedAdjustmentKcal"] = requested

            self.stdout.write(
                "plan {} (clamped={}) <- {} [{}]".format(
                    plan.id, plan.clamped, data, config.name
                )
            )
            if not dry_run:
                client.plan.update(where={"id": plan.id}, data=data)
            updated += 1

        verb = "would update" if dry_run else "updated"
        self.stdout.write(
            self.style.SUCCESS(
                "{} {} plan(s), {} skipped".format(verb, updated, skipped)
            )
        )
