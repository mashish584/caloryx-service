"""Input validation - PRD §5.2 and §9."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from rest_framework.exceptions import ValidationError

from common.exceptions import AgeBelowMinimumError
from common.management.commands.backfill_date_of_birth import dob_from_age
from tests.support import dob_for_age, dob_str
from onboarding.serializers import (
    DEFAULT_PREFERRED_UNITS,
    age_from_dob,
    PlanRationaleSerializer,
    ProfileUpsertSerializer,
    serialize_profile,
    serialize_stored_plan,
)

VALID = {
    "sexAtBirth": "MALE",
    "dateOfBirth": dob_str(30),
    "weightKg": 90.0,
    "heightCm": 180.0,
    "targetWeightKg": 82.0,
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
    assert data["preferredUnits"] == DEFAULT_PREFERRED_UNITS == {
        "weight": "KG",
        "height": "CM",
    }
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
        validated(dateOfBirth=dob_str(17))

    assert exc.value.code == "age_below_minimum"
    assert exc.value.status_code == 422
    assert exc.value.details["minimumAge"] == 18
    assert exc.value.details["age"] == 17


def test_the_birth_date_is_kept_not_consumed():
    """It is the stored value now; age is derived from it on every read."""
    data = validated(dateOfBirth=dob_str(30))
    assert data["dateOfBirth"] == dob_for_age(30)


def test_a_birth_date_is_required():
    payload = dict(VALID)
    payload.pop("dateOfBirth")
    serializer = ProfileUpsertSerializer(data=payload)

    assert not serializer.is_valid()
    assert "dateOfBirth" in serializer.errors


def test_a_supplied_age_is_ignored():
    """`age` left the request contract; sending one must not resurrect it."""
    assert validated(age=99)["dateOfBirth"] == dob_for_age(30)


def test_a_future_birth_date_is_a_field_error_not_an_age_error():
    """It would otherwise derive a negative age and trip the under-18 gate,
    whose copy makes no sense for a date that has not happened."""
    serializer = ProfileUpsertSerializer(
        data=dict(VALID, dateOfBirth=(date.today() + timedelta(days=1)).isoformat())
    )

    assert not serializer.is_valid()
    assert "dateOfBirth" in serializer.errors


# -- age derivation (§9) ----------------------------------------------------


def test_the_exact_eighteenth_birthday_is_allowed():
    """The boundary the gate turns on - only expressible now a date is the
    input, where an integer age rounded it away."""
    assert validated(dateOfBirth=dob_str(18))


def test_one_day_short_of_eighteen_is_blocked():
    just_short = dob_for_age(18) + timedelta(days=1)
    with pytest.raises(AgeBelowMinimumError):
        validated(dateOfBirth=just_short.isoformat())


def test_a_birthday_later_this_year_has_not_happened_yet():
    """The tuple comparison in `age_from_dob` exists for exactly this."""
    today = date(2026, 6, 15)
    assert age_from_dob(date(1996, 12, 25), today=today) == 29
    assert age_from_dob(date(1996, 1, 5), today=today) == 30
    assert age_from_dob(date(1996, 6, 15), today=today) == 30  # birthday today


def test_a_leap_day_birth_date_derives_in_a_non_leap_year():
    assert age_from_dob(date(2000, 2, 29), today=date(2026, 2, 28)) == 25
    assert age_from_dob(date(2000, 2, 29), today=date(2026, 3, 1)) == 26


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


def test_target_weight_is_required():
    """It used to be optional. The goal is derived from it now, so a profile
    without one has no way to say which direction the user is going."""
    payload = dict(VALID)
    payload.pop("targetWeightKg")
    serializer = ProfileUpsertSerializer(data=payload)

    assert not serializer.is_valid()
    assert "targetWeightKg" in serializer.errors


def test_target_weight_may_not_be_null():
    serializer = ProfileUpsertSerializer(data=dict(VALID, targetWeightKg=None))

    assert not serializer.is_valid()
    assert "targetWeightKg" in serializer.errors


def test_a_supplied_goal_is_ignored():
    """`goal` left the request contract - the target weight determines it, and
    accepting both invited a profile that contradicted itself."""
    assert "goal" not in validated(goal="GAIN")


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


# -- display units (§5.2) --------------------------------------------------


def test_a_full_imperial_pair_round_trips():
    units = {"weight": "LB", "height": "FT_IN"}
    assert validated(preferredUnits=units)["preferredUnits"] == units


def test_a_mixed_pair_round_trips():
    """The case the old single METRIC/IMPERIAL flag could not represent: someone
    who thinks in kilograms but in feet and inches."""
    units = {"weight": "KG", "height": "FT_IN"}
    assert validated(preferredUnits=units)["preferredUnits"] == units


def test_the_other_mixed_pair_round_trips():
    units = {"weight": "LB", "height": "CM"}
    assert validated(preferredUnits=units)["preferredUnits"] == units


@pytest.mark.parametrize(
    "units",
    [
        {"weight": "STONE", "height": "CM"},
        {"weight": "KG", "height": "INCHES"},
        {"weight": "KG"},
        {"height": "CM"},
        {},
        "METRIC",  # the retired flat form
    ],
)
def test_malformed_unit_preferences_are_rejected(units):
    serializer = ProfileUpsertSerializer(data=dict(VALID, preferredUnits=units))
    assert not serializer.is_valid()
    assert "preferredUnits" in serializer.errors


def test_serialize_profile_emits_the_nested_pair():
    """Columns are flat; the wire shape is nested."""
    profile = SimpleNamespace(
        id="p1",
        sexAtBirth="FEMALE",
        dateOfBirth=date(1996, 3, 14),
        weightKg=68.0,
        heightCm=165.0,
        targetWeightKg=None,
        goal="LOSE",
        activityLevel="MODERATE",
        weightUnit="KG",
        heightUnit="FT_IN",
        onboardedAt=None,
        updatedAt=datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc),
    )
    assert serialize_profile(profile)["preferredUnits"] == {
        "weight": "KG",
        "height": "FT_IN",
    }


# -- backfill of pre-existing rows ------------------------------------------


def test_a_reconstructed_birth_date_derives_back_to_the_stored_age():
    declared_on = date(2026, 8, 27)
    assert age_from_dob(dob_from_age(30, declared_on), today=declared_on) == 30
    assert age_from_dob(dob_from_age(18, declared_on), today=declared_on) == 18


def test_a_leap_day_declaration_falls_back_to_the_28th():
    """29 Feb minus a non-leap number of years has no such date."""
    assert dob_from_age(25, date(2024, 2, 29)) == date(1999, 2, 28)
    assert dob_from_age(24, date(2024, 2, 29)) == date(2000, 2, 29)  # leap target
