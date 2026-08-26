"""Domain enums. Values match the Prisma enums exactly (PRD §7)."""
from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """str-backed enum so values serialise directly to JSON and compare to strings."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)


class SexAtBirth(StrEnum):
    MALE = "MALE"
    FEMALE = "FEMALE"
    # Defensive-only (§6.4). Onboarding v7 never produces this; imported or
    # malformed records can, and the engine must not throw on them.
    UNSPECIFIED = "UNSPECIFIED"


class Goal(StrEnum):
    LOSE = "LOSE"
    MAINTAIN = "MAINTAIN"
    GAIN = "GAIN"


class ActivityLevel(StrEnum):
    SEDENTARY = "SEDENTARY"
    LIGHT = "LIGHT"
    MODERATE = "MODERATE"
    VERY = "VERY"
    EXTREME = "EXTREME"


class UnitSystem(StrEnum):
    """DEPRECATED - superseded by WeightUnit + HeightUnit. Retained only so
    `backfill_unit_preferences` can read the old column."""

    METRIC = "METRIC"
    IMPERIAL = "IMPERIAL"


class WeightUnit(StrEnum):
    """Display unit for current and target weight (§5.2). Never a storage unit -
    the wire and the database are always kg."""

    KG = "KG"
    LB = "LB"


class HeightUnit(StrEnum):
    """Display unit for height (§5.2). Storage is always cm."""

    CM = "CM"
    FT_IN = "FT_IN"


class AuthProvider(StrEnum):
    GUEST = "GUEST"
    CLERK = "CLERK"
