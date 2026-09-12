"""Source-agnostic pieces of catalog ingestion: the normalized record every
parser yields, plausibility validation, and the name/unit heuristics shared
across sources.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from chatparser.units import UNIT_WORDS
from nutrition.enums import FoodState, ServingUnitType

# Units a food can carry as a `FoodServingUnit`. g/kg are universal in
# `nutrition.units.resolve_grams` and never stored per food.
_SERVING_UNIT_TYPES = {
    "katori": ServingUnitType.HOUSEHOLD,
    "cup": ServingUnitType.HOUSEHOLD,
    "tbsp": ServingUnitType.HOUSEHOLD,
    "tsp": ServingUnitType.HOUSEHOLD,
    "bowl": ServingUnitType.HOUSEHOLD,
    "plate": ServingUnitType.HOUSEHOLD,
    "piece": ServingUnitType.COUNTABLE,
    "slice": ServingUnitType.COUNTABLE,
}

_COOKED_WORDS = (
    "cooked", "boiled", "roasted", "grilled", "fried", "baked", "steamed",
    "stewed", "braised", "broiled", "sauteed", "poached", "microwaved",
)
_COOKED_RE = re.compile(r"\b(?:{})\b".format("|".join(_COOKED_WORDS)))
_RAW_RE = re.compile(r"\braw\b")

# Energy-consistency tolerance for `validate(check_energy=True)`. Below the
# floor, rounding in the source data dominates the Atwater estimate, so
# low-energy foods (leafy vegetables, diet drinks) are never flagged.
_ENERGY_TOLERANCE = 0.25
_ENERGY_FLOOR_KCAL = 40.0

# How far below zero a nutrient may sit and still read as zero rather than as
# bad data - see `FoodRecord.__post_init__`.
_NEGATIVE_TOLERANCE = 1.0
_NUTRIENT_FIELDS = (
    "calories_kcal", "protein_g", "carbs_g", "fat_g", "fiber_g", "sugar_g",
    "saturated_fat_g", "sodium_mg", "cholesterol_mg",
)


@dataclass(frozen=True)
class ServingUnitRecord:
    unit: str
    grams: float  # grams per one `unit`, as-served
    type: str


@dataclass
class FoodRecord:
    """One catalog row, per 100g, in `Food` column units. Optional nutrients
    are `None` when the source doesn't carry them - never 0 (§8)."""

    name: str
    source: str
    source_ref: str
    calories_kcal: Optional[float]
    protein_g: Optional[float]
    carbs_g: Optional[float]
    fat_g: Optional[float]
    fiber_g: Optional[float] = None
    sugar_g: Optional[float] = None
    saturated_fat_g: Optional[float] = None
    sodium_mg: Optional[float] = None
    cholesterol_mg: Optional[float] = None
    brand: Optional[str] = None
    default_state: str = FoodState.UNSPECIFIED.value
    default_serving_grams: Optional[float] = None
    category: Optional[str] = None
    serving_units: List[ServingUnitRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Clamp negligible negative nutrients to 0.

        USDA computes carbohydrate "by difference" (100g minus water, protein,
        fat, ash), which underflows slightly negative on raw meats - Foundation
        lists chicken breast at -0.43g carbs. That's an artifact of the
        formula, not a measurement: a food has no negative carbohydrate, and
        rejecting chicken/lamb/bison over it would be worse than reading it as
        zero. Anything past `_NEGATIVE_TOLERANCE` is left alone for `validate`
        to reject as the data error it is.
        """
        for name in _NUTRIENT_FIELDS:
            value = getattr(self, name)
            if value is not None and -_NEGATIVE_TOLERANCE <= value < 0:
                setattr(self, name, 0.0)

    def to_food_data(self) -> Dict[str, Any]:
        """The Prisma `Food` create payload, without `servingUnits` (written
        separately so a batch can use `create_many`)."""
        return {
            "name": self.name,
            "source": self.source,
            "sourceRef": self.source_ref,
            "brand": self.brand,
            "defaultState": self.default_state,
            "caloriesKcalPer100g": self.calories_kcal,
            "proteinGPer100g": self.protein_g,
            "carbsGPer100g": self.carbs_g,
            "fatGPer100g": self.fat_g,
            "fiberGPer100g": self.fiber_g,
            "sugarGPer100g": self.sugar_g,
            "saturatedFatGPer100g": self.saturated_fat_g,
            "sodiumMgPer100g": self.sodium_mg,
            "cholesterolMgPer100g": self.cholesterol_mg,
            "defaultServingGrams": self.default_serving_grams,
            "category": self.category,
        }


def validate(record: FoodRecord, *, check_energy: bool = False) -> Optional[str]:
    """`None` if the record is plausible enough to import, otherwise a short
    skip reason the ingest command tallies.

    `check_energy` compares stated kcal against 4P + 4C + 9F. It's meant for
    crowdsourced data (Open Food Facts, where kJ-typed-as-kcal is common),
    not lab-derived tables: USDA/INDB energy is authoritative, and alcohol
    (7 kcal/g, absent from the formula) would wrongly fail every beer and
    wine.
    """
    if not record.name.strip():
        return "missing_name"
    core = (record.calories_kcal, record.protein_g, record.carbs_g, record.fat_g)
    if any(value is None for value in core):
        return "missing_macro"
    optional = (
        record.fiber_g,
        record.sugar_g,
        record.saturated_fat_g,
        record.sodium_mg,
        record.cholesterol_mg,
    )
    if any(value is not None and value < 0 for value in core + optional):
        return "negative_value"
    kcal, protein, carbs, fat = core
    if protein + carbs + fat > 105.0:
        return "macro_sum"
    if check_energy:
        expected = 4 * protein + 4 * carbs + 9 * fat
        larger = max(kcal, expected)
        if larger >= _ENERGY_FLOOR_KCAL and abs(kcal - expected) > _ENERGY_TOLERANCE * larger:
            return "kcal_mismatch"
    return None


def infer_state(name: str) -> str:
    """Default raw/cooked state from the food's own name. Both or neither
    word present -> UNSPECIFIED rather than a guess."""
    lowered = name.lower()
    cooked = bool(_COOKED_RE.search(lowered))
    raw = bool(_RAW_RE.search(lowered))
    if cooked and not raw:
        return FoodState.COOKED.value
    if raw and not cooked:
        return FoodState.RAW.value
    return FoodState.UNSPECIFIED.value


def normalize_unit(text: Optional[str]) -> Optional[str]:
    """A source's portion/serving label -> a canonical storable unit
    ("cup, chopped" -> "cup", "Tablespoons" -> "tbsp"), via the same
    `UNIT_WORDS` vocabulary the chat grammar parses with - a stored unit the
    parser can't produce would be unreachable. `None` for anything else."""
    if not text:
        return None
    match = re.match(r"\s*([a-zA-Z]+)", text)
    if match is None:
        return None
    canonical = UNIT_WORDS.get(match.group(1).lower())
    return canonical if canonical in _SERVING_UNIT_TYPES else None


def serving_unit(unit: str, grams: float) -> ServingUnitRecord:
    return ServingUnitRecord(unit=unit, grams=round(grams, 2), type=_SERVING_UNIT_TYPES[unit].value)


def parse_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None
