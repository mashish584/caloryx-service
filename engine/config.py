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

    def to_public_dict(self) -> Dict[str, Any]:
        """Shape handed to the client so its optimistic preview (§5.5) uses the
        same constants the server will. Never include anything secret here."""
        return {
            "configName": self.name,
            "goalAdjustmentsKcal": {
                Goal.LOSE.value: self.lose_adjustment_kcal,
                Goal.MAINTAIN.value: 0,
                Goal.GAIN.value: self.gain_adjustment_kcal,
            },
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


DEFAULT_CONFIG = EngineConfig()

# Maps camelCase Prisma columns onto the snake_case dataclass fields.
COLUMN_TO_FIELD = {
    "id": "id",
    "name": "name",
    "loseAdjustmentKcal": "lose_adjustment_kcal",
    "gainAdjustmentKcal": "gain_adjustment_kcal",
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
