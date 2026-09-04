"""CaloryX meal-logging nutrition engine (PRD §8).

Deliberately dependency-free: no Django, no Prisma, no I/O - mirrors `engine/`'s
boundary (see README's "calculation engine runs in-process" note). Item and
meal nutrition always derive from a food's per-100g vector and a resolved gram
amount; nothing here ever accepts a pre-computed calorie number, which is the
structural half of the PRD's header principle - "the nutrition database
determines what a meal contains" - that this package exists to enforce.
"""
from .calculator import NutrientVector, NutritionError, ZERO_VECTOR, apply_yield, item_nutrition, sum_nutrition
from .enums import FoodSource, FoodState, LoggedMealSource, MealSlot, ServingUnitType
from .units import ServingUnit, UnknownServingUnitError, resolve_grams

__all__ = [
    "FoodSource",
    "FoodState",
    "LoggedMealSource",
    "MealSlot",
    "NutrientVector",
    "NutritionError",
    "ServingUnit",
    "ServingUnitType",
    "UnknownServingUnitError",
    "ZERO_VECTOR",
    "apply_yield",
    "item_nutrition",
    "resolve_grams",
    "sum_nutrition",
]
