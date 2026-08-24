"""Engine tests - PRD §6."""
from __future__ import annotations

import pytest

from engine import (
    DEFAULT_CONFIG,
    ActivityLevel,
    EngineConfig,
    Goal,
    PlanInput,
    SexAtBirth,
    calculate_bmr,
    calculate_plan,
)
from engine.calculator import RECONCILE_TOLERANCE_KCAL

PERSONA = PlanInput(
    sex_at_birth=SexAtBirth.MALE,
    age=30,
    weight_kg=90.0,
    height_cm=180.0,
    goal=Goal.LOSE,
    activity_level=ActivityLevel.SEDENTARY,
)


def test_worked_example_matches_prd_section_6_5():
    result = calculate_plan(PERSONA)

    assert result.bmr == 1880.0
    assert result.tdee == 2256.0
    assert result.calories_kcal == 1860
    assert result.macros.protein_g == 162
    assert result.macros.carbs_g == 186
    assert result.macros.fat_g == 52
    assert result.macros.fiber_g == 28
    assert result.weekly_change_kg == -0.4
    assert result.clamped is False
    assert result.is_estimate is False


def test_worked_example_response_shape_matches_prd_section_8():
    body = calculate_plan(PERSONA).to_response()

    assert body["calories"] == 1860
    assert body["macros"] == {"proteinG": 162, "carbsG": 186, "fatG": 52, "fiberG": 28}
    assert body["rationale"]["adjustmentKcal"] == -400
    assert body["rationale"]["weeklyChangeKg"] == -0.4
    assert body["rationale"]["clamped"] is False
    assert body["isEstimate"] is False
    assert body["bmr"] == 1880 and body["tdee"] == 2256


@pytest.mark.parametrize(
    "sex,expected_constant", [(SexAtBirth.MALE, 5), (SexAtBirth.FEMALE, -161)]
)
def test_bmr_uses_mifflin_st_jeor_constants(sex, expected_constant):
    inp = PlanInput(sex, 30, 90.0, 180.0, Goal.MAINTAIN, ActivityLevel.SEDENTARY)
    assert calculate_bmr(inp) == 10 * 90 + 6.25 * 180 - 5 * 30 + expected_constant


def test_fiber_is_excluded_from_the_energy_total():
    """§5.5 - fiber is a subset of carbohydrate grams and must never be added to
    the calorie total or any energy ring."""
    result = calculate_plan(PERSONA)
    energy = result.to_response()["macroEnergyKcal"]

    assert "fiber" not in energy
    assert energy["total"] == energy["protein"] + energy["carbs"] + energy["fat"]
    assert energy["total"] == result.calories_kcal


@pytest.mark.parametrize("sex", list(SexAtBirth))
@pytest.mark.parametrize("goal", list(Goal))
@pytest.mark.parametrize("activity", list(ActivityLevel))
@pytest.mark.parametrize("weight", [42.0, 68.5, 90.0, 140.0])
@pytest.mark.parametrize("age", [18, 35, 100])
def test_macros_always_reconcile_with_the_target(sex, goal, activity, weight, age):
    """§6.3 - protein*4 + fat*9 + carbs*4 must equal the target within rounding."""
    result = calculate_plan(
        PlanInput(sex, age, weight, 168.0, goal, activity, target_weight_kg=None)
    )
    drift = abs(result.macros.energy_kcal - result.calories_kcal)

    assert drift <= RECONCILE_TOLERANCE_KCAL
    assert result.macros.protein_g >= 0
    assert result.macros.carbs_g >= 0
    assert result.macros.fat_g >= 0


def test_goal_adjustments_are_locked_at_400():
    base = dict(age=30, weight_kg=90.0, height_cm=180.0, activity_level=ActivityLevel.SEDENTARY)
    maintain = calculate_plan(PlanInput(SexAtBirth.MALE, goal=Goal.MAINTAIN, **base))
    lose = calculate_plan(PlanInput(SexAtBirth.MALE, goal=Goal.LOSE, **base))
    gain = calculate_plan(PlanInput(SexAtBirth.MALE, goal=Goal.GAIN, **base))

    assert maintain.calories_kcal == 2260  # 2,256 rounded to the nearest 10
    assert lose.calories_kcal == 1860
    assert gain.calories_kcal == 2660
    assert (lose.adjustment_kcal, maintain.adjustment_kcal, gain.adjustment_kcal) == (
        -400,
        0,
        400,
    )


