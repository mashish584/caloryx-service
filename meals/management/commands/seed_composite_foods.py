"""Seed a small dev/test composite-food catalog (§7.6).

Not curated production dishes - just enough to develop and test deterministic
dish expansion against, built from the foods `seed_foods` creates. Run that
command first.

    python manage.py seed_composite_foods
    python manage.py seed_composite_foods --reset   # delete and re-seed
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from common.db import get_client

# `ratioOfServing` values are as-served fractions of `servingGrams` and sum to
# ~1.0 per dish - a curation-time invariant (§9), not runtime-validated.
COMPOSITES = [
    {
        "name": "Chicken Biryani",
        "aliases": ["biryani"],
        "servingGrams": 350.0,
        "servingLabel": "1 plate",
        "isCurated": True,
        "components": [
            {"foodName": "Cooked White Rice", "ratioOfServing": 0.65, "state": "COOKED"},
            {"foodName": "Grilled Chicken Breast", "ratioOfServing": 0.30, "state": "COOKED"},
            {"foodName": "Olive Oil", "ratioOfServing": 0.05, "state": "UNSPECIFIED"},
        ],
    },
    {
        "name": "Dal Chawal",
        "aliases": ["dal rice"],
        "servingGrams": 300.0,
        "servingLabel": "1 plate",
        "isCurated": True,
        "components": [
            {"foodName": "Toor Dal, Cooked", "ratioOfServing": 0.5, "state": "COOKED"},
            {"foodName": "Cooked White Rice", "ratioOfServing": 0.5, "state": "COOKED"},
        ],
    },
    {
        "name": "Roti Sabzi",
        "aliases": [],
        "servingGrams": 180.0,
        "servingLabel": "1 plate",
        "isCurated": True,
        "components": [
            {"foodName": "Whole Wheat Roti", "ratioOfServing": 0.4444, "state": "COOKED"},
            {"foodName": "Spinach, Raw", "ratioOfServing": 0.5556, "state": "RAW"},
        ],
    },
]


class Command(BaseCommand):
    help = "Seed (idempotently) a small dev/test composite-food catalog."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete every existing CompositeFood row before seeding.",
        )

    def handle(self, *args, **options) -> None:
        client = get_client()

        if options["reset"]:
            client.compositefoodcomponent.delete_many()
            client.compositefood.delete_many()
            self.stdout.write("cleared existing composite foods")

        for source_entry in COMPOSITES:
            entry = dict(source_entry)
            existing = client.compositefood.find_first(where={"name": entry["name"]})
            if existing is not None:
                self.stdout.write("skip (exists): {}".format(entry["name"]))
                continue

            components = entry.pop("components")
            composite = client.compositefood.create(data=entry)
            for component in components:
                component = dict(component)
                food_name = component.pop("foodName")
                food = client.food.find_first(where={"name": food_name})
                if food is None:
                    raise CommandError(
                        "food {!r} not found - run `manage.py seed_foods` first".format(food_name)
                    )
                client.compositefoodcomponent.create(
                    data=dict(
                        component,
                        composite={"connect": {"id": composite.id}},
                        food={"connect": {"id": food.id}},
                    )
                )
            self.stdout.write(self.style.SUCCESS("created: {}".format(entry["name"])))
