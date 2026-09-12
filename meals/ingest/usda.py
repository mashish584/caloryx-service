"""USDA FoodData Central bulk-CSV parser (generic foods, CC0).

Reads one *unzipped* FDC CSV download directory - SR Legacy, Foundation, or
Survey (FNDDS) - from https://fdc.nal.usda.gov/download-datasets. Branded
Foods is deliberately not handled: branded products come from Open Food Facts
(PRD §8), and FDC's branded set is US-only.

Files read: `food.csv`, `food_nutrient.csv`, and (when present)
`food_portion.csv`, `measure_unit.csv`, `food_category.csv`,
`wweia_food_category.csv`. Streamed row-by-row with the stdlib `csv` module;
only the nutrients mapped below are held in memory.
"""
from __future__ import annotations

import csv
import os
import re
from typing import Dict, Iterator, List, Optional, Tuple

from meals.ingest.common import FoodRecord, parse_float, infer_state, normalize_unit, serving_unit
from nutrition.enums import FoodCategory, FoodSource

_DATA_TYPES = {"sr_legacy_food", "foundation_food", "survey_fndds_food"}

# FDC nutrient ids, in fallback order - Foundation Foods often omits the
# classic id and carries only the Atwater/NLEA/summation variant.
_NUTRIENT_IDS: Dict[str, Tuple[str, ...]] = {
    "calories_kcal": ("1008", "2048", "2047"),  # Energy (kcal); Atwater specific; general
    "protein_g": ("1003",),
    "fat_g": ("1004", "1085"),  # Total lipid; Total fat (NLEA)
    "carbs_g": ("1005", "1050"),  # By difference; by summation
    "fiber_g": ("1079",),
    "sugar_g": ("2000", "1063"),  # Total incl. NLEA; Sugars, Total
    "saturated_fat_g": ("1258",),
    "sodium_mg": ("1093",),
    "cholesterol_mg": ("1253",),
}
_WANTED_NUTRIENTS = {nid for ids in _NUTRIENT_IDS.values() for nid in ids}

# FNDDS's own "as typically eaten, amount unstated" portion - exactly ladder
# step 3's canonical serving (§5.1.1a).
_UNSPECIFIED_PORTION = "quantity not specified"

# Coarse keyword -> ladder step 4 bucket, over FDC food-category / WWEIA
# category descriptions. First match wins; `None` (no category) is the safe
# default - a wrong category is a wrong silent serving size.
_CATEGORY_RULES: Tuple[Tuple[Tuple[str, ...], Optional[str]], ...] = (
    (("dairy",), None),  # "Dairy and Egg Products" mixes milk, cheese, butter, eggs
    (("dressing",), FoodCategory.DRESSING.value),
    (("oil", "fats", "margarine"), FoodCategory.OIL.value),
    (("vegetable",), FoodCategory.VEGETABLE.value),
    (("cereal", "grain", "pasta", "rice", "bread", "tortilla", "noodle"), FoodCategory.GRAIN.value),
    (
        ("poultry", "chicken", "turkey", "beef", "pork", "lamb", "fish", "shellfish",
         "legume", "beans", "egg", "sausage"),
        FoodCategory.PROTEIN.value,
    ),
)


def parse_usda(directory: str) -> Iterator[FoodRecord]:
    foods = _read_foods(directory)
    nutrients = _read_nutrients(directory, foods)
    portions = _read_portions(directory, foods)
    categories = _read_categories(directory)

    for fdc_id, (description, category_id) in foods.items():
        values = nutrients.get(fdc_id, {})
        units, default_serving = portions.get(fdc_id, ([], None))
        yield FoodRecord(
            name=description,
            source=FoodSource.USDA.value,
            source_ref=fdc_id,
            default_state=infer_state(description),
            default_serving_grams=default_serving,
            category=map_category(categories.get(category_id)),
            serving_units=units,
            **{field: _first(values, ids) for field, ids in _NUTRIENT_IDS.items()},
        )


def map_category(description: Optional[str]) -> Optional[str]:
    if not description:
        return None
    lowered = description.lower()
    for keywords, category in _CATEGORY_RULES:
        if any(keyword in lowered for keyword in keywords):
            return category
    return None


def _first(values: Dict[str, float], ids: Tuple[str, ...]) -> Optional[float]:
    for nid in ids:
        if nid in values:
            return values[nid]
    return None


def _rows(directory: str, filename: str) -> Iterator[Dict[str, str]]:
    path = os.path.join(directory, filename)
    if not os.path.exists(path):
        return
    with open(path, newline="", encoding="utf-8") as handle:
        yield from csv.DictReader(handle)


