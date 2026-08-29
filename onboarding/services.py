"""Onboarding use cases.

Views stay thin: they parse, call one of these, and render. Anything that
combines the engine with persistence lives here.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from common.db import from_prisma_date, to_prisma_date
from common.exceptions import ProfileRequiredError
from engine import (
    Advisory,
    EngineConfig,
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
from .serializers import (
    DEFAULT_PREFERRED_UNITS,
    age_from_dob,
    serialize_profile,
    serialize_stored_plan,
)

logger = logging.getLogger(__name__)


def _profile_age(profile: Any) -> int:
    """Age at this moment, derived rather than remembered (§9).

    Falls back to the legacy `age` column for rows written before `dateOfBirth`
    existed; `backfill_date_of_birth` clears those, and the fallback goes with
    the column.
    """
    # Normalised because the source varies: Prisma hands back a `datetime` even
    # for the `@db.Date` column, the repository seams in the tests hand back a
    # `date`. `age_from_dob` happens to work on both - `datetime` subclasses
    # `date` - but the read side has drifted on exactly that coincidence before.
    dob = from_prisma_date(getattr(profile, "dateOfBirth", None))
    return age_from_dob(dob) if dob is not None else profile.age


def _to_plan_input(profile: Any) -> PlanInput:
    return PlanInput(
        sex_at_birth=SexAtBirth(profile.sexAtBirth),
        age=_profile_age(profile),
        weight_kg=profile.weightKg,
        height_cm=profile.heightCm,
        goal=Goal(profile.goal),
        activity_level=ActivityLevel(profile.activityLevel),
        target_weight_kg=profile.targetWeightKg,
    )


def profile_advisories(profile: Any) -> List[Advisory]:
    return evaluate_profile(
        weight_kg=profile.weightKg,
        height_cm=profile.heightCm,
        target_weight_kg=profile.targetWeightKg,
    )


def plan_advisories(
    profile: Any, *, clamped: bool, safety_floor_kcal: int
) -> List[Advisory]:
    """Profile hints plus the §6.2 clamp explanation.

    Computing a plan and resuming one both go through here. They used to assemble
    this list separately, and the resume path forgot the clamp advisory - so a
    user held at the floor saw the explanation once and never again.
    """
    advisories = profile_advisories(profile)
    if clamped:
        advisories.insert(0, clamped_advisory(safety_floor_kcal))
    return advisories


def derive_stored_rationale(profile: Any, config: EngineConfig) -> Tuple[int, int]:
    """Safety floor and requested adjustment for a Plan row that predates those
    columns. Shared with the `backfill_plan_rationale` command so the healed value
    and the backfilled value are always derived the same way."""
    return (
        config.floor_for(SexAtBirth(profile.sexAtBirth)),
        config.adjustment_for(Goal(profile.goal)),
    )


def save_profile(user_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """Upsert the step 1-3 inputs and hand back any inline hints."""
    payload = dict(data)
    # `goal` left the request contract: the target weight already says which way
    # the user is going, so the goal screen was a step for an answer we had.
    # Derived here rather than in the serializer because the MAINTAIN band is
    # server-tunable config and the serializers stay clear of the repository.
    # `get_active_engine_config` is cached and falls back to the compiled
    # defaults, so this cannot cost a profile save.
    config = repository.get_active_engine_config()
    payload["goal"] = config.goal_for(
        payload["weightKg"], payload["targetWeightKg"]
    ).value
    # `preferredUnits` is nested on the wire but two columns in the database, and
    # `upsert_profile` spreads this payload straight into Prisma. Flatten here so
    # the repository stays a blind pass-through.
    units = payload.pop("preferredUnits", None) or DEFAULT_PREFERRED_UNITS
    payload["weightUnit"] = units["weight"]
    payload["heightUnit"] = units["height"]
    # DRF hands back a `date`, which Prisma cannot serialize at all - it used to
    # crash the whole endpoint with a 500. See `to_prisma_date`.
    payload["dateOfBirth"] = to_prisma_date(payload.get("dateOfBirth"))
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
    plan = repository.upsert_plan(profile.id, result)

    advisories = plan_advisories(
        profile, clamped=result.clamped, safety_floor_kcal=result.safety_floor_kcal
    )

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
    # Taken from the persisted row rather than a fresh _now(): a client caching
    # this payload ages it against the same instant the database holds, and the
    # POST and GET shapes stay identical. The engine has no clock of its own.
    response["computedAt"] = plan.computedAt.isoformat()
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
    rationale = payload["rationale"]
    if rationale["safetyFloorKcal"] is None or rationale["requestedAdjustmentKcal"] is None:
        # Written before the columns existed. `backfill_plan_rationale` is what
        # actually clears these; healing here keeps the response well-formed in
        # the meantime, at the cost of using today's config rather than the one
        # that produced the plan.
        floor, requested = derive_stored_rationale(
            profile, repository.get_active_engine_config()
        )
        if rationale["safetyFloorKcal"] is None:
            rationale["safetyFloorKcal"] = floor
        if rationale["requestedAdjustmentKcal"] is None:
            rationale["requestedAdjustmentKcal"] = requested

    payload["advisories"] = [
        a.to_dict()
        for a in plan_advisories(
            profile,
            clamped=plan.clamped,
            safety_floor_kcal=rationale["safetyFloorKcal"],
        )
    ]
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
