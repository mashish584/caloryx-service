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
