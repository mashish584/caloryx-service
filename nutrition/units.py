"""Quantity + unit -> canonical grams (PRD §8).

Grams are the only mass nutrition math ever runs on. `g`/`kg` are universal -
any food can be logged by mass with no catalog entry - anything else (a
household measure or a countable unit) must be declared per food via
`FoodServingUnit`, because a cup of rice and a cup of spinach are not the same
mass (§8).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .enums import FoodCategory

_GRAM_UNITS = {"g": 1.0, "kg": 1000.0}

# Quantity-resolution ladder (PRD §5.1.1a). Step 2's multiplier applies on top
# of whichever base serving (step 3's `Food.defaultServingGrams`, or step 4's
# category fallback below) ends up firing - "small"/"large" scale a serving,
# they are never a serving amount on their own. A qualifier the model didn't
# recognize (or none stated) is treated as "medium" (1.0x, a no-op).
SIZE_QUALIFIER_MULTIPLIERS = {"small": 0.7, "medium": 1.0, "large": 1.4}

# Ladder step 4 - a flat default for a food with no `defaultServingGrams`
# catalogued yet. Literal values per §5.1.1a; revisit only alongside a
# curation pass, not per-code-change.
CATEGORY_FALLBACK_GRAMS = {
    FoodCategory.GRAIN: 150.0,
    FoodCategory.PROTEIN: 100.0,
    FoodCategory.VEGETABLE: 80.0,
    FoodCategory.DRESSING: 20.0,
    FoodCategory.OIL: 5.0,
}


class UnknownServingUnitError(Exception):
    """`unit` is neither a gram unit nor a declared serving unit for this food."""


@dataclass(frozen=True)
class ServingUnit:
    unit: str
    grams: float  # grams per one `unit`, as-served
    type: str  # ServingUnitType value; carried through, not interpreted here


def resolve_grams(quantity: float, unit: str, serving_units: Iterable[ServingUnit]) -> float:
    """`quantity` of `unit` -> grams, using the food's declared serving units."""
    normalized = unit.strip().lower()
    if normalized in _GRAM_UNITS:
        return quantity * _GRAM_UNITS[normalized]

    for serving_unit in serving_units:
        if serving_unit.unit.strip().lower() == normalized:
            return quantity * serving_unit.grams

    raise UnknownServingUnitError(
        "'{}' is not a recognised unit for this food.".format(unit)
    )
