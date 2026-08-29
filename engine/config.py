"""Engine tuning constants.

PRD §10 requires activity multipliers, goal adjustments, macro ratios and safety
floors to live in *server* config so they are tunable without an app release.

Resolution order (lowest to highest precedence):
    1. the compiled defaults below,
    2. the active `EngineConfig` row in Postgres.

The dataclass is deliberately free of any DB or Django import so the calculator
stays a pure, independently testable unit (and stays extractable into its own
service later, per PRD §8).
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any, Dict, Mapping, Optional

from .enums import ActivityLevel, Goal, SexAtBirth


@dataclass(frozen=True)
class EngineConfig:
    # Identity of the config row that produced a plan, so a stored plan stays
    # explainable after the constants are retuned. None == compiled defaults.
    id: Optional[str] = None
    name: str = "default"

    # Goal adjustments, kcal (§5.3). Locked at -/+400 for v1.
    lose_adjustment_kcal: int = -400
    gain_adjustment_kcal: int = 400

    # Half-width of the MAINTAIN band, kg (§5.3). The goal is derived from the
    # target weight rather than asked for, so this is what separates "hold
    # steady" from a 400 kcal adjustment - see `goal_for`.
    maintain_band_kg: float = 1.0

    # TDEE activity multipliers (§5.4).
    sedentary_multiplier: float = 1.2
    light_multiplier: float = 1.375
    moderate_multiplier: float = 1.55
    very_multiplier: float = 1.725
    extreme_multiplier: float = 1.9

    # Macro split (§6.3).
    protein_per_kg: float = 1.8
    fat_pct: float = 0.25
    fiber_g_per_1000_kcal: float = 15.0

    # Safety floors (§6.2). UNSPECIFIED uses the higher, safer floor.
    floor_male_kcal: int = 1500
    floor_female_kcal: int = 1200
    floor_unspecified_kcal: int = 1500

    # The target is rounded to this granularity before display and storage.
    # 10 kcal reproduces the PRD's worked example (2,256 - 400 = 1,856 -> 1,860).
    target_rounding_kcal: int = 10

    # Energy density of body mass, used for the "~0.4 kg / week" rationale (§6.5).
    kcal_per_kg_body_mass: float = 7700.0

    def goal_for(self, weight_kg: float, target_weight_kg: float) -> Goal:
        """Derive the goal from the two weights (§5.3).

        The goal screen asked for an answer the target weight already carries,
        so the direction of the target is now the single source of truth. The
        band matters in both directions: someone who wants to hold steady should
        not have to type their current weight to the decimal, and a difference
        the size of a rounding error must not silently buy a 400 kcal deficit.
        """
        delta = target_weight_kg - weight_kg
        if abs(delta) <= self.maintain_band_kg:
            return Goal.MAINTAIN
        return Goal.LOSE if delta < 0 else Goal.GAIN

    def adjustment_for(self, goal: Goal) -> int:
        if goal is Goal.LOSE:
            return self.lose_adjustment_kcal
        if goal is Goal.GAIN:
            return self.gain_adjustment_kcal
        return 0

    def multiplier_for(self, activity: ActivityLevel) -> float:
        return {
            ActivityLevel.SEDENTARY: self.sedentary_multiplier,
            ActivityLevel.LIGHT: self.light_multiplier,
            ActivityLevel.MODERATE: self.moderate_multiplier,
            ActivityLevel.VERY: self.very_multiplier,
            ActivityLevel.EXTREME: self.extreme_multiplier,
        }[activity]

    def floor_for(self, sex: SexAtBirth) -> int:
        return {
            SexAtBirth.MALE: self.floor_male_kcal,
            SexAtBirth.FEMALE: self.floor_female_kcal,
            SexAtBirth.UNSPECIFIED: self.floor_unspecified_kcal,
        }[sex]

    def to_public_dict(
        self, validation: Optional[Mapping[str, Any]] = None
    ) -> Dict[str, Any]:
        """Shape handed to the client so its optimistic preview (§5.5) uses the
        same constants the server will. Never include anything secret here.

        `validation` carries the input bounds the API enforces (§9). They are
        injected rather than read here because they live in Django settings and
        the onboarding serializers, and this module stays free of both - see the
        purity note in the module docstring. Omitted when not supplied, so the
        engine's own tests can call this with no Django context.
        """
        payload: Dict[str, Any] = {
            "configName": self.name,
            "goalAdjustmentsKcal": {
                Goal.LOSE.value: self.lose_adjustment_kcal,
                Goal.MAINTAIN.value: 0,
                Goal.GAIN.value: self.gain_adjustment_kcal,
            },
            # The client derives the goal the same way the server does (§5.5),
            # so the threshold has to travel with the adjustments it selects
            # between - a hardcoded copy would drift the moment this is retuned.
            "maintainBandKg": self.maintain_band_kg,
            "activityMultipliers": {
                level.value: self.multiplier_for(level) for level in ActivityLevel
            },
            "macros": {
                "proteinPerKg": self.protein_per_kg,
                "fatPct": self.fat_pct,
                "fiberGPer1000Kcal": self.fiber_g_per_1000_kcal,
            },
            "safetyFloorsKcal": {
                SexAtBirth.MALE.value: self.floor_male_kcal,
                SexAtBirth.FEMALE.value: self.floor_female_kcal,
                SexAtBirth.UNSPECIFIED.value: self.floor_unspecified_kcal,
            },
            "targetRoundingKcal": self.target_rounding_kcal,
            "kcalPerKgBodyMass": self.kcal_per_kg_body_mass,
        }
        if validation is not None:
            # Deep, not `dict(...)`: the ranges are nested one level, and a
            # shallow copy would leave the caller holding a handle into a payload
            # that may well be cached.
            payload["validation"] = deepcopy(dict(validation))
        return payload


DEFAULT_CONFIG = EngineConfig()

# Maps camelCase Prisma columns onto the snake_case dataclass fields.
COLUMN_TO_FIELD = {
    "id": "id",
    "name": "name",
    "loseAdjustmentKcal": "lose_adjustment_kcal",
    "gainAdjustmentKcal": "gain_adjustment_kcal",
    "maintainBandKg": "maintain_band_kg",
    "sedentaryMultiplier": "sedentary_multiplier",
    "lightMultiplier": "light_multiplier",
    "moderateMultiplier": "moderate_multiplier",
    "veryMultiplier": "very_multiplier",
    "extremeMultiplier": "extreme_multiplier",
    "proteinPerKg": "protein_per_kg",
    "fatPct": "fat_pct",
    "fiberGPer1000Kcal": "fiber_g_per_1000_kcal",
    "floorMaleKcal": "floor_male_kcal",
    "floorFemaleKcal": "floor_female_kcal",
    "floorUnspecifiedKcal": "floor_unspecified_kcal",
    "targetRoundingKcal": "target_rounding_kcal",
    "kcalPerKgBodyMass": "kcal_per_kg_body_mass",
}


def config_from_row(row: Mapping[str, Any]) -> EngineConfig:
    """Build a config from an `EngineConfig` DB row, ignoring unknown columns and
    falling back to the compiled default for anything null."""
    overrides = {}
    for column, field in COLUMN_TO_FIELD.items():
        value = row.get(column) if isinstance(row, Mapping) else getattr(row, column, None)
        if value is not None:
            overrides[field] = value
    return replace(DEFAULT_CONFIG, **overrides)
