"""Edge-case advisory tests - PRD §9."""
from __future__ import annotations

import json

import pytest

from engine import (
    AdvisoryCode,
    AdvisoryField,
    AdvisorySeverity,
    Goal,
    bmi,
    clamped_advisory,
    evaluate_profile,
)
from onboarding.serializers import (
    AdvisoryPatchSerializer,
    AdvisorySerializer,
    ProfileUpsertSerializer,
)


def codes(advisories):
    return [a.code for a in advisories]


def test_lose_goal_with_a_higher_target_weight_offers_a_one_tap_choice():
    advisories = evaluate_profile(
        goal=Goal.LOSE, weight_kg=70.0, height_cm=175.0, target_weight_kg=80.0
    )

    assert codes(advisories) == ["goal_target_weight_conflict"]
    conflict = advisories[0]
    # Never a silent auto-correct: the user picks.
    assert [o["id"] for o in conflict.options] == ["keep_goal", "switch_goal"]
    assert conflict.severity == "warning"


def test_gain_goal_with_a_lower_target_weight_conflicts_too():
    advisories = evaluate_profile(
        goal=Goal.GAIN, weight_kg=70.0, height_cm=175.0, target_weight_kg=65.0
    )
    assert codes(advisories) == ["goal_target_weight_conflict"]


def test_equal_target_weight_still_counts_as_a_conflict_for_lose():
    advisories = evaluate_profile(
        goal=Goal.LOSE, weight_kg=70.0, height_cm=175.0, target_weight_kg=70.0
    )
    assert "goal_target_weight_conflict" in codes(advisories)


def test_a_consistent_goal_produces_no_advisories():
    advisories = evaluate_profile(
        goal=Goal.LOSE, weight_kg=80.0, height_cm=175.0, target_weight_kg=72.0
    )
    assert advisories == []


def test_omitted_target_weight_is_allowed():
    """§9 - target weight omitted defaults to 'no target'; nothing is blocked."""
    assert evaluate_profile(goal=Goal.LOSE, weight_kg=80.0, height_cm=175.0) == []


def test_target_weight_below_healthy_bmi_triggers_the_wellbeing_safeguard():
    advisories = evaluate_profile(
        goal=Goal.LOSE, weight_kg=60.0, height_cm=175.0, target_weight_kg=52.0
    )

    assert "target_weight_below_healthy_bmi" in codes(advisories)
    assert bmi(52.0, 175.0) < 18.5
    safeguard = next(a for a in advisories if a.code == "target_weight_below_healthy_bmi")
    # Supportive, and it points somewhere useful rather than just refusing.
    assert "doctor or dietitian" in safeguard.message


def test_implausible_but_accepted_values_only_warn():
    advisories = evaluate_profile(goal=Goal.MAINTAIN, weight_kg=300.0, height_cm=125.0)

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
    evaluate_profile(goal=Goal.LOSE, weight_kg=70.0, height_cm=175.0, target_weight_kg=80.0)
    + evaluate_profile(goal=Goal.GAIN, weight_kg=80.0, height_cm=175.0, target_weight_kg=70.0)
    + evaluate_profile(goal=Goal.LOSE, weight_kg=300.0, height_cm=125.0, target_weight_kg=40.0)
    # BMI 16.3 at the target, which is what trips the wellbeing safeguard.
    + evaluate_profile(goal=Goal.LOSE, weight_kg=70.0, height_cm=175.0, target_weight_kg=50.0)
    + [clamped_advisory(1200)]
)


def test_a_patch_can_only_name_a_field_the_request_accepts():
    """The client merges `patch` into the upsert body and resends it, so a key
    outside that body would produce a 400 the user cannot act on."""
    patch_fields = set(AdvisoryPatchSerializer().fields)
    upsert_fields = set(ProfileUpsertSerializer().fields)

    assert patch_fields <= upsert_fields, patch_fields - upsert_fields


def test_every_patch_actually_emitted_validates():
    """Guards the hand-written patch dicts in engine/advisories.py."""
    patches = [o["patch"] for a in ALL_ADVISORIES for o in a.options]
    assert patches, "no advisory carried options - the walk above missed one"

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
