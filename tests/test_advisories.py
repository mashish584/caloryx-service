"""Edge-case advisory tests - PRD §9."""
from __future__ import annotations

import json

import pytest

from engine import (
    AdvisoryCode,
    AdvisoryField,
    AdvisorySeverity,
    bmi,
    clamped_advisory,
    evaluate_profile,
)
from onboarding.serializers import (
    WEIGHT_KG_RANGE,
    AdvisoryCheckSerializer,
    AdvisoryPatchSerializer,
    AdvisorySerializer,
    ProfileUpsertSerializer,
)
from onboarding.services import check_advisories


def codes(advisories):
    return [a.code for a in advisories]


def test_a_target_weight_in_either_direction_is_unremarkable():
    """The goal is derived from the target, so no direction is a contradiction.

    This replaces four tests that asserted a goal could conflict with the target
    weight. `EngineConfig.goal_for` made the conflict unrepresentable.
    """
    assert evaluate_profile(weight_kg=80.0, height_cm=175.0, target_weight_kg=72.0) == []
    assert evaluate_profile(weight_kg=70.0, height_cm=175.0, target_weight_kg=80.0) == []
    assert evaluate_profile(weight_kg=70.0, height_cm=175.0, target_weight_kg=70.0) == []


def test_omitted_target_weight_is_allowed():
    """The upsert now requires a target, but stored rows written before that
    change have none - `evaluate_profile` still has to cope on the read path."""
    assert evaluate_profile(weight_kg=80.0, height_cm=175.0) == []


def test_target_weight_below_healthy_bmi_triggers_the_wellbeing_safeguard():
    advisories = evaluate_profile(
        weight_kg=60.0, height_cm=175.0, target_weight_kg=52.0
    )

    assert "target_weight_below_healthy_bmi" in codes(advisories)
    assert bmi(52.0, 175.0) < 18.5
    safeguard = next(a for a in advisories if a.code == "target_weight_below_healthy_bmi")
    # Supportive, and it points somewhere useful rather than just refusing.
    assert "doctor or dietitian" in safeguard.message


def test_implausible_but_accepted_values_only_warn():
    advisories = evaluate_profile(weight_kg=300.0, height_cm=125.0)

    assert codes(advisories) == [
        "weight_out_of_typical_range",
        "height_out_of_typical_range",
    ]
    assert all(a.severity == "warning" for a in advisories)


def test_clamped_advisory_names_the_floor():
    advisory = clamped_advisory(1200)
    assert advisory.code == "calories_clamped_to_floor"
    assert "1200" in advisory.message


def test_advisory_dict_drops_empty_fields():
    advisory = clamped_advisory(1500).to_dict()
    assert "options" not in advisory and "field" not in advisory


# -- the payload is typed, and the wire shape did not move -----------------

# Every advisory the producers can emit, reached through its triggering input.
ALL_ADVISORIES = (
    evaluate_profile(weight_kg=300.0, height_cm=125.0, target_weight_kg=40.0)
    # BMI 16.3 at the target, which is what trips the wellbeing safeguard.
    + evaluate_profile(weight_kg=70.0, height_cm=175.0, target_weight_kg=50.0)
    + [clamped_advisory(1200)]
)


def test_a_patch_can_only_name_a_field_the_request_accepts():
    """The client merges `patch` into the upsert body and resends it, so a key
    outside that body would produce a 400 the user cannot act on."""
    patch_fields = set(AdvisoryPatchSerializer().fields)
    upsert_fields = set(ProfileUpsertSerializer().fields)

    assert patch_fields <= upsert_fields, patch_fields - upsert_fields


def test_every_patch_actually_emitted_validates():
    """Guards any hand-written patch dict in engine/advisories.py.

    Vacuous today: the goal/target-weight conflict was the only advisory that
    ever carried options, and the derived goal removed it. Kept because the
    `options` contract is still declared, so the first advisory to use it again
    is checked from the moment it lands.
    """
    patches = [o["patch"] for a in ALL_ADVISORIES for o in a.options]

    for patch in patches:
        serializer = AdvisoryPatchSerializer(data=patch)
        assert serializer.is_valid(), (patch, serializer.errors)


def test_the_wire_shape_is_still_plain_strings():
    """`StrEnum` members must serialise as their values - the enums are a
    type-level change only, and any JSON difference would break clients."""
    payload = json.loads(json.dumps([a.to_dict() for a in ALL_ADVISORIES]))

    for item in payload:
        assert isinstance(item["code"], str)
        assert isinstance(item["severity"], str)
        assert isinstance(item.get("field", ""), str)


