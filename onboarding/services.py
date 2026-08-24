"""Onboarding use cases.

Views stay thin: they parse, call one of these, and render. Anything that
combines the engine with persistence lives here.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from common.exceptions import ProfileRequiredError
from engine import (
    Advisory,
    Goal,
    PlanInput,
    PlanResult,
    SexAtBirth,
    calculate_plan,
    clamped_advisory,
    evaluate_profile,
)
from engine.enums import ActivityLevel

from . import repository
from .serializers import serialize_profile, serialize_stored_plan

logger = logging.getLogger(__name__)


def _to_plan_input(profile: Any) -> PlanInput:
    return PlanInput(
        sex_at_birth=SexAtBirth(profile.sexAtBirth),
        age=profile.age,
        weight_kg=profile.weightKg,
        height_cm=profile.heightCm,
        goal=Goal(profile.goal),
        activity_level=ActivityLevel(profile.activityLevel),
        target_weight_kg=profile.targetWeightKg,
    )


def profile_advisories(profile: Any) -> List[Advisory]:
    return evaluate_profile(
        goal=Goal(profile.goal),
        weight_kg=profile.weightKg,
        height_cm=profile.heightCm,
        target_weight_kg=profile.targetWeightKg,
    )


def save_profile(user_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """Upsert the step 1-3 inputs and hand back any inline hints."""
    payload = dict(data)
    payload.setdefault("targetWeightKg", None)
    profile = repository.upsert_profile(user_id, payload)
    advisories = profile_advisories(profile)
    return {
        "profile": serialize_profile(profile),
        "advisories": [a.to_dict() for a in advisories],
    }


def generate_plan(user_id: str) -> Dict[str, Any]:
    """Compute the authoritative plan and persist it (PRD §5.5, §8).

    The client may have rendered an optimistic preview; this is the number it
    reconciles to.
    """
    profile = repository.get_profile(user_id)
    if profile is None:
        raise ProfileRequiredError()

    config = repository.get_active_engine_config()
    result: PlanResult = calculate_plan(_to_plan_input(profile), config)
    repository.upsert_plan(profile.id, result)

    advisories = profile_advisories(profile)
    if result.clamped:
        advisories.insert(0, clamped_advisory(result.safety_floor_kcal))

    logger.info(
        "plan generated user=%s calories=%s goal=%s activity=%s clamped=%s estimate=%s",
        user_id,
        result.calories_kcal,
        profile.goal,
        profile.activityLevel,
        result.clamped,
        result.is_estimate,
    )

    response = result.to_response()
    response["advisories"] = [a.to_dict() for a in advisories]
    return response


def fetch_plan(user_id: str) -> Optional[Dict[str, Any]]:
    profile = repository.get_profile(user_id)
    if profile is None:
        raise ProfileRequiredError()
    plan = getattr(profile, "plan", None)
    if plan is None:
        return None

    payload = serialize_stored_plan(plan)
    payload["advisories"] = [a.to_dict() for a in profile_advisories(profile)]
    return payload


def complete_onboarding(user_id: str) -> Dict[str, Any]:
    """Mark `onboardedAt` and finalise (PRD §8).

    Requires a plan: "You're all set" should never be reachable without one, and
    a missing plan here means the client skipped a step.
    """
    profile = repository.get_profile(user_id)
    if profile is None:
        raise ProfileRequiredError()
    if getattr(profile, "plan", None) is None:
        raise ProfileRequiredError(
            "Generate a plan before completing onboarding.", code="plan_required"
        )

    updated = repository.mark_onboarded(user_id)
    logger.info("onboarding completed user=%s", user_id)
    return {"profile": serialize_profile(updated)}
