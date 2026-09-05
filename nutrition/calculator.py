"""Deterministic nutrition math (PRD §8).

Pure and side-effect free: no DB, no Django, no I/O. Item nutrition is always
`per_100g x grams / 100`; meal totals are the sum of item vectors. Rounding is
applied exactly once, by the caller at display time (§8) - every function here
returns full-precision floats so summing many items never drifts from what
rounding each one first would have produced.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Tuple

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
    # Optional like `fiber_g` (Chunk 5b): an estimated-dish item (§7.6.1) has
    # a deterministic calorie midpoint but no macro breakdown at all - "—" in
    # the UI, not a guessed zero. Every other caller still passes concrete
    # floats, so this only ever matters for that one new case.
    protein_g: Optional[float]
    carbs_g: Optional[float]
    fat_g: Optional[float]
    fiber_g: Optional[float] = None

    def __add__(self, other: "NutrientVector") -> "NutrientVector":
        return NutrientVector(
            calories_kcal=self.calories_kcal + other.calories_kcal,
            protein_g=_sum_optional(self.protein_g, other.protein_g),
            carbs_g=_sum_optional(self.carbs_g, other.carbs_g),
            fat_g=_sum_optional(self.fat_g, other.fat_g),
            fiber_g=_sum_optional(self.fiber_g, other.fiber_g),
        )


# Macros start unknown, not zero (Chunk 5b) - matching `fiber_g`'s existing
# value here. This is the fold's identity element for `sum_nutrition`: a
# concrete starting zero would silently turn the *first* unknown macro it
# combines with into a false confirmed zero (`_sum_optional(0.0, None) ==
# 0.0`), whereas starting from unknown correctly propagates
# (`_sum_optional(None, None) is None`, `_sum_optional(None, 5.4) == 5.4`) -
# the same numeric answer as before whenever at least one real vector has a
# known value, and an honest `None` only when none of them do.
ZERO_VECTOR = NutrientVector(0.0, None, None, None, None)


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


def estimated_dish_nutrition(
    p25_per_100g: float, p75_per_100g: float, grams: float
) -> Tuple[float, float, float]:
    """A `DishCategoryProfile`'s per-100g p25-p75 band x grams -> `(kcal_low,
    kcal_high, kcal_midpoint)` (§7.6.1). The one formula both the message-time
    resolution and the confirm-time recompute call, so an estimated dish's
    range math exists in exactly one place - never the model's own number."""
    factor = grams / 100.0
    kcal_low = p25_per_100g * factor
    kcal_high = p75_per_100g * factor
    return kcal_low, kcal_high, (kcal_low + kcal_high) / 2.0


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
