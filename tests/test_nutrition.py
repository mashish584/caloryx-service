"""Nutrition engine - PRD §8. Pure math, no DB, no Django (mirrors test_engine.py)."""
from __future__ import annotations

import pytest

from nutrition import (
    FoodState,
    NutrientVector,
    NutritionError,
    ServingUnit,
    UnknownServingUnitError,
    apply_yield,
    item_nutrition,
    resolve_grams,
    sum_nutrition,
)


# -- item_nutrition -----------------------------------------------------------


def test_item_nutrition_scales_per_100g_by_grams():
    per_100g = NutrientVector(165.0, 31.0, 0.0, 3.6, 0.0)
    result = item_nutrition(per_100g, 120.0)

    assert result.calories_kcal == pytest.approx(198.0)
    assert result.protein_g == pytest.approx(37.2)
    assert result.fat_g == pytest.approx(4.32)


def test_item_nutrition_preserves_missing_fiber_as_none_not_zero():
    """§8 - missing != zero, so a food with no fiber data must not report 0."""
    per_100g = NutrientVector(100.0, 5.0, 10.0, 2.0, fiber_g=None)
    result = item_nutrition(per_100g, 200.0)
    assert result.fiber_g is None


# -- sum_nutrition (§6 - meal totals are always the sum of items) -------------


def test_sum_nutrition_adds_component_vectors():
    a = NutrientVector(100.0, 10.0, 5.0, 2.0, 1.0)
    b = NutrientVector(50.0, 5.0, 2.5, 1.0, 0.5)
    total = sum_nutrition([a, b])

    assert total.calories_kcal == pytest.approx(150.0)
    assert total.protein_g == pytest.approx(15.0)
    assert total.fiber_g == pytest.approx(1.5)


def test_sum_nutrition_of_no_items_is_zero():
    total = sum_nutrition([])
    assert total.calories_kcal == 0.0
    assert total.fiber_g is None


def test_sum_nutrition_treats_all_missing_fiber_as_none():
    a = NutrientVector(100.0, 10.0, 5.0, 2.0, fiber_g=None)
    b = NutrientVector(50.0, 5.0, 2.5, 1.0, fiber_g=None)
    assert sum_nutrition([a, b]).fiber_g is None


def test_sum_nutrition_reports_the_known_fiber_alongside_an_unknown_item():
    a = NutrientVector(100.0, 10.0, 5.0, 2.0, fiber_g=3.0)
    b = NutrientVector(50.0, 5.0, 2.5, 1.0, fiber_g=None)
    assert sum_nutrition([a, b]).fiber_g == pytest.approx(3.0)


# -- apply_yield (§8 - raw/cooked, the largest single accuracy risk) ----------


def test_apply_yield_converts_raw_to_cooked():
    # Rice: 100g raw -> 300g cooked at a 3.0 yield.
    grams = apply_yield(
        100.0, from_state=FoodState.RAW, to_state=FoodState.COOKED, raw_to_cooked_yield=3.0
    )
    assert grams == pytest.approx(300.0)


def test_apply_yield_converts_cooked_to_raw():
    grams = apply_yield(
        300.0, from_state=FoodState.COOKED, to_state=FoodState.RAW, raw_to_cooked_yield=3.0
    )
    assert grams == pytest.approx(100.0)


def test_apply_yield_is_a_no_op_when_states_match():
    grams = apply_yield(
        150.0, from_state=FoodState.COOKED, to_state=FoodState.COOKED, raw_to_cooked_yield=None
    )
    assert grams == 150.0


@pytest.mark.parametrize(
    "from_state,to_state",
    [(FoodState.UNSPECIFIED, FoodState.COOKED), (FoodState.RAW, FoodState.UNSPECIFIED)],
)
def test_apply_yield_treats_unspecified_as_nothing_to_convert(from_state, to_state):
    grams = apply_yield(
        150.0, from_state=from_state, to_state=to_state, raw_to_cooked_yield=None
    )
    assert grams == 150.0


def test_apply_yield_raises_without_a_yield_factor():
    """A food with no catalogued conversion must fail loudly, not guess."""
    with pytest.raises(NutritionError):
        apply_yield(
            100.0, from_state=FoodState.RAW, to_state=FoodState.COOKED, raw_to_cooked_yield=None
        )


# -- resolve_grams (§8 - household measures are per-food) ---------------------


def test_resolve_grams_treats_g_and_kg_as_universal():
    assert resolve_grams(200.0, "g", []) == 200.0
    assert resolve_grams(1.5, "kg", []) == 1500.0


def test_resolve_grams_uses_the_foods_declared_serving_unit():
    units = [ServingUnit(unit="katori", grams=150.0, type="HOUSEHOLD")]
    assert resolve_grams(2.0, "katori", units) == 300.0


def test_resolve_grams_is_case_and_whitespace_insensitive():
    units = [ServingUnit(unit="Katori", grams=150.0, type="HOUSEHOLD")]
    assert resolve_grams(1.0, " KATORI ", units) == 150.0


def test_resolve_grams_rejects_an_undeclared_unit():
    """A cup of rice and a cup of spinach are not the same mass (§8) - an
    undeclared unit must fail rather than silently assume grams."""
    with pytest.raises(UnknownServingUnitError):
        resolve_grams(1.0, "cup", [])
