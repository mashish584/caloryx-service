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
    METRIC = "METRIC"
    IMPERIAL = "IMPERIAL"


class AuthProvider(StrEnum):
    GUEST = "GUEST"
    CLERK = "CLERK"
