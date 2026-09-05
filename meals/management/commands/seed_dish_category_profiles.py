"""Seed the curated `DishCategoryProfile` starter set (§7.6.1).

Illustrative per-100g calorie bands, not production nutrition-team-curated
values - a starting point to develop and test estimated-dish resolution
against, same "not the production pipeline" framing as `seed_foods.py`.

    python manage.py seed_dish_category_profiles
    python manage.py seed_dish_category_profiles --reset   # delete and re-seed
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from common.db import get_client

PROFILES = [
    {"category": "SPICED_CURRY", "caloriesKcalP25Per100g": 120.0, "caloriesKcalP75Per100g": 220.0},
    {"category": "FRIED_SNACK", "caloriesKcalP25Per100g": 250.0, "caloriesKcalP75Per100g": 400.0},
    {"category": "CREAMY_PASTA", "caloriesKcalP25Per100g": 150.0, "caloriesKcalP75Per100g": 250.0},
    {"category": "CLEAR_SOUP", "caloriesKcalP25Per100g": 20.0, "caloriesKcalP75Per100g": 60.0},
    {"category": "GRAIN_BOWL", "caloriesKcalP25Per100g": 100.0, "caloriesKcalP75Per100g": 180.0},
]


class Command(BaseCommand):
    help = "Seed (idempotently) the curated DishCategoryProfile starter set."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete every existing DishCategoryProfile row before seeding.",
        )

    def handle(self, *args, **options) -> None:
        client = get_client()

        if options["reset"]:
            client.dishcategoryprofile.delete_many()
            self.stdout.write("cleared existing dish category profiles")

        for entry in PROFILES:
            existing = client.dishcategoryprofile.find_unique(
                where={"category": entry["category"]}
            )
            if existing is not None:
                self.stdout.write("skip (exists): {}".format(entry["category"]))
                continue

            client.dishcategoryprofile.create(data=entry)
            self.stdout.write(self.style.SUCCESS("created: {}".format(entry["category"])))
