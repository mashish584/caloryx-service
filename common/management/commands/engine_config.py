"""Inspect and retune the engine constants without shipping an app release.

PRD §10 requires activity multipliers, goal adjustments, macro ratios and safety
floors to be server-side configuration. This command manages the single active
`EngineConfig` row that backs them.

    python manage.py engine_config --show
    python manage.py engine_config --seed --name v1 --activate
    python manage.py engine_config --name v1 --set loseAdjustmentKcal=-350 --activate
"""
from __future__ import annotations

import json
from typing import Any, Dict

from django.core.management.base import BaseCommand, CommandError

from common.db import get_client
from engine.config import COLUMN_TO_FIELD, DEFAULT_CONFIG, config_from_row
from onboarding.repository import invalidate_engine_config_cache
from onboarding.serializers import validation_bounds

_INT_COLUMNS = {
    "loseAdjustmentKcal",
    "gainAdjustmentKcal",
    "floorMaleKcal",
    "floorFemaleKcal",
    "floorUnspecifiedKcal",
    "targetRoundingKcal",
}
_EDITABLE = {c for c in COLUMN_TO_FIELD if c not in {"id", "name"}}


def _coerce(column: str, raw: str) -> Any:
    if column in _INT_COLUMNS:
        return int(raw)
    return float(raw)


class Command(BaseCommand):
    help = "Show or update the server-side engine configuration."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--name", default="default", help="Config row name.")
        parser.add_argument(
            "--show", action="store_true", help="Print the active configuration."
        )
        parser.add_argument(
            "--seed",
            action="store_true",
            help="Create the row from the compiled defaults if it does not exist.",
        )
        parser.add_argument(
            "--set",
            action="append",
            default=[],
            metavar="COLUMN=VALUE",
            help="Override a constant, e.g. --set loseAdjustmentKcal=-350",
        )
        parser.add_argument(
            "--activate",
            action="store_true",
            help="Make this row the active one (deactivates every other row).",
        )

    def handle(self, *args, **options) -> None:
        client = get_client()
        name = options["name"]

        if options["show"] and not (options["seed"] or options["set"] or options["activate"]):
            row = client.engineconfig.find_first(where={"isActive": True})
            config = config_from_row(row) if row else DEFAULT_CONFIG
            source = "database row '{}'".format(row.name) if row else "compiled defaults"
            self.stdout.write("source: {}".format(source))
            self.stdout.write(
                json.dumps(config.to_public_dict(validation_bounds()), indent=2)
            )
            return

        updates: Dict[str, Any] = {}
        for assignment in options["set"]:
            if "=" not in assignment:
                raise CommandError("--set expects COLUMN=VALUE, got {!r}".format(assignment))
            column, raw = assignment.split("=", 1)
            column = column.strip()
            if column not in _EDITABLE:
                raise CommandError(
                    "unknown column {!r}. Known: {}".format(
                        column, ", ".join(sorted(_EDITABLE))
                    )
                )
            updates[column] = _coerce(column, raw.strip())

        existing = client.engineconfig.find_unique(where={"name": name})
        if existing is None:
            if not options["seed"]:
                raise CommandError(
                    "no config named {!r}. Pass --seed to create it.".format(name)
                )
            row = client.engineconfig.create(data=dict(updates, name=name))
            self.stdout.write(self.style.SUCCESS("created config {!r}".format(name)))
        elif updates:
            row = client.engineconfig.update(where={"name": name}, data=updates)
            self.stdout.write(self.style.SUCCESS("updated config {!r}".format(name)))
        else:
            row = existing

        if options["activate"]:
            # Exactly one row may be active; the read path takes find_first.
            client.engineconfig.update_many(
                where={"id": {"not": row.id}}, data={"isActive": False}
            )
            row = client.engineconfig.update(where={"id": row.id}, data={"isActive": True})
            self.stdout.write(self.style.SUCCESS("activated config {!r}".format(name)))

        invalidate_engine_config_cache()
        self.stdout.write(
            json.dumps(
                config_from_row(row).to_public_dict(validation_bounds()), indent=2
            )
        )
