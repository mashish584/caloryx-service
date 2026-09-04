"""Deterministic nutrition math (PRD §8).

Pure and side-effect free: no DB, no Django, no I/O. Item nutrition is always
`per_100g x grams / 100`; meal totals are the sum of item vectors. Rounding is
applied exactly once, by the caller at display time (§8) - every function here
returns full-precision floats so summing many items never drifts from what
rounding each one first would have produced.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from .enums import FoodState


class NutritionError(Exception):
    """A conversion this module cannot perform at all (e.g. no yield factor)."""


def _sum_optional(a: Optional[float], b: Optional[float]) -> Optional[float]:
    # Missing != zero (§8): two unknowns stay unknown; one known value alongside
    # an unknown is still the closest honest total we can report.
    if a is None and b is None:
        return None
    return (a or 0.0) + (b or 0.0)


@dataclass(frozen=True)
class NutrientVector:
    calories_kcal: float
    protein_g: float
    carbs_g: float
    fat_g: float
    fiber_g: Optional[float] = None

    def __add__(self, other: "NutrientVector") -> "NutrientVector":
        return NutrientVector(
            calories_kcal=self.calories_kcal + other.calories_kcal,
            protein_g=self.protein_g + other.protein_g,
            carbs_g=self.carbs_g + other.carbs_g,
            fat_g=self.fat_g + other.fat_g,
            fiber_g=_sum_optional(self.fiber_g, other.fiber_g),
        )


ZERO_VECTOR = NutrientVector(0.0, 0.0, 0.0, 0.0, None)


def item_nutrition(per_100g: NutrientVector, grams: float) -> NutrientVector:
    """`per_100g x grams / 100` (§8) - the one formula every item's numbers come from."""
    factor = grams / 100.0
    return NutrientVector(
        calories_kcal=per_100g.calories_kcal * factor,
        protein_g=per_100g.protein_g * factor,
        carbs_g=per_100g.carbs_g * factor,
        fat_g=per_100g.fat_g * factor,
        fiber_g=per_100g.fiber_g * factor if per_100g.fiber_g is not None else None,
    )


def sum_nutrition(vectors: Iterable[NutrientVector]) -> NutrientVector:
    """Meal totals = sum(items) (§6) - never an independently authored number."""
    total = ZERO_VECTOR
    for vector in vectors:
        total = total + vector
    return total


def apply_yield(
    grams: float,
    *,
    from_state: FoodState,
    to_state: FoodState,
    raw_to_cooked_yield: Optional[float],
) -> float:
    """Convert a mass measured `from_state` to its `to_state` equivalent (§8) -
    rice raw vs. cooked differ ~3x, the largest single accuracy risk in
    text-based logging.

    `raw_to_cooked_yield` is grams-cooked-per-gram-raw for this food.
    UNSPECIFIED on either side means "no distinction was captured", not "no
    conversion is needed" in a way that could hide a real one - treated the
    same as a match: nothing to convert.
    """
    if from_state == to_state or FoodState.UNSPECIFIED in (from_state, to_state):
        return grams

    if raw_to_cooked_yield is None:
        raise NutritionError(
            "No yield factor available to convert {} to {}.".format(
                from_state.value, to_state.value
            )
        )

    if from_state == FoodState.RAW and to_state == FoodState.COOKED:
        return grams * raw_to_cooked_yield
    if from_state == FoodState.COOKED and to_state == FoodState.RAW:
        return grams / raw_to_cooked_yield

    raise NutritionError(  # pragma: no cover - exhaustive for a 3-member enum
        "Unsupported state conversion: {} to {}.".format(from_state.value, to_state.value)
    )
