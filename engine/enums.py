"""Domain enums.

Values of the persisted enums match the Prisma enums exactly (PRD §7). The
advisory enums at the bottom are wire-only - nothing stores them - so they keep
the lowercase form the client already receives.
"""
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


class AdvisorySeverity(StrEnum):
    """How prominently the client renders an advisory. Never blocking (§9)."""

    INFO = "info"
    WARNING = "warning"


class AdvisoryCode(StrEnum):
    """What the advisory is about. The client switches on this for copy and for
    analytics, so adding a member is a contract change."""

    WEIGHT_OUT_OF_TYPICAL_RANGE = "weight_out_of_typical_range"
    HEIGHT_OUT_OF_TYPICAL_RANGE = "height_out_of_typical_range"
    GOAL_TARGET_WEIGHT_CONFLICT = "goal_target_weight_conflict"
    TARGET_WEIGHT_BELOW_HEALTHY_BMI = "target_weight_below_healthy_bmi"
    CALORIES_CLAMPED_TO_FLOOR = "calories_clamped_to_floor"


class AdvisoryField(StrEnum):
    """Which request field the hint attaches to. Values are `ProfileUpsert` field
    names - one that is not orphans the message beside no input at all."""

    WEIGHT_KG = "weightKg"
    HEIGHT_CM = "heightCm"
    TARGET_WEIGHT_KG = "targetWeightKg"


# drf-spectacular matches ENUM_NAME_OVERRIDES against the exact choice list a
# field declares, so expose the values in the same form the serializers use.
ADVISORY_SEVERITY_CHOICES = [s.value for s in AdvisorySeverity]
ADVISORY_CODE_CHOICES = [c.value for c in AdvisoryCode]
ADVISORY_FIELD_CHOICES = [f.value for f in AdvisoryField]
