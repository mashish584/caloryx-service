"""Fixed, versioned evaluation set (§12.10, §12.11, Chunk 8e).

An honestly-scoped starter set - a few dozen hand-authored cases, not the
PRD's "few hundred" - covering the four PRD-mandated axes measured
separately: extraction accuracy, food-resolution accuracy, nutrition error,
and intent-classification accuracy. Restricted to the deterministic tiers
(T-1's classifier, T1's grammar, composite matching, the nutrition engine) -
there is no live OpenAI key in this environment to assert T2/T3 output
against, so an LLM-inclusive eval set is a documented gap, not silently
narrowed away (see the Chunk 8e plan).

Every LOG_NEW-shaped case runs through the real production resolution
function, `assistant.services._build_items_from_phrase` - the same one
`send_message` itself calls - against a small, fixed, in-test food/composite
catalog (deliberately independent of `seed_foods.py`'s mutable dev data, so
this golden set's ground truth can't silently drift when someone tunes a
seed value). Expected values are derived independently by hand (see the
Chunk 8e plan), not copied from a run of the code under test - a golden set
that just encodes whatever the code currently does could never catch a
regression.

Each case is asserted individually (`pytest.mark.parametrize`), not against
an aggregate percentage - every case here runs through fully deterministic
code, so a correct implementation reproduces every expected value exactly
(floating-point rounding aside). The PRD's "MAE at p50/p90, segmented by
tier and cuisine" framing fits a noisy, LLM-driven signal this environment
has no live model to produce; the aggregate nutrition-MAE test below reports
a single number instead, which should be ~0 for a deterministic engine - any
drift is a real regression.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List
from types import SimpleNamespace

import pytest

import chatparser
from assistant import services as assistant_services
from meals import repository as meals_repository

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "eval_golden_set.json"
_GOLDEN_SET = json.loads(_FIXTURE_PATH.read_text())
assert _GOLDEN_SET["version"] == 1

_CASES_BY_ID = {case["id"]: case for case in _GOLDEN_SET["cases"]}


def _cases(category: str) -> List[Dict[str, Any]]:
    return [c for c in _GOLDEN_SET["cases"] if c["category"] == category]


_LOG_NEW_CATEGORIES = {
    "quantified",
    "household_measure",
    "multi_item",
    "composite",
    "regional",
    "misspelling",
}
_LOG_NEW_CASES = [c for c in _GOLDEN_SET["cases"] if c["category"] in _LOG_NEW_CATEGORIES]
_EDIT_CASES = _cases("edit")
_NON_LOGGING_CASES = _cases("non_logging")


# -- a small, fixed eval catalog - independent of seed_foods.py's mutable data


def _food(id, name, kcal, protein, carbs, fat, fiber, default_state="COOKED", yield_=None, units=None):
    return SimpleNamespace(
        id=id,
        name=name,
        defaultState=default_state,
        rawToCookedYield=yield_,
        caloriesKcalPer100g=kcal,
        proteinGPer100g=protein,
        carbsGPer100g=carbs,
        fatGPer100g=fat,
        fiberGPer100g=fiber,
        servingUnits=[SimpleNamespace(unit=u, grams=g, type="HOUSEHOLD") for u, g in (units or [])],
    )


_RICE = _food("food-rice", "Cooked White Rice", 130.0, 2.7, 28.2, 0.3, 0.4, yield_=3.0, units=[("katori", 150.0)])
_CHICKEN = _food("food-chicken", "Grilled Chicken Breast", 165.0, 31.0, 0.0, 3.6, 0.0)
_EGG = _food("food-egg", "Boiled Egg", 155.0, 13.0, 1.1, 11.0, 0.0, units=[("piece", 50.0)])
_DAL = _food("food-dal", "Toor Dal, Cooked", 120.0, 6.0, 20.0, 2.0, 3.0)
_ROTI = _food("food-roti", "Whole Wheat Roti", 270.0, 8.0, 48.0, 4.0, 6.0, units=[("piece", 40.0)])
_SPINACH = _food("food-spinach", "Spinach, Raw", 23.0, 2.9, 3.6, 0.4, 2.2, default_state="RAW")

_EVAL_FOODS = {f.id: f for f in [_RICE, _CHICKEN, _EGG, _DAL, _ROTI, _SPINACH]}


def _component(food, ratio):
    return SimpleNamespace(food=food, ratioOfServing=ratio, state="UNSPECIFIED", prep=None)


_BIRYANI = SimpleNamespace(
    name="Chicken Biryani",
    aliases=["biryani"],
    servingGrams=300.0,
    isCurated=True,
    components=[_component(_RICE, 0.6), _component(_CHICKEN, 0.4)],
)
_DAL_CHAWAL = SimpleNamespace(
    name="Dal Chawal",
    aliases=["dal rice"],
    servingGrams=300.0,
    isCurated=True,
    components=[_component(_DAL, 0.5), _component(_RICE, 0.5)],
)
_EVAL_COMPOSITES = [_BIRYANI, _DAL_CHAWAL]


@pytest.fixture(autouse=True)
def _eval_catalog(monkeypatch):
    monkeypatch.setattr(meals_repository, "get_food", lambda food_id: _EVAL_FOODS.get(food_id))
    monkeypatch.setattr(meals_repository, "search_foods", lambda query, **kw: list(_EVAL_FOODS.values()))
    monkeypatch.setattr(meals_repository, "get_composite_foods", lambda: _EVAL_COMPOSITES)
    monkeypatch.setattr(meals_repository, "file_food_miss", lambda raw_text, locale="": None)


def _resolve(text: str) -> List[Dict[str, Any]]:
    normalized = chatparser.normalize_text(text)
    phrases, _unconsumed = chatparser.parse_new_item_phrases(normalized)
    resolved: List[Dict[str, Any]] = []
    for phrase in phrases:
        for payload, vector in assistant_services._build_items_from_phrase(phrase):
            resolved.append(
                {
                    "foodId": payload.get("foodId"),
                    "quantity": payload.get("quantity"),
                    "unit": payload.get("unit"),
                    "resolution": payload["resolution"],
                    "caloriesKcal": vector.calories_kcal,
                }
            )
    return resolved


# -- extraction + food-resolution + nutrition --------------------------------


@pytest.mark.parametrize("case", _LOG_NEW_CASES, ids=[c["id"] for c in _LOG_NEW_CASES])
def test_extraction_resolution_and_nutrition(case):
    actual = _resolve(case["input"])
    expected = case["expectedItems"]

    assert len(actual) == len(expected), "item count mismatch for case {}".format(case["id"])
    for actual_item, expected_item in zip(actual, expected):
        assert actual_item["foodId"] == expected_item["foodId"]
        assert actual_item["quantity"] == expected_item["quantity"]
        assert actual_item["unit"] == expected_item["unit"]
        assert actual_item["resolution"] == expected_item["resolution"]
        assert actual_item["caloriesKcal"] == pytest.approx(expected_item["caloriesKcal"], abs=0.05)


def test_nutrition_mae_is_within_tolerance():
    errors = []
    for case in _LOG_NEW_CASES:
        actual = _resolve(case["input"])
        for actual_item, expected_item in zip(actual, case["expectedItems"]):
            errors.append(abs(actual_item["caloriesKcal"] - expected_item["caloriesKcal"]))

    mae = sum(errors) / len(errors)
    print("\ngolden-set nutrition MAE (kcal): {:.4f} over {} items".format(mae, len(errors)))
    assert mae <= 0.05


# -- edit-grammar extraction accuracy -----------------------------------------


@pytest.mark.parametrize("case", _EDIT_CASES, ids=[c["id"] for c in _EDIT_CASES])
def test_edit_grammar(case):
    parsed = chatparser.parse_edit_command(chatparser.normalize_text(case["input"]))
    expected = case["expectedEdit"]

    assert parsed is not None, "expected an edit match for case {}".format(case["id"])
    assert parsed.intent == expected["intent"]
    if "targetText" in expected:
        assert parsed.target_text == expected["targetText"]
    if "quantity" in expected:
        assert parsed.quantity == expected["quantity"]
    if "unit" in expected:
        assert parsed.unit == expected["unit"]
    if "slot" in expected:
        assert parsed.slot == expected["slot"]
    if "itemFoodText" in expected:
        assert parsed.item is not None
        assert parsed.item.food_text == expected["itemFoodText"]
        assert parsed.item.quantity == expected["itemQuantity"]
        assert parsed.item.unit == expected["itemUnit"]


# -- intent-classification accuracy -------------------------------------------


@pytest.mark.parametrize("case", _NON_LOGGING_CASES, ids=[c["id"] for c in _NON_LOGGING_CASES])
def test_intent_classification(case):
    actual = chatparser.classify_t1_intent(chatparser.normalize_text(case["input"]))
    assert actual == case["expectedIntent"]
