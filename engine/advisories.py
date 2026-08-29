"""Non-blocking advisories — PRD §9.

Onboarding speed and completion are core metrics, so nothing here blocks a
request. Each advisory is a structured object the client renders as an inline
hint. An advisory may also carry one-tap `options` rather than have the server
silently auto-correct anything; none does today - the only advisory that ever
did was the goal/target-weight conflict, which the derived goal made impossible.
"""
from __future__ import annotations

import dataclasses
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

from .enums import AdvisoryCode, AdvisoryField, AdvisorySeverity

# Physiological soft bounds. Outside these we warn but still accept; the hard
# caps that *do* reject live in the serializers (onboarding/serializers.py).
SOFT_WEIGHT_KG = (35.0, 250.0)
SOFT_HEIGHT_CM = (130.0, 220.0)

# Below this BMI a goal weight triggers the wellbeing safeguard (§9).
UNDERWEIGHT_BMI = 18.5


@dataclass(frozen=True)
class Advisory:
    code: AdvisoryCode
    message: str
    field: Optional[AdvisoryField] = None
    severity: AdvisorySeverity = AdvisorySeverity.INFO
    # Present when the PRD asks for a one-tap choice; each option is
    # {"id": ..., "label": ..., "patch": {...}} where `patch` is the request body
    # the client would resend if the user picks it.
    options: List[Dict[str, Any]] = dataclasses.field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, [])}


def bmi(weight_kg: float, height_cm: float) -> float:
    height_m = height_cm / 100.0
    return weight_kg / (height_m * height_m)


def _wellbeing(
    height_cm: float, target_weight_kg: Optional[float]
) -> List[Advisory]:
    """Wellbeing safeguard (§9). The safety floor protects the daily number; this
    protects the goal itself."""
    if target_weight_kg is None:
        return []
    if bmi(target_weight_kg, height_cm) >= UNDERWEIGHT_BMI:
        return []
    return [
        Advisory(
            code=AdvisoryCode.TARGET_WEIGHT_BELOW_HEALTHY_BMI,
            field=AdvisoryField.TARGET_WEIGHT_KG,
            severity=AdvisorySeverity.WARNING,
            message=(
                "That target weight falls below the healthy range for your height. "
                "We'd rather not build an aggressive plan around it - it's worth "
                "talking to a doctor or dietitian about a goal that fits you."
            ),
        )
    ]


def _plausibility(weight_kg: float, height_cm: float) -> List[Advisory]:
    out: List[Advisory] = []
    low, high = SOFT_WEIGHT_KG
    if not low <= weight_kg <= high:
        out.append(
            Advisory(
                code=AdvisoryCode.WEIGHT_OUT_OF_TYPICAL_RANGE,
                field=AdvisoryField.WEIGHT_KG,
                severity=AdvisorySeverity.WARNING,
                message="Double-check your weight - that's outside the usual range.",
            )
        )
    low, high = SOFT_HEIGHT_CM
    if not low <= height_cm <= high:
        out.append(
            Advisory(
                code=AdvisoryCode.HEIGHT_OUT_OF_TYPICAL_RANGE,
                field=AdvisoryField.HEIGHT_CM,
                severity=AdvisorySeverity.WARNING,
                message="Double-check your height - that's outside the usual range.",
            )
        )
    return out


def evaluate_profile(
    *,
    weight_kg: float,
    height_cm: float,
    target_weight_kg: Optional[float] = None,
) -> List[Advisory]:
    """All profile-level advisories, in the order the client should surface them.

    There was a third advisory here once, reconciling a goal that contradicted
    the target weight. The goal is now derived from that same target
    (`EngineConfig.goal_for`), so the two can no longer disagree and the hint has
    nothing left to warn about.
    """
    advisories = _plausibility(weight_kg, height_cm)
    advisories += _wellbeing(height_cm, target_weight_kg)
    return advisories


def clamped_advisory(floor_kcal: int) -> Advisory:
    """Supportive message shown when the target was clamped up to the floor (§6.2)."""
    return Advisory(
        code=AdvisoryCode.CALORIES_CLAMPED_TO_FLOOR,
        severity=AdvisorySeverity.INFO,
        message=(
            "We've set your daily target to {} kcal - the lowest we'll recommend "
            "without medical supervision. Eating less than this makes it hard to "
            "get the nutrients you need.".format(floor_kcal)
        ),
    )