@pytest.mark.parametrize("advisory", ALL_ADVISORIES, ids=lambda a: str(a.code))
def test_every_advisory_matches_the_declared_schema(advisory):
    serializer = AdvisorySerializer(data=advisory.to_dict())
    assert serializer.is_valid(), serializer.errors


def test_every_declared_code_is_reachable():
    """A member nothing emits is a lie in the contract - the client would write
    a branch that never runs."""
    emitted = {a.code for a in ALL_ADVISORIES}
    assert emitted == set(AdvisoryCode), set(AdvisoryCode) - emitted


def test_every_emitted_value_is_a_declared_member():
    """The other direction: nothing escapes the enums."""
    for a in ALL_ADVISORIES:
        assert a.code in set(AdvisoryCode)
        assert a.severity in set(AdvisorySeverity)
        assert a.field is None or a.field in set(AdvisoryField)


def test_the_clamp_advisory_is_the_only_supportive_one():
    """§6.2 copy is reassurance, not a warning; the client styles on this."""
    by_severity = {a.code: a.severity for a in ALL_ADVISORIES}
    assert by_severity[AdvisoryCode.CALORIES_CLAMPED_TO_FLOOR] == AdvisorySeverity.INFO
    assert all(
        s == AdvisorySeverity.WARNING
        for c, s in by_severity.items()
        if c != AdvisoryCode.CALORIES_CLAMPED_TO_FLOOR
    )


# -- the preview endpoint (POST /onboarding/advisories) ---------------------


def test_the_preview_returns_what_the_save_would_have_returned():
    """The drift guard. Two paths reach the user with a hint about the same
    numbers; if they ever disagree, the preview is worse than useless."""
    measurements = {"weightKg": 300.0, "heightCm": 125.0, "targetWeightKg": 40.0}

    assert check_advisories(measurements)["advisories"] == [
        a.to_dict()
        for a in evaluate_profile(
            weight_kg=300.0, height_cm=125.0, target_weight_kg=40.0
        )
    ]


def test_values_that_look_fine_come_back_with_an_empty_list():
    """An empty list is the "everything looks good" answer - not an absent key,
    and not a null."""
    payload = check_advisories(
        {"weightKg": 80.0, "heightCm": 175.0, "targetWeightKg": 72.0}
    )

    assert payload == {"advisories": []}


def test_the_preview_never_emits_the_plan_time_advisory():
    """The clamp is an outcome of computing a plan (§6.2). Nothing about a set
    of measurements alone can produce it."""
    payload = check_advisories(
        {"weightKg": 300.0, "heightCm": 125.0, "targetWeightKg": 40.0}
    )

    assert AdvisoryCode.CALORIES_CLAMPED_TO_FLOOR not in {
        a["code"] for a in payload["advisories"]
    }


def test_the_preview_only_asks_for_fields_the_upsert_accepts():
    """Same reasoning as the patch above: the client collects these values for
    the save, so the dry run must not invent a second vocabulary for them."""
    check_fields = set(AdvisoryCheckSerializer().fields)
    upsert_fields = set(ProfileUpsertSerializer().fields)

    assert check_fields <= upsert_fields, check_fields - upsert_fields


def test_the_preview_asks_for_every_field_an_advisory_can_name():
    """Otherwise an advisory could point at an input the caller never sent, and
    the client would have nothing to attach the message to."""
    check_fields = set(AdvisoryCheckSerializer().fields)

    assert {f.value for f in AdvisoryField} <= check_fields


def test_the_preview_rejects_what_the_save_would_reject():
    """§9: hard caps reject on both paths. A body that passes the dry run has to
    be one the upsert will accept, or the preview is lying."""
    serializer = AdvisoryCheckSerializer(
        data={
            "weightKg": WEIGHT_KG_RANGE[1] + 1,
            "heightCm": 175.0,
            "targetWeightKg": 72.0,
        }
    )

    assert not serializer.is_valid()
    assert "weightKg" in serializer.errors


def test_the_preview_accepts_what_the_save_would_only_warn_about():
    """The other half of §9: inside the hard cap but outside the soft range is a
    200 with an advisory, never a 400."""
    serializer = AdvisoryCheckSerializer(
        data={"weightKg": 300.0, "heightCm": 175.0, "targetWeightKg": 90.0}
    )

    assert serializer.is_valid(), serializer.errors
    advisories = check_advisories(serializer.validated_data)["advisories"]
    assert [a["code"] for a in advisories] == ["weight_out_of_typical_range"]


def test_all_three_measurements_are_required():
    """The target weight is load-bearing here, not decorative: it is the only
    input the wellbeing safeguard reads."""
    serializer = AdvisoryCheckSerializer(data={"weightKg": 80.0})

    assert not serializer.is_valid()
    assert set(serializer.errors) == {"heightCm", "targetWeightKg"}
