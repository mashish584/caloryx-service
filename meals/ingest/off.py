"""Open Food Facts parser - branded / packaged products.

Reads the full CSV export (`en.openfoodfacts.org.products.csv.gz`, tab-
separated, from https://world.openfoodfacts.org/data), streamed through
`gzip` without decompressing to disk or loading into memory - the export is
millions of rows. A plain `.csv` path works too.

Every country by default; `countries` narrows it to products tagged as sold
in any of the given `countries_tags` (e.g. "en:india"). Only products with a
name and all four core macros are yielded; `validate(check_energy=True)` then
drops crowdsourced entry errors (kJ typed as kcal and similar).

Names are OFF's `product_name`, i.e. the product's main language - a global
import carries French/German/etc. names, reachable by barcode or by that
name, not by English chat text.

Licensing: the database is ODbL - attribute Open Food Facts in the
nutrition-source sheet (§5.2.1), and a publicly redistributed derivative of
the *database* must stay ODbL.

Known limitation: OFF's `*_100g` fields are per 100 ml for liquids; they are
imported as-is, i.e. 1 ml is treated as 1 g.
"""
from __future__ import annotations

import csv
import gzip
import sys
from typing import Dict, Iterable, Iterator, Optional

from meals.ingest.common import FoodRecord, infer_state, parse_float
from nutrition.enums import FoodSource

# Some product fields (ingredients text) exceed csv's default 128KB limit.
csv.field_size_limit(sys.maxsize)


def parse_off(
    path: str,
    *,
    countries: Iterable[str] = (),
    min_scans: int = 0,
) -> Iterator[FoodRecord]:
    wanted = {c.strip().lower() for c in countries if c.strip()}
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle, delimiter="\t", quoting=csv.QUOTE_NONE):
            if not _in_countries(row, wanted):
                continue
            if min_scans and (parse_float(row.get("unique_scans_n")) or 0) < min_scans:
                continue
            record = _record(row)
            if record is not None:
                yield record


def _in_countries(row: Dict[str, str], wanted: set) -> bool:
    if not wanted:
        return True
    tags = {t.strip().lower() for t in (row.get("countries_tags") or "").split(",")}
    return bool(tags & wanted)


def _record(row: Dict[str, str]) -> Optional[FoodRecord]:
    code = (row.get("code") or "").strip()
    name = (row.get("product_name") or "").strip()
    if not code or not name:
        return None
    sodium_g = _num(row, "sodium_100g")
    cholesterol_g = _num(row, "cholesterol_100g")
    serving = _num(row, "serving_quantity")
    return FoodRecord(
        name=name,
        source=FoodSource.OPEN_FOOD_FACTS.value,
        source_ref=code,
        brand=_first_brand(row.get("brands")),
        calories_kcal=_num(row, "energy-kcal_100g"),
        protein_g=_num(row, "proteins_100g"),
        carbs_g=_num(row, "carbohydrates_100g"),
        fat_g=_num(row, "fat_100g"),
        fiber_g=_num(row, "fiber_100g"),
        sugar_g=_num(row, "sugars_100g"),
        saturated_fat_g=_num(row, "saturated-fat_100g"),
        # OFF stores sodium and cholesterol in g/100g; Food stores mg.
        sodium_mg=round(sodium_g * 1000.0, 3) if sodium_g is not None else None,
        cholesterol_mg=round(cholesterol_g * 1000.0, 3) if cholesterol_g is not None else None,
        default_state=infer_state(name),
        default_serving_grams=serving if serving and serving > 0 else None,
    )


def _first_brand(brands: Optional[str]) -> Optional[str]:
    first = (brands or "").split(",")[0].strip()
    return first or None


def _num(row: Dict[str, str], column: str) -> Optional[float]:
    """OFF's values carry float32 noise ("0.600000023841858") - rounded to 3
    decimals, well below any precision the source actually has."""
    value = parse_float(row.get(column))
    return round(value, 3) if value is not None else None