def test_activity_multipliers_match_the_prd_table():
    expected = {
        ActivityLevel.SEDENTARY: 1.2,
        ActivityLevel.LIGHT: 1.375,
        ActivityLevel.MODERATE: 1.55,
        ActivityLevel.VERY: 1.725,
        ActivityLevel.EXTREME: 1.9,
    }
    for level, multiplier in expected.items():
        assert DEFAULT_CONFIG.multiplier_for(level) == multiplier


def test_target_is_clamped_up_to_the_female_safety_floor():
    """§6.2 - a small, older, sedentary woman on a deficit lands below 1,200."""
    result = calculate_plan(
        PlanInput(SexAtBirth.FEMALE, 60, 45.0, 150.0, Goal.LOSE, ActivityLevel.SEDENTARY)
    )

    assert result.clamped is True
    assert result.calories_kcal == 1200
    assert result.safety_floor_kcal == 1200


def test_clamped_rationale_never_advertises_more_than_the_number_supports():
    """§6.2 - the displayed deficit is recomputed from the clamped target."""
    result = calculate_plan(
        PlanInput(SexAtBirth.FEMALE, 60, 45.0, 150.0, Goal.LOSE, ActivityLevel.SEDENTARY)
    )

    assert result.requested_adjustment_kcal == -400
    assert result.adjustment_kcal > -400  # a smaller deficit than was requested
    assert result.adjustment_kcal == round(result.calories_kcal - result.tdee)
    assert abs(result.weekly_change_kg) < 0.4


def test_male_floor_is_the_higher_one():
    result = calculate_plan(
        PlanInput(SexAtBirth.MALE, 65, 50.0, 155.0, Goal.LOSE, ActivityLevel.SEDENTARY)
    )
    assert result.safety_floor_kcal == 1500
    assert result.calories_kcal >= 1500


def test_unspecified_basis_is_a_defensive_fallback_not_an_error():
    """§6.4 - the engine must not throw on imported or malformed records."""
    result = calculate_plan(
        PlanInput(SexAtBirth.UNSPECIFIED, 30, 90.0, 180.0, Goal.LOSE, ActivityLevel.SEDENTARY)
    )

    assert result.is_estimate is True
    assert result.bmr == 10 * 90 + 6.25 * 180 - 5 * 30 - 78
    assert result.safety_floor_kcal == 1500  # the higher, safer floor
    assert "unspecified_body_basis_fallback" in result.notes


def test_male_and_female_paths_are_never_flagged_as_estimates():
    for sex in (SexAtBirth.MALE, SexAtBirth.FEMALE):
        result = calculate_plan(
            PlanInput(sex, 30, 70.0, 170.0, Goal.MAINTAIN, ActivityLevel.LIGHT)
        )
        assert result.is_estimate is False


def test_server_config_overrides_change_the_result():
    """§10 - the constants are tunable without an app release."""
    config = EngineConfig(
        id="cfg_test",
        name="aggressive",
        lose_adjustment_kcal=-500,
        protein_per_kg=2.2,
        fat_pct=0.30,
    )
    result = calculate_plan(PERSONA, config)

    assert result.calories_kcal == 1760  # 2,256 - 500, rounded to the nearest 10
    assert result.adjustment_kcal == -500
    assert result.macros.protein_g == 198  # 2.2 x 90
    assert result.macros.fat_g == 59  # 0.30 x 1,760 / 9
    assert result.macros.energy_kcal == pytest.approx(1760, abs=RECONCILE_TOLERANCE_KCAL)
    assert result.config.id == "cfg_test"


def test_weekly_change_is_zero_for_maintain():
    result = calculate_plan(
        PlanInput(SexAtBirth.FEMALE, 30, 65.0, 165.0, Goal.MAINTAIN, ActivityLevel.MODERATE)
    )
    assert result.adjustment_kcal == 0
    assert result.weekly_change_kg == 0.0
