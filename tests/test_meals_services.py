"""Meal-logging use cases - PRD §6, §8. Prisma is never reached; `meals.repository`
is the seam (see tests/settings.py and tests/test_services.py for the pattern).
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from common.exceptions import NotFoundError, UnresolvableQuantityError
from meals import repository, services

LOGGED_AT = datetime(2026, 9, 4, 13, 0, tzinfo=timezone.utc)


def serving_unit(unit, grams, type_="HOUSEHOLD"):
    return SimpleNamespace(unit=unit, grams=grams, type=type_)


def make_food(**overrides):
    fields = dict(
        id="food-rice",
        name="Cooked White Rice",
        source="CALORYX_CURATED",
        defaultState="COOKED",
        rawToCookedYield=3.0,
        caloriesKcalPer100g=130.0,
        proteinGPer100g=2.7,
        carbsGPer100g=28.2,
        fatGPer100g=0.3,
        fiberGPer100g=0.4,
        servingUnits=[serving_unit("katori", 150.0)],
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def make_item(food, **overrides):
    fields = dict(
        id="item-1",
        foodId=food.id,
        food=food,
        quantity=200.0,
        unit="g",
        grams=200.0,
        state="COOKED",
        caloriesKcal=260.0,
        proteinG=5.4,
        carbsG=56.4,
        fatG=0.6,
        fiberG=0.8,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def make_meal(items, **overrides):
    fields = dict(
        id="meal-1",
        name="Lunch",
        slot="LUNCH",
        source="MANUAL",
        loggedAt=LOGGED_AT,
        userId="user-1",
        caloriesKcal=sum(i.caloriesKcal for i in items),
        proteinG=sum(i.proteinG for i in items),
        carbsG=sum(i.carbsG for i in items),
        fatG=sum(i.fatG for i in items),
        fiberG=sum(i.fiberG for i in items if i.fiberG is not None) or None,
        items=items,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


@pytest.fixture
def seam(monkeypatch):
    state = SimpleNamespace(foods={}, meal=None, created=None, updated_totals=None, updated_item=None)

    monkeypatch.setattr(repository, "get_food", lambda food_id: state.foods.get(food_id))

    def create_logged_meal(user_id, meal_data, items_data):
        state.created = (user_id, meal_data, items_data)
        items = [
            make_item(
                state.foods[item["foodId"]],
                id="item-{}".format(i),
                **{k: v for k, v in item.items() if k != "foodId"},
            )
            for i, item in enumerate(items_data)
        ]
        state.meal = make_meal(items, userId=user_id)
        return state.meal

    monkeypatch.setattr(repository, "create_logged_meal", create_logged_meal)

    def get_logged_meal(user_id, meal_id):
        # Same 404 either way a real ownership mismatch would (repository.py) -
        # the fake has to reproduce that check, or a cross-user leak here would
        # go undetected.
        if state.meal is None or state.meal.id != meal_id or state.meal.userId != user_id:
            return None
        return state.meal

    monkeypatch.setattr(repository, "get_logged_meal", get_logged_meal)

    def get_logged_meal_item(user_id, meal_id, item_id):
        meal = state.meal
        if meal is None or meal.id != meal_id:
            return None
        return next((i for i in meal.items if i.id == item_id), None)

    monkeypatch.setattr(repository, "get_logged_meal_item", get_logged_meal_item)

    def update_logged_meal_item(item_id, data):
        state.updated_item = (item_id, data)
        items = [i for i in state.meal.items if i.id != item_id]
        food = state.foods[data["foodId"]]
        items.append(make_item(food, id=item_id, **{k: v for k, v in data.items() if k != "foodId"}))
        state.meal = make_meal(items, id=state.meal.id, userId=state.meal.userId)
        return next(i for i in state.meal.items if i.id == item_id)

    monkeypatch.setattr(repository, "update_logged_meal_item", update_logged_meal_item)

    def delete_logged_meal_item(item_id):
        items = [i for i in state.meal.items if i.id != item_id]
        state.meal = make_meal(items, id=state.meal.id, userId=state.meal.userId)

    monkeypatch.setattr(repository, "delete_logged_meal_item", delete_logged_meal_item)

    def update_logged_meal_totals(meal_id, totals):
        state.updated_totals = totals
        state.meal = make_meal(state.meal.items, id=meal_id, userId=state.meal.userId, **totals)
        return state.meal

    monkeypatch.setattr(repository, "update_logged_meal_totals", update_logged_meal_totals)

    def delete_logged_meal(user_id, meal_id):
        if state.meal is None or state.meal.id != meal_id:
            return False
        state.meal = None
        return True

    monkeypatch.setattr(repository, "delete_logged_meal", delete_logged_meal)
    return state


# -- log_meal (§4 "fully quantified") -----------------------------------------


def test_log_meal_computes_totals_from_per_100g_and_grams(seam):
    seam.foods["food-rice"] = make_food()
    payload = services.log_meal(
        "user-1",
        {
            "name": "Lunch",
            "slot": "LUNCH",
            "items": [{"foodId": "food-rice", "quantity": 200.0, "unit": "g"}],
        },
    )

    assert payload["totals"]["caloriesKcal"] == 260  # 130 * 2
    assert payload["items"][0]["grams"] == 200.0
    assert payload["items"][0]["state"] == "COOKED"


def test_log_meal_resolves_a_household_unit_via_the_foods_serving_table(seam):
    seam.foods["food-rice"] = make_food()
    services.log_meal(
        "user-1",
        {
            "name": "Lunch",
            "slot": "LUNCH",
            "items": [{"foodId": "food-rice", "quantity": 1.0, "unit": "katori"}],
        },
    )
    _, _, items_data = seam.created
    assert items_data[0]["grams"] == 150.0


def test_log_meal_converts_a_raw_stated_quantity_through_the_yield_factor(seam):
    """200g raw rice at a 3.0 yield -> 600g cooked-equivalent for the lookup,
    but the stored `grams` stays the stated (raw) amount (§8)."""
    seam.foods["food-rice"] = make_food()
    payload = services.log_meal(
        "user-1",
        {
            "name": "Lunch",
            "slot": "LUNCH",
            "items": [
                {"foodId": "food-rice", "quantity": 200.0, "unit": "g", "state": "RAW"}
            ],
        },
    )
    item = payload["items"][0]
    assert item["grams"] == 200.0
    assert item["state"] == "RAW"
    assert item["caloriesKcal"] == 780  # 130 kcal/100g cooked-basis * 600g


def test_log_meal_raises_food_not_found_for_an_unknown_food_id(seam):
    with pytest.raises(NotFoundError) as exc_info:
        services.log_meal(
            "user-1",
            {
                "name": "Lunch",
                "slot": "LUNCH",
                "items": [{"foodId": "missing", "quantity": 100.0, "unit": "g"}],
            },
        )
    assert exc_info.value.code == "food_not_found"


def test_log_meal_raises_invalid_quantity_for_an_undeclared_unit(seam):
    seam.foods["food-rice"] = make_food()
    with pytest.raises(UnresolvableQuantityError):
        services.log_meal(
            "user-1",
            {
                "name": "Lunch",
                "slot": "LUNCH",
                "items": [{"foodId": "food-rice", "quantity": 1.0, "unit": "cup"}],
            },
        )


def test_log_meal_raises_invalid_quantity_when_no_yield_factor_covers_the_stated_state(seam):
    seam.foods["food-rice"] = make_food(rawToCookedYield=None)
    with pytest.raises(UnresolvableQuantityError):
        services.log_meal(
            "user-1",
            {
                "name": "Lunch",
                "slot": "LUNCH",
                "items": [
                    {"foodId": "food-rice", "quantity": 100.0, "unit": "g", "state": "RAW"}
                ],
            },
        )


def test_log_meal_preserves_missing_fiber_as_none_in_the_response(seam):
    seam.foods["food-oil"] = make_food(
        id="food-oil", name="Olive Oil", defaultState="UNSPECIFIED",
        rawToCookedYield=None, fiberGPer100g=None, servingUnits=[],
    )
    payload = services.log_meal(
        "user-1",
        {
            "name": "Lunch",
            "slot": "LUNCH",
            "items": [{"foodId": "food-oil", "quantity": 10.0, "unit": "g"}],
        },
    )
    assert payload["items"][0]["fiberG"] is None
    assert payload["totals"]["fiberG"] is None


# -- fetch / delete meal --------------------------------------------------


def test_fetch_logged_meal_raises_not_found_for_another_users_meal(seam):
    seam.foods["food-rice"] = make_food()
    services.log_meal(
        "user-1",
        {"name": "L", "slot": "LUNCH", "items": [{"foodId": "food-rice", "quantity": 100.0, "unit": "g"}]},
    )
    with pytest.raises(NotFoundError):
        services.fetch_logged_meal("someone-else", seam.meal.id)


def test_delete_logged_meal_raises_not_found_when_missing(seam):
    with pytest.raises(NotFoundError):
        services.delete_logged_meal("user-1", "no-such-meal")


# -- item edit / delete recompute totals (§6 - ingredients are the source of truth) --


def test_update_logged_meal_item_recomputes_meal_totals(seam):
    seam.foods["food-rice"] = make_food()
    services.log_meal(
        "user-1",
        {"name": "L", "slot": "LUNCH", "items": [{"foodId": "food-rice", "quantity": 100.0, "unit": "g"}]},
    )
    item_id = seam.meal.items[0].id

    payload = services.update_logged_meal_item(
        "user-1", seam.meal.id, item_id, {"quantity": 300.0}
    )

    assert payload["totals"]["caloriesKcal"] == 390  # 130 * 3


def test_update_logged_meal_item_raises_not_found_for_a_missing_item(seam):
    seam.foods["food-rice"] = make_food()
    services.log_meal(
        "user-1",
        {"name": "L", "slot": "LUNCH", "items": [{"foodId": "food-rice", "quantity": 100.0, "unit": "g"}]},
    )
    with pytest.raises(NotFoundError):
        services.update_logged_meal_item("user-1", seam.meal.id, "no-such-item", {"quantity": 50.0})


def test_delete_logged_meal_item_recomputes_meal_totals_to_zero_when_last_item_removed(seam):
    seam.foods["food-rice"] = make_food()
    services.log_meal(
        "user-1",
        {"name": "L", "slot": "LUNCH", "items": [{"foodId": "food-rice", "quantity": 100.0, "unit": "g"}]},
    )
    item_id = seam.meal.items[0].id

    payload = services.delete_logged_meal_item("user-1", seam.meal.id, item_id)

    assert payload["items"] == []
    assert payload["totals"]["caloriesKcal"] == 0
