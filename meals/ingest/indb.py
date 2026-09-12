"""Indian Nutrient Databank (INDB) parser - Indian raw foods and recipes.

INDB (Anuvaad Solutions, derived from ICMR-NIN IFCT 2017 with UK/US gap
filling) is published as `INDB.xlsx` at
https://www.anuvaad.org.in/indian-nutrient-databank/ and
https://github.com/lindsayjaacks/Indian-Nutrient-Databank-INDB-. Save its
sheet as CSV and pass that path - reading CSV keeps ingestion stdlib-only.
UTF-8 and Excel's Windows-1252 "CSV" export are both handled. Values are
per 100g.

Attribution: cite INDB / Anuvaad in the nutrition-source sheet (§5.2.1). The
GitHub repo carries no license file - confirm redistribution terms with
Anuvaad before shipping to production.
"""
from __future__ import annotations

import csv
import io
from typing import Dict, Iterator, Optional

from meals.ingest.common import FoodRecord, infer_state, normalize_unit, parse_float, serving_unit
from nutrition.enums import FoodSource

# INDB column -> FoodRecord field. Column names follow the INDB/NIN_fct
# variable list; verify against the downloaded file if a release renames any.
# `freesugar_g` is deliberately not mapped: free sugars are a subset of total
# sugars, and storing them as `sugarGPer100g` would silently understate.
COLUMNS = {
    "code": "food_code",
    "name": "food_name",
    "calories_kcal": "energy_kcal",
    "protein_g": "protein_g",
    "carbs_g": "carb_g",
    "fat_g": "fat_g",
    "fiber_g": "fibre_g",
    "saturated_fat_mg": "sfa_mg",  # INDB reports SFA in mg; converted to g
    "sodium_mg": "sodium_mg",
    "cholesterol_mg": "cholesterol_mg",
    # Recipes only: household serving label + that serving's energy, from
    # which grams-per-serving is derived.
    "serving_unit": "servings_unit",
    "serving_kcal": "unit_serving_energy_kcal",
}


def parse_indb(path: str) -> Iterator[FoodRecord]:
    # newline="" mirrors open(path, newline="") - csv needs raw, untranslated
    # line endings to recognize a "\r\n" inside a quoted multi-line field;
    # StringIO's default newline="\n" strips that and confuses the reader.
    for row in csv.DictReader(io.StringIO(_read_text(path), newline="")):
        record = _record(row)
        if record is not None:
            yield record


def _read_text(path: str) -> str:
    """INDB.csv is a manual Excel export, and Excel's plain "CSV" format
    (as opposed to "CSV UTF-8") writes Windows-1252, not UTF-8 - any curly
    quote or dash in a food name then breaks strict UTF-8 decoding partway
    through the file. Fall back to cp1252 rather than fail the whole ingest
    over a handful of punctuation bytes."""
    with open(path, "rb") as handle:
        raw = handle.read()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _record(row: Dict[str, str]) -> Optional[FoodRecord]:
    code = (row.get(COLUMNS["code"]) or "").strip()
    if not code:
        return None
    name = (row.get(COLUMNS["name"]) or "").strip()
    kcal = _value(row, "calories_kcal")
    saturated_mg = _value(row, "saturated_fat_mg")

    units = []
    default_serving = None
    unit = normalize_unit(row.get(COLUMNS["serving_unit"]))
    serving_kcal = _value(row, "serving_kcal")
    if unit and serving_kcal and kcal:
        grams = serving_kcal / kcal * 100.0
        units.append(serving_unit(unit, grams))
        default_serving = units[0].grams

    return FoodRecord(
        name=name,
        source=FoodSource.INDB.value,
        source_ref=code,
        calories_kcal=kcal,
        protein_g=_value(row, "protein_g"),
        carbs_g=_value(row, "carbs_g"),
        fat_g=_value(row, "fat_g"),
        fiber_g=_value(row, "fiber_g"),
        saturated_fat_g=saturated_mg / 1000.0 if saturated_mg is not None else None,
        sodium_mg=_value(row, "sodium_mg"),
        cholesterol_mg=_value(row, "cholesterol_mg"),
        default_state=infer_state(name),
        default_serving_grams=default_serving,
        serving_units=units,
    )


def _value(row: Dict[str, str], field: str) -> Optional[float]:
    return parse_float(row.get(COLUMNS[field]))
