"""Bulk food-catalog ingestion (PRD §8 "Data sources").

USDA FoodData Central (generic), INDB (Indian foods and recipes), and Open
Food Facts (branded) are all normalized into the local `Food` table here, at
ingest time - never looked up live on the logging path. Each source module is
a pure parser over local bulk-download files (no DB, no network) yielding
`FoodRecord`s; `manage.py ingest_foods` owns the writes.
"""
from meals.ingest.common import FoodRecord, ServingUnitRecord, validate

__all__ = ["FoodRecord", "ServingUnitRecord", "validate"]