def _read_foods(directory: str) -> Dict[str, Tuple[str, str]]:
    if not os.path.exists(os.path.join(directory, "food.csv")):
        raise FileNotFoundError("food.csv not found in {}".format(directory))
    foods: Dict[str, Tuple[str, str]] = {}
    for row in _rows(directory, "food.csv"):
        # A Foundation download's food.csv also lists its lab sample/sub-sample
        # rows - only the finished food entries are catalog material.
        if row.get("data_type") in _DATA_TYPES and row.get("description", "").strip():
            foods[row["fdc_id"]] = (row["description"].strip(), row.get("food_category_id", ""))
    return foods


def _read_nutrients(directory: str, foods: Dict[str, Tuple[str, str]]) -> Dict[str, Dict[str, float]]:
    # SR Legacy/Foundation key `food_nutrient.nutrient_id` by nutrient id
    # (1008 = energy); the Survey/FNDDS download keys the same column by the
    # legacy nutrient *number* (208 = energy). `nutrient.csv` maps number ->
    # id; the two ranges don't overlap (numbers < 1000 <= ids).
    number_to_id = {
        row["nutrient_nbr"]: row["id"]
        for row in _rows(directory, "nutrient.csv")
        if row.get("nutrient_nbr") and row.get("id") in _WANTED_NUTRIENTS
    }
    nutrients: Dict[str, Dict[str, float]] = {}
    for row in _rows(directory, "food_nutrient.csv"):
        fdc_id = row.get("fdc_id")
        nutrient_id = row.get("nutrient_id")
        nutrient_id = number_to_id.get(nutrient_id, nutrient_id)
        if fdc_id not in foods or nutrient_id not in _WANTED_NUTRIENTS:
            continue
        amount = parse_float(row.get("amount"))
        if amount is not None:
            nutrients.setdefault(fdc_id, {})[nutrient_id] = amount
    return nutrients


def _read_portions(
    directory: str, foods: Dict[str, Tuple[str, str]]
) -> Dict[str, Tuple[List, Optional[float]]]:
    measure_units = {
        row["id"]: row.get("name", "")
        for row in _rows(directory, "measure_unit.csv")
        if row.get("name", "").lower() != "undetermined"
    }
    raw: Dict[str, List[Tuple[float, Dict[str, str]]]] = {}
    for row in _rows(directory, "food_portion.csv"):
        if row.get("fdc_id") in foods:
            seq = parse_float(row.get("seq_num")) or 0.0
            raw.setdefault(row["fdc_id"], []).append((seq, row))

    portions: Dict[str, Tuple[List, Optional[float]]] = {}
    for fdc_id, rows in raw.items():
        units: List = []
        seen = set()
        default_serving: Optional[float] = None
        for _, row in sorted(rows, key=lambda pair: pair[0]):
            gram_weight = parse_float(row.get("gram_weight"))
            if gram_weight is None or gram_weight <= 0:
                continue
            description = row.get("portion_description", "")
            if description.strip().lower() == _UNSPECIFIED_PORTION:
                default_serving = gram_weight
                continue
            unit = (
                normalize_unit(measure_units.get(row.get("measure_unit_id", "")))
                or normalize_unit(row.get("modifier"))
                or normalize_unit(_strip_leading_amount(description))
            )
            # First (lowest seq_num) portion per unit wins - FoodServingUnit
            # is unique per (food, unit), and "cup, chopped" vs "cup, sliced"
            # can't both be "the" cup.
            if unit is None or unit in seen:
                continue
            amount = parse_float(row.get("amount")) or _leading_amount(description) or 1.0
            if amount <= 0:
                continue
            seen.add(unit)
            units.append(serving_unit(unit, gram_weight / amount))
        portions[fdc_id] = (units, default_serving)
    return portions


def _read_categories(directory: str) -> Dict[str, str]:
    categories = {
        row["id"]: row.get("description", "") for row in _rows(directory, "food_category.csv")
    }
    for row in _rows(directory, "wweia_food_category.csv"):
        categories[row["wweia_food_category"]] = row.get("wweia_food_category_description", "")
    return categories


_LEADING_AMOUNT = re.compile(r"^\s*(\d+(?:\.\d+)?)(?:\s*/\s*(\d+))?\s*")


def _leading_amount(text: str) -> Optional[float]:
    """"1 cup" -> 1.0, "1/2 cup" -> 0.5, "cup" -> None."""
    match = _LEADING_AMOUNT.match(text or "")
    if match is None:
        return None
    numerator = float(match.group(1))
    return numerator / float(match.group(2)) if match.group(2) else numerator


def _strip_leading_amount(text: str) -> str:
    return _LEADING_AMOUNT.sub("", text or "", count=1)
