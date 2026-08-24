"""Edge-case advisory tests - PRD §9."""
from __future__ import annotations

from engine import Goal, bmi, clamped_advisory, evaluate_profile


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
