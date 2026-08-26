"""Onboarding use cases - PRD §6.2, §8.

The point of this file is the compute/resume symmetry: POST /onboarding/plan and
GET /onboarding/plan must describe the same plan the same way. They drifted once,
and a clamped user lost the explanation for their target on every app restart.

Prisma is never reached; `onboarding.repository` is the seam (see tests/settings.py).
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from engine import DEFAULT_CONFIG
from onboarding import repository, services
from onboarding.serializers import PlanRationaleSerializer

# The §6.2 fixture from test_engine.py: TDEE 1,111.8, a -400 deficit lands at
# 711.8, and the female floor pulls the target back up to 1,200.
CLAMPED = {
    "sexAtBirth": "FEMALE",
    "age": 60,
    "weightKg": 45.0,
    "heightCm": 150.0,
    "targetWeightKg": None,
    "goal": "LOSE",
    "activityLevel": "SEDENTARY",
}
UNCLAMPED = {
    "sexAtBirth": "MALE",
    "age": 30,
    "weightKg": 90.0,
    "heightCm": 180.0,
    "targetWeightKg": 82.0,
    "goal": "LOSE",
    "activityLevel": "MODERATE",
}


def make_profile(fields, plan=None):
    return SimpleNamespace(id="profile-1", **fields, plan=plan)


def stored_plan_from(response, **overrides):
    """A Plan row shaped like what `upsert_plan` would have written for a POST
    response, so the resume path reads back exactly what compute persisted."""
    rationale = response["rationale"]
    row = {
        "caloriesKcal": response["calories"],
        "proteinG": response["macros"]["proteinG"],
        "carbsG": response["macros"]["carbsG"],
        "fatG": response["macros"]["fatG"],
        "fiberG": response["macros"]["fiberG"],
        "bmr": response["bmr"],
        "tdee": response["tdee"],
        "clamped": rationale["clamped"],
        "isEstimate": response["isEstimate"],
        "adjustmentKcal": rationale["adjustmentKcal"],
        "weeklyChangeKg": rationale["weeklyChangeKg"],
        "safetyFloorKcal": rationale["safetyFloorKcal"],
        "requestedAdjustmentKcal": rationale["requestedAdjustmentKcal"],
        "computedAt": datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc),
    }
    row.update(overrides)
    return SimpleNamespace(id="plan-1", **row)


@pytest.fixture
def seam(monkeypatch):
    """Swap the repository for in-memory state and capture what gets persisted."""
    state = SimpleNamespace(profile=None, written=None)

    monkeypatch.setattr(repository, "get_profile", lambda user_id: state.profile)
    monkeypatch.setattr(
        repository, "get_active_engine_config", lambda **kw: DEFAULT_CONFIG
    )

    def upsert_plan(profile_id, result):
        state.written = result
        return result

    monkeypatch.setattr(repository, "upsert_plan", upsert_plan)
    return state


def codes(payload):
    return [a["code"] for a in payload["advisories"]]


def advisory(payload, code):
    return next(a for a in payload["advisories"] if a["code"] == code)


def test_a_clamped_plan_keeps_its_explanation_when_resumed(seam):
    """The regression this file exists for: the §6.2 message survives a restart."""
    seam.profile = make_profile(CLAMPED)
    computed = services.generate_plan("user-1")

    seam.profile = make_profile(CLAMPED, plan=stored_plan_from(computed))
    resumed = services.fetch_plan("user-1")

    assert "calories_clamped_to_floor" in codes(computed)
    assert "calories_clamped_to_floor" in codes(resumed)
    assert advisory(computed, "calories_clamped_to_floor") == advisory(
        resumed, "calories_clamped_to_floor"
    )
    assert "1200 kcal" in advisory(resumed, "calories_clamped_to_floor")["message"]


def test_compute_and_resume_describe_the_plan_identically(seam):
    seam.profile = make_profile(CLAMPED)
    computed = services.generate_plan("user-1")

    seam.profile = make_profile(CLAMPED, plan=stored_plan_from(computed))
    resumed = services.fetch_plan("user-1")

    assert resumed["rationale"] == computed["rationale"]
    assert resumed["advisories"] == computed["advisories"]
    # GET adds computedAt and is otherwise the POST payload.
    assert set(resumed) - set(computed) == {"computedAt"}
    assert not set(computed) - set(resumed)


def test_the_stored_rationale_matches_the_declared_schema(seam):
    seam.profile = make_profile(CLAMPED)
    computed = services.generate_plan("user-1")
    seam.profile = make_profile(CLAMPED, plan=stored_plan_from(computed))

    resumed = services.fetch_plan("user-1")
    assert set(resumed["rationale"]) == set(PlanRationaleSerializer().fields)


def test_the_clamp_fields_are_persisted(seam):
    """`upsert_plan` must carry the two numbers the resume path needs."""
    seam.profile = make_profile(CLAMPED)
    services.generate_plan("user-1")

    assert seam.written.clamped is True
    assert seam.written.safety_floor_kcal == 1200
    assert seam.written.requested_adjustment_kcal == -400
    # The effective adjustment is rebuilt from the clamped target, so it is not
    # the -400 that was asked for.
    assert seam.written.adjustment_kcal != seam.written.requested_adjustment_kcal


def test_an_unclamped_plan_never_carries_the_floor_message(seam):
    seam.profile = make_profile(UNCLAMPED)
    computed = services.generate_plan("user-1")

    seam.profile = make_profile(UNCLAMPED, plan=stored_plan_from(computed))
    resumed = services.fetch_plan("user-1")

    assert computed["rationale"]["clamped"] is False
    assert "calories_clamped_to_floor" not in codes(computed)
    assert "calories_clamped_to_floor" not in codes(resumed)
    assert resumed["rationale"] == computed["rationale"]


def test_a_row_predating_the_columns_is_healed_on_read(seam):
    """Until `backfill_plan_rationale` runs, GET must still answer in full."""
    seam.profile = make_profile(CLAMPED)
    computed = services.generate_plan("user-1")
    legacy = stored_plan_from(
        computed, safetyFloorKcal=None, requestedAdjustmentKcal=None
    )

    seam.profile = make_profile(CLAMPED, plan=legacy)
    resumed = services.fetch_plan("user-1")

    assert resumed["rationale"] == computed["rationale"]
    assert "calories_clamped_to_floor" in codes(resumed)


def test_fetch_plan_returns_none_before_a_plan_exists(seam):
    seam.profile = make_profile(CLAMPED)
    assert services.fetch_plan("user-1") is None
