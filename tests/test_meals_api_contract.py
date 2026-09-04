"""Meal-logging endpoints through the real route (PRD §6, §8) - auth gating and
the request/response contract. Mirrors tests/test_api_contract.py: Prisma is
never reached, `meals.repository` is monkeypatched the same way BearerAuthentication's
`authx.repository` is.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from django.test import Client
from django.urls import reverse

from authx import repository as authx_repository
from authx.tokens import issue_guest_token
from meals import repository as meals_repository


@pytest.fixture
def client():
    return Client()


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
        servingUnits=[SimpleNamespace(unit="katori", grams=150.0, type="HOUSEHOLD")],
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def make_meal(items, **overrides):
    fields = dict(
        id="meal-1",
        name="Lunch",
        slot="LUNCH",
        source="MANUAL",
        loggedAt=datetime(2026, 9, 4, 13, 0, tzinfo=timezone.utc),
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
def guest(monkeypatch):
    monkeypatch.setattr(
        authx_repository,
        "get_user",
        lambda user_id: SimpleNamespace(id=user_id, claimedAt=None),
    )
    return "Bearer " + issue_guest_token("user-1")["token"]


PROTECTED = [
    ("get", "/api/v1/meals/foods"),
    ("get", "/api/v1/meals"),
    ("post", "/api/v1/meals"),
]


@pytest.mark.parametrize("method,path", PROTECTED)
def test_protected_endpoints_reject_anonymous_callers(client, method, path):
    response = getattr(client, method)(path)
    assert response.status_code == 401
    assert response.json()["error"]["code"]


def test_urls_resolve():
    assert reverse("meals-food-search") == "/api/v1/meals/foods"
    assert reverse("meals-list-create") == "/api/v1/meals"
    assert reverse("meals-detail", args=["meal-1"]) == "/api/v1/meals/meal-1"
    assert (
        reverse("meals-item", args=["meal-1", "item-1"])
        == "/api/v1/meals/meal-1/items/item-1"
    )


def test_food_search_returns_catalog_matches(client, guest, monkeypatch):
    monkeypatch.setattr(
        meals_repository, "search_foods", lambda query, **kw: [make_food()]
    )
    response = client.get("/api/v1/meals/foods?q=rice", HTTP_AUTHORIZATION=guest)

    assert response.status_code == 200
    body = response.json()
    assert body["foods"][0]["name"] == "Cooked White Rice"
    assert body["foods"][0]["servingUnits"][0]["unit"] == "katori"


def test_creating_a_meal_returns_the_computed_totals(client, guest, monkeypatch):
    food = make_food()
    monkeypatch.setattr(meals_repository, "get_food", lambda food_id: food)

    def create_logged_meal(user_id, meal_data, items_data):
        item = SimpleNamespace(
            id="item-1",
            foodId=food.id,
            food=food,
            **{k: v for k, v in items_data[0].items() if k != "foodId"},
        )
        return make_meal([item], userId=user_id, **meal_data)

    monkeypatch.setattr(meals_repository, "create_logged_meal", create_logged_meal)

    response = client.post(
        "/api/v1/meals",
        data={
            "name": "Lunch",
            "slot": "LUNCH",
            "items": [{"foodId": food.id, "quantity": 200.0, "unit": "g"}],
        },
        content_type="application/json",
        HTTP_AUTHORIZATION=guest,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["totals"]["caloriesKcal"] == 260
    assert body["items"][0]["foodName"] == "Cooked White Rice"


def test_creating_a_meal_with_no_items_is_rejected(client, guest):
    response = client.post(
        "/api/v1/meals",
        data={"name": "Lunch", "slot": "LUNCH", "items": []},
        content_type="application/json",
        HTTP_AUTHORIZATION=guest,
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


def test_fetching_a_meal_that_does_not_exist_is_a_404(client, guest, monkeypatch):
    monkeypatch.setattr(meals_repository, "get_logged_meal", lambda user_id, meal_id: None)
    response = client.get("/api/v1/meals/no-such-meal", HTTP_AUTHORIZATION=guest)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "meal_not_found"
