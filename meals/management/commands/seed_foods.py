"""Seed a small dev/test food catalog.

Not the production USDA/Open Food Facts ingestion pipeline (that's later,
higher-volume data-ops work) - just enough curated entries to develop and test
against, covering every unit type and the raw/cooked yield path.

    python manage.py seed_foods
    python manage.py seed_foods --reset   # delete and re-seed
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from common.db import get_client

FOODS = [
    {
        "name": "Cooked White Rice",
        "source": "CALORYX_CURATED",
        "defaultState": "COOKED",
        "rawToCookedYield": 3.0,
        "caloriesKcalPer100g": 130.0,
        "proteinGPer100g": 2.7,
        "carbsGPer100g": 28.2,
        "fatGPer100g": 0.3,
        "fiberGPer100g": 0.4,
        "servingUnits": [
            {"unit": "katori", "grams": 150.0, "type": "HOUSEHOLD"},
            {"unit": "cup", "grams": 158.0, "type": "HOUSEHOLD"},
        ],
    },
    {
        "name": "Grilled Chicken Breast",
        "source": "CALORYX_CURATED",
        "defaultState": "COOKED",
        "rawToCookedYield": 0.75,
        "caloriesKcalPer100g": 165.0,
        "proteinGPer100g": 31.0,
        "carbsGPer100g": 0.0,
        "fatGPer100g": 3.6,
        "fiberGPer100g": 0.0,
        "servingUnits": [],
    },
    {
        "name": "Whole Wheat Roti",
        "source": "CALORYX_CURATED",
        "defaultState": "COOKED",
        "rawToCookedYield": None,
        "caloriesKcalPer100g": 297.0,
        "proteinGPer100g": 11.0,
        "carbsGPer100g": 56.0,
        "fatGPer100g": 4.6,
        "fiberGPer100g": 8.0,
        "servingUnits": [{"unit": "piece", "grams": 40.0, "type": "COUNTABLE"}],
    },
    {
        "name": "Toor Dal, Cooked",
        "source": "CALORYX_CURATED",
        "defaultState": "COOKED",
        "rawToCookedYield": None,
        "caloriesKcalPer100g": 116.0,
        "proteinGPer100g": 7.0,
        "carbsGPer100g": 20.0,
        "fatGPer100g": 0.4,
        "fiberGPer100g": 5.0,
        "servingUnits": [{"unit": "katori", "grams": 150.0, "type": "HOUSEHOLD"}],
    },
    {
        "name": "Boiled Egg",
        "source": "CALORYX_CURATED",
        "defaultState": "COOKED",
        "rawToCookedYield": None,
        "caloriesKcalPer100g": 155.0,
        "proteinGPer100g": 13.0,
        "carbsGPer100g": 1.1,
        "fatGPer100g": 11.0,
        "fiberGPer100g": 0.0,
        "servingUnits": [{"unit": "piece", "grams": 50.0, "type": "COUNTABLE"}],
    },
    {
        "name": "Banana",
        "source": "USDA",
        "defaultState": "UNSPECIFIED",
        "rawToCookedYield": None,
        "caloriesKcalPer100g": 89.0,
        "proteinGPer100g": 1.1,
        "carbsGPer100g": 22.8,
        "fatGPer100g": 0.3,
        "fiberGPer100g": 2.6,
        "servingUnits": [{"unit": "piece", "grams": 118.0, "type": "COUNTABLE"}],
    },
    {
        "name": "Olive Oil",
        "source": "USDA",
        "defaultState": "UNSPECIFIED",
        "rawToCookedYield": None,
        "caloriesKcalPer100g": 884.0,
        "proteinGPer100g": 0.0,
        "carbsGPer100g": 0.0,
        "fatGPer100g": 100.0,
        "fiberGPer100g": 0.0,
        "servingUnits": [
            {"unit": "tbsp", "grams": 13.5, "type": "HOUSEHOLD"},
            {"unit": "tsp", "grams": 4.5, "type": "HOUSEHOLD"},
        ],
    },
    {
        "name": "Spinach, Raw",
        "source": "USDA",
        "defaultState": "RAW",
        "rawToCookedYield": None,
        "caloriesKcalPer100g": 23.0,
        "proteinGPer100g": 2.9,
        "carbsGPer100g": 3.6,
        "fatGPer100g": 0.4,
        "fiberGPer100g": 2.2,
        "servingUnits": [{"unit": "cup", "grams": 30.0, "type": "HOUSEHOLD"}],
    },
]


class Command(BaseCommand):
    help = "Seed (idempotently) a small dev/test food catalog."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete every existing Food row before seeding.",
        )

    def handle(self, *args, **options) -> None:
        client = get_client()

        if options["reset"]:
            client.foodservingunit.delete_many()
            client.food.delete_many()
            self.stdout.write("cleared existing foods")

        for source_entry in FOODS:
            entry = dict(source_entry)  # seeding is idempotent per-run; don't mutate FOODS
            existing = client.food.find_first(where={"name": entry["name"]})
            if existing is not None:
                self.stdout.write("skip (exists): {}".format(entry["name"]))
                continue

            serving_units = entry.pop("servingUnits")
            food = client.food.create(data=entry)
            for serving_unit in serving_units:
                client.foodservingunit.create(
                    data=dict(serving_unit, food={"connect": {"id": food.id}})
                )
            self.stdout.write(self.style.SUCCESS("created: {}".format(entry["name"])))
