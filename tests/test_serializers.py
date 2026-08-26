"""Input validation - PRD §5.2 and §9."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from rest_framework.exceptions import ValidationError

from common.exceptions import AgeBelowMinimumError
from onboarding.serializers import (
    PlanRationaleSerializer,
    ProfileUpsertSerializer,
    serialize_stored_plan,
)

VALID = {
    "sexAtBirth": "MALE",
    "age": 30,
    "weightKg": 90.0,
    "heightCm": 180.0,
    "targetWeightKg": 82.0,
    "goal": "LOSE",
    "activityLevel": "SEDENTARY",
}


def validated(**overrides):
    payload = dict(VALID)
    payload.update(overrides)
    serializer = ProfileUpsertSerializer(data=payload)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


def test_valid_payload_defaults_to_metric():
    data = validated()
    assert data["preferredUnits"] == "METRIC"
    assert data["age"] == 30


def test_body_basis_is_required():
    payload = dict(VALID)
    payload.pop("sexAtBirth")
    serializer = ProfileUpsertSerializer(data=payload)
    assert not serializer.is_valid()
    assert "sexAtBirth" in serializer.errors


def test_unspecified_body_basis_is_rejected_over_the_api():
    """v7 makes the field a required binary choice; UNSPECIFIED survives only as
    a defensive engine value (§6.4) and must never arrive from onboarding."""
    serializer = ProfileUpsertSerializer(data=dict(VALID, sexAtBirth="UNSPECIFIED"))
    assert not serializer.is_valid()
    assert "sexAtBirth" in serializer.errors


def test_age_below_the_minimum_is_blocked_with_a_dedicated_code():
    """§9 - block, don't warn, and give the client its own code to branch on."""
    with pytest.raises(AgeBelowMinimumError) as exc:
        validated(age=17)

    assert exc.value.code == "age_below_minimum"
    assert exc.value.status_code == 422
    assert exc.value.details["minimumAge"] == 18


def test_age_can_be_derived_from_a_real_date_of_birth():
    """§9 asks for a real date entry rather than a Y/N tickbox."""
    dob = date.today() - timedelta(days=365 * 30 + 8)
    data = validated(dateOfBirth=dob.isoformat())
    assert data["age"] == 30
    assert "dateOfBirth" not in data  # derived, not stored


def test_date_of_birth_wins_over_a_supplied_age():
    dob = date.today() - timedelta(days=365 * 45 + 12)
    assert validated(age=22, dateOfBirth=dob.isoformat())["age"] == 45


def test_a_just_under_18_date_of_birth_is_blocked():
    dob = date.today() - timedelta(days=365 * 17)
    with pytest.raises(AgeBelowMinimumError):
        validated(dateOfBirth=dob.isoformat())


def test_either_age_or_date_of_birth_must_be_present():
    payload = dict(VALID)
    payload.pop("age")
    serializer = ProfileUpsertSerializer(data=payload)
    with pytest.raises(ValidationError):
        serializer.is_valid(raise_exception=True)


@pytest.mark.parametrize("weight", [5.0, 900.0])
def test_weights_outside_physiological_bounds_are_rejected(weight):
    serializer = ProfileUpsertSerializer(data=dict(VALID, weightKg=weight))
    assert not serializer.is_valid()
    assert "weightKg" in serializer.errors


@pytest.mark.parametrize("height", [30.0, 300.0])
def test_heights_outside_physiological_bounds_are_rejected(height):
    serializer = ProfileUpsertSerializer(data=dict(VALID, heightCm=height))
    assert not serializer.is_valid()
    assert "heightCm" in serializer.errors


def test_target_weight_is_optional():
    payload = dict(VALID)
    payload.pop("targetWeightKg")
    serializer = ProfileUpsertSerializer(data=payload)
    assert serializer.is_valid(), serializer.errors


def test_target_weight_may_be_null():
    assert validated(targetWeightKg=None)["targetWeightKg"] is None


def test_unusual_but_plausible_values_are_accepted():
    """Soft-range concerns come back as advisories, not rejections."""
    serializer = ProfileUpsertSerializer(data=dict(VALID, weightKg=260.0))
    assert serializer.is_valid(), serializer.errors


STORED_PLAN = SimpleNamespace(
    caloriesKcal=1200,
    proteinG=81,
    carbsG=113,
    fatG=33,
    fiberG=18,
    bmr=926.5,
    tdee=1111.8,
    clamped=True,
    isEstimate=False,
    adjustmentKcal=88,
    weeklyChangeKg=0.1,
    safetyFloorKcal=1200,
    requestedAdjustmentKcal=-400,
    computedAt=datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc),
)


def test_stored_plan_rationale_matches_the_declared_schema():
    """GET and POST share `PlanRationaleSerializer`; if a field is added to the
    schema without being read off the row, the resume path silently returns null."""
    rationale = serialize_stored_plan(STORED_PLAN)["rationale"]
    assert set(rationale) == set(PlanRationaleSerializer().fields)


def test_stored_plan_carries_the_clamp_fields():
    """§6.2 - the floor is what the clamped advisory interpolates."""
    rationale = serialize_stored_plan(STORED_PLAN)["rationale"]
    assert rationale["safetyFloorKcal"] == 1200
    assert rationale["requestedAdjustmentKcal"] == -400
    assert rationale["clamped"] is True
