"""Calorie & macro engine — PRD §6.

Pure and side-effect free: no DB, no Django, no I/O. Everything it needs arrives
through `PlanInput` and `EngineConfig`, which keeps it trivially testable and
lets it be lifted into a standalone service without touching callers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .config import DEFAULT_CONFIG, EngineConfig
from .enums import ActivityLevel, Goal, SexAtBirth
from .rounding import round_half_up, round_int, round_to_nearest

# Mifflin-St Jeor sex constants (§6.1). -78 is the defensive fallback only.
_BMR_CONSTANT = {
    SexAtBirth.MALE: 5.0,
    SexAtBirth.FEMALE: -161.0,
    SexAtBirth.UNSPECIFIED: -78.0,
}

KCAL_PER_G_PROTEIN = 4
KCAL_PER_G_CARB = 4
KCAL_PER_G_FAT = 9

# Independent rounding of three macro grams can leave the energy sum a couple of
# kcal off the target. Anything beyond this is a bug, not rounding (§6.3).
RECONCILE_TOLERANCE_KCAL = 4

# Floor for the carbohydrate remainder. A very heavy user on a clamped target can
# have protein + fat alone exceed the target; rather than emit negative carbs we
# rebalance (see `_split_macros`). Not reachable from realistic onboarding input.
MIN_CARBS_G = 0


class EngineError(Exception):
    """Raised only for inputs the engine cannot compute at all."""


class PlanReconciliationError(EngineError):
    """Macros failed to sum back to the calorie target — indicates a code defect."""


@dataclass(frozen=True)
class PlanInput:
    sex_at_birth: SexAtBirth
    age: int
    weight_kg: float
    height_cm: float
    goal: Goal
    activity_level: ActivityLevel
    target_weight_kg: Optional[float] = None


@dataclass(frozen=True)
class Macros:
    protein_g: int
    carbs_g: int
    fat_g: int
    fiber_g: int  # informational only; never counted toward energy (§5.5)

    @property
    def protein_kcal(self) -> int:
        return self.protein_g * KCAL_PER_G_PROTEIN

    @property
    def carbs_kcal(self) -> int:
        return self.carbs_g * KCAL_PER_G_CARB

    @property
    def fat_kcal(self) -> int:
        return self.fat_g * KCAL_PER_G_FAT

    @property
    def energy_kcal(self) -> int:
        return self.protein_kcal + self.carbs_kcal + self.fat_kcal


@dataclass(frozen=True)
class PlanResult:
    calories_kcal: int
    macros: Macros
    bmr: float
    tdee: float
    clamped: bool
    is_estimate: bool
    # The adjustment the rationale is built from. Equals the configured goal
    # adjustment normally; recomputed from the clamped target when clamped, so we
    # never advertise a deficit larger than the number supports (§6.2).
    adjustment_kcal: int
    requested_adjustment_kcal: int
    weekly_change_kg: float
    safety_floor_kcal: int
    config: EngineConfig = DEFAULT_CONFIG
    notes: List[str] = field(default_factory=list)

    def to_response(self) -> Dict[str, Any]:
        """PRD §8 response shape, plus the fields the plan screen needs to render
        its rationale and the clamped-state message."""
        return {
            "calories": self.calories_kcal,
            "macros": {
                "proteinG": self.macros.protein_g,
                "carbsG": self.macros.carbs_g,
                "fatG": self.macros.fat_g,
                "fiberG": self.macros.fiber_g,
            },
            "macroEnergyKcal": {
                "protein": self.macros.protein_kcal,
                "carbs": self.macros.carbs_kcal,
                "fat": self.macros.fat_kcal,
                # Fiber is deliberately absent: it is a subset of carbohydrate
                # grams and must not appear in any energy ring (§5.5).
                "total": self.macros.energy_kcal,
            },
            "rationale": {
                "adjustmentKcal": self.adjustment_kcal,
                "weeklyChangeKg": self.weekly_change_kg,
                "clamped": self.clamped,
                "safetyFloorKcal": self.safety_floor_kcal,
                "requestedAdjustmentKcal": self.requested_adjustment_kcal,
            },
            "isEstimate": self.is_estimate,
            "bmr": self.bmr,
            "tdee": self.tdee,
        }


def calculate_bmr(inp: PlanInput) -> float:
    """Mifflin-St Jeor (§6.1)."""
    return (
        10.0 * inp.weight_kg
        + 6.25 * inp.height_cm
        - 5.0 * inp.age
        + _BMR_CONSTANT[inp.sex_at_birth]
    )


def _split_macros(target_kcal: int, weight_kg: float, config: EngineConfig) -> Macros:
    """Derive macros from the calorie target so they always self-reconcile (§6.3).

    Protein is anchored to bodyweight, fat to a share of calories, and carbs take
    whatever energy is left. Fiber is derived from calories and stays out of the
    energy budget entirely.
    """
    protein_g = round_int(config.protein_per_kg * weight_kg)
    fat_g = round_int((config.fat_pct * target_kcal) / KCAL_PER_G_FAT)
    remainder = target_kcal - protein_g * KCAL_PER_G_PROTEIN - fat_g * KCAL_PER_G_FAT
    carbs_g = round_int(remainder / KCAL_PER_G_CARB)

    if carbs_g < MIN_CARBS_G:
        # Protein + fat alone overshoot the target. Hold fat at its configured
        # share, pin carbs at the minimum, and give protein the rest.
        carbs_g = MIN_CARBS_G
        protein_budget = (
            target_kcal - fat_g * KCAL_PER_G_FAT - carbs_g * KCAL_PER_G_CARB
        )
        protein_g = max(0, round_int(protein_budget / KCAL_PER_G_PROTEIN))

    fiber_g = round_int(config.fiber_g_per_1000_kcal * target_kcal / 1000.0)
    return Macros(protein_g=protein_g, carbs_g=carbs_g, fat_g=fat_g, fiber_g=fiber_g)


def calculate_plan(inp: PlanInput, config: EngineConfig = DEFAULT_CONFIG) -> PlanResult:
    """Full BMR -> TDEE -> target -> macros pipeline (§6.1-§6.3)."""
    notes: List[str] = []

    sex = inp.sex_at_birth
    is_estimate = sex is SexAtBirth.UNSPECIFIED
    if is_estimate:
        # §6.4 — unreachable from onboarding v7; kept so the engine never throws
        # on imported or malformed records.
        notes.append("unspecified_body_basis_fallback")

    bmr = calculate_bmr(inp)
    tdee = bmr * config.multiplier_for(inp.activity_level)

    requested_adjustment = config.adjustment_for(inp.goal)
    raw_target = tdee + requested_adjustment

    floor = config.floor_for(sex)
    clamped = raw_target < floor
    if clamped:
        notes.append("clamped_to_safety_floor")

    target_kcal = round_to_nearest(max(raw_target, float(floor)), config.target_rounding_kcal)

    if clamped:
        # Rebuild the rationale from what the clamped number actually delivers.
        adjustment_kcal = round_int(target_kcal - tdee)
    else:
        # The goal adjustment is the single source of truth for the copy (§5.3);
        # target rounding must not make the plan screen read "-396 kcal".
        adjustment_kcal = requested_adjustment

    weekly_change_kg = round_half_up(
        adjustment_kcal * 7.0 / config.kcal_per_kg_body_mass, 1
    )

    macros = _split_macros(target_kcal, inp.weight_kg, config)

    drift = abs(macros.energy_kcal - target_kcal)
    if drift > RECONCILE_TOLERANCE_KCAL:
        raise PlanReconciliationError(
            "macros do not reconcile with the calorie target: "
            "{}+{}+{} = {} kcal vs target {} kcal".format(
                macros.protein_kcal,
                macros.carbs_kcal,
                macros.fat_kcal,
                macros.energy_kcal,
                target_kcal,
            )
        )

    return PlanResult(
        calories_kcal=target_kcal,
        macros=macros,
        bmr=round_half_up(bmr, 1),
        tdee=round_half_up(tdee, 1),
        clamped=clamped,
        is_estimate=is_estimate,
        adjustment_kcal=adjustment_kcal,
        requested_adjustment_kcal=requested_adjustment,
        weekly_change_kg=weekly_change_kg,
        safety_floor_kcal=floor,
        config=config,
        notes=notes,
    )
