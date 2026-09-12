"""Domain enums for meal logging (PRD §8, §9).

Values match the Prisma enums exactly, following the same convention as
`engine/enums.py` (which this reuses `StrEnum` from, rather than redefining it).
"""
from __future__ import annotations

from engine.enums import StrEnum


class FoodSource(StrEnum):
    USDA = "USDA"
    OPEN_FOOD_FACTS = "OPEN_FOOD_FACTS"
    CALORYX_CURATED = "CALORYX_CURATED"
    INDB = "INDB"


class FoodState(StrEnum):
    RAW = "RAW"
    COOKED = "COOKED"
    UNSPECIFIED = "UNSPECIFIED"


class ServingUnitType(StrEnum):
    CONTINUOUS = "CONTINUOUS"
    COUNTABLE = "COUNTABLE"
    HOUSEHOLD = "HOUSEHOLD"


class MealSlot(StrEnum):
    BREAKFAST = "BREAKFAST"
    LUNCH = "LUNCH"
    DINNER = "DINNER"
    SNACK = "SNACK"


class LoggedMealSource(StrEnum):
    MANUAL = "MANUAL"
    CHAT_AI = "CHAT_AI"


class FoodCategory(StrEnum):
    """Quantity-resolution ladder step 4's fallback bucket (PRD §5.1.1a) -
    grams per category live in `nutrition.units.CATEGORY_FALLBACK_GRAMS`."""

    GRAIN = "GRAIN"
    PROTEIN = "PROTEIN"
    VEGETABLE = "VEGETABLE"
    DRESSING = "DRESSING"
    OIL = "OIL"


class DishCategory(StrEnum):
    """Estimated-dish handling for an uncurated composite (PRD §7.6.1) - a
    closed, curated starter set. Adding a category is a curation change (new
    enum value + a seeded `DishCategoryProfile`), same posture as
    `CompositeFood`."""

    SPICED_CURRY = "SPICED_CURRY"
    FRIED_SNACK = "FRIED_SNACK"
    CREAMY_PASTA = "CREAMY_PASTA"
    CLEAR_SOUP = "CLEAR_SOUP"
    GRAIN_BOWL = "GRAIN_BOWL"
