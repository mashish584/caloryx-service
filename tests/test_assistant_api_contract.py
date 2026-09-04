"""Meal Assistant draft endpoints through the real route (PRD §9, §10) - auth
gating and the request/response contract. Mirrors tests/test_meals_api_contract.py:
Prisma is never reached, `assistant.repository`/`meals.repository` are
monkeypatched the same way BearerAuthentication's `authx.repository` is.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from django.test import Client
from django.urls import reverse

from assistant import repository as assistant_repository
from authx import repository as authx_repository
from authx.tokens import issue_guest_token
from meals import repository as meals_repository


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def guest(monkeypatch):
    monkeypatch.setattr(
        authx_repository,
        "get_user",
        lambda user_id: SimpleNamespace(id=user_id, claimedAt=None),
    )
    return "Bearer " + issue_guest_token("user-1")["token"]


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


def make_item(food, **overrides):
    fields = dict(
        id="item-1",
        resolution="RESOLVED",
        foodId=food.id,
        food=food,
        rawText="200g " + food.name,
        quantity=200.0,
        unit="g",
        grams=200.0,
        state="COOKED",
        defaultGrams=200.0,
        prep=None,
        sizeQualifier=None,
        quantitySource="EXPLICIT",
        massSource="DIRECT",
        matchScore=None,
        matchBand=None,
        dishCategory=None,
        kcalLow=None,
        kcalHigh=None,
        kcalMidpoint=None,
        profileVersion=None,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def make_draft(items, **overrides):
    fields = dict(
        id="draft-1",
        userId="user-1",
        sessionId="session-1",
        name="Lunch",
        slot="LUNCH",
        caloriesKcal=260.0,
        proteinG=5.4,
        carbsG=56.4,
        fatG=0.6,
        fiberG=0.8,
        parseTier="MANUAL",
        confidence=1.0,
        version=1,
        status="OPEN",
        expiresAt=datetime(2099, 1, 1, tzinfo=timezone.utc),
        items=items,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


PROTECTED = [
    ("post", "/api/v1/assistant/drafts"),
    ("get", "/api/v1/assistant/drafts/draft-1"),
    ("patch", "/api/v1/assistant/drafts/draft-1"),
    ("delete", "/api/v1/assistant/drafts/draft-1"),
    ("post", "/api/v1/assistant/drafts/draft-1/items"),
    ("patch", "/api/v1/assistant/drafts/draft-1/items/item-1"),
    ("delete", "/api/v1/assistant/drafts/draft-1/items/item-1"),
    ("post", "/api/v1/assistant/drafts/draft-1/confirm"),
    ("post", "/api/v1/assistant/messages"),
]


@pytest.mark.parametrize("method,path", PROTECTED)
def test_protected_endpoints_reject_anonymous_callers(client, method, path):
    response = getattr(client, method)(path)
    assert response.status_code == 401
    assert response.json()["error"]["code"]


def test_urls_resolve():
    assert reverse("assistant-drafts-create") == "/api/v1/assistant/drafts"
    assert reverse("assistant-drafts-detail", args=["draft-1"]) == "/api/v1/assistant/drafts/draft-1"
    assert (
        reverse("assistant-draft-items-create", args=["draft-1"])
        == "/api/v1/assistant/drafts/draft-1/items"
    )
    assert (
        reverse("assistant-draft-items-detail", args=["draft-1", "item-1"])
        == "/api/v1/assistant/drafts/draft-1/items/item-1"
    )
    assert (
        reverse("assistant-drafts-confirm", args=["draft-1"])
        == "/api/v1/assistant/drafts/draft-1/confirm"
    )
    assert reverse("assistant-messages") == "/api/v1/assistant/messages"


def test_creating_a_draft_returns_the_computed_totals(client, guest, monkeypatch):
    food = make_food()
    monkeypatch.setattr(meals_repository, "get_food", lambda food_id: food)
    monkeypatch.setattr(
        assistant_repository, "get_or_create_today_session", lambda user_id: SimpleNamespace(id="session-1")
    )

    def create_draft_with_expiry_check(user_id, session_id, draft_data, items_data):
        item = make_item(food, **{k: v for k, v in items_data[0].items() if k != "foodId"})
        return make_draft([item], userId=user_id, sessionId=session_id, **draft_data)

    monkeypatch.setattr(
        assistant_repository, "create_draft_with_expiry_check", create_draft_with_expiry_check
    )

    response = client.post(
        "/api/v1/assistant/drafts",
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
    assert body["parseTier"] == "MANUAL"
    assert body["items"][0]["perGram"]["kcal"] == pytest.approx(1.3)


def test_creating_a_draft_with_no_items_is_rejected(client, guest):
    response = client.post(
        "/api/v1/assistant/drafts",
        data={"name": "Lunch", "slot": "LUNCH", "items": []},
        content_type="application/json",
        HTTP_AUTHORIZATION=guest,
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


def test_creating_a_draft_while_one_is_open_returns_409_with_the_existing_draft(
    client, guest, monkeypatch
):
    food = make_food()
    existing = make_draft([make_item(food)])
    monkeypatch.setattr(meals_repository, "get_food", lambda food_id: food)
    monkeypatch.setattr(
        assistant_repository, "get_or_create_today_session", lambda user_id: SimpleNamespace(id="session-1")
    )
    monkeypatch.setattr(
        assistant_repository, "create_draft_with_expiry_check", lambda *a, **kw: None
    )
    monkeypatch.setattr(assistant_repository, "get_open_draft", lambda user_id: existing)

    response = client.post(
        "/api/v1/assistant/drafts",
        data={
            "name": "Snack",
            "slot": "SNACK",
            "items": [{"foodId": food.id, "quantity": 50.0, "unit": "g"}],
        },
        content_type="application/json",
        HTTP_AUTHORIZATION=guest,
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "open_draft_exists"
    assert body["error"]["details"]["draft"]["id"] == existing.id


def test_fetching_a_draft_that_does_not_exist_is_a_404(client, guest, monkeypatch):
    monkeypatch.setattr(assistant_repository, "get_draft", lambda user_id, draft_id: None)
    response = client.get("/api/v1/assistant/drafts/no-such-draft", HTTP_AUTHORIZATION=guest)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "draft_not_found"


def test_updating_a_draft_with_a_stale_version_returns_409_with_the_current_draft(
    client, guest, monkeypatch
):
    food = make_food()
    current = make_draft([make_item(food)], version=3)
    monkeypatch.setattr(assistant_repository, "get_draft", lambda user_id, draft_id: current)
    monkeypatch.setattr(assistant_repository, "expire_draft_if_stale", lambda draft: draft)

    response = client.patch(
        "/api/v1/assistant/drafts/draft-1",
        data={"name": "New name", "version": 1},
        content_type="application/json",
        HTTP_AUTHORIZATION=guest,
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "draft_version_conflict"
    assert body["error"]["details"]["draft"]["version"] == 3


def test_confirming_a_draft_returns_the_logged_meal_and_daily_totals(client, guest, monkeypatch):
    food = make_food()
    draft = make_draft([make_item(food)])
    monkeypatch.setattr(assistant_repository, "get_draft", lambda user_id, draft_id: draft)
    monkeypatch.setattr(assistant_repository, "expire_draft_if_stale", lambda d: d)
    monkeypatch.setattr(assistant_repository, "get_idempotency_record", lambda key: None)
    monkeypatch.setattr(assistant_repository, "update_draft", lambda draft_id, data: draft)
    monkeypatch.setattr(assistant_repository, "save_idempotency_record", lambda *a, **kw: None)

    def create_logged_meal(user_id, meal_data, items_data):
        item = SimpleNamespace(
            id="lmi-1",
            foodId=food.id,
            food=food,
            **{k: v for k, v in items_data[0].items() if k != "foodId"},
        )
        return SimpleNamespace(
            id="meal-1",
            loggedAt=datetime.now(timezone.utc),
            items=[item],
            **meal_data,
        )

    monkeypatch.setattr(meals_repository, "create_logged_meal", create_logged_meal)
    monkeypatch.setattr(meals_repository, "list_logged_meals", lambda user_id, **kw: [])

    response = client.post(
        "/api/v1/assistant/drafts/draft-1/confirm",
        data={"idempotencyKey": "idem-1", "version": 1},
        content_type="application/json",
        HTTP_AUTHORIZATION=guest,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["loggedMeal"]["source"] == "CHAT_AI"
    assert body["dailyTotals"]["caloriesKcal"] == 0


def test_sending_a_message_creates_a_draft_from_text(client, guest, monkeypatch):
    food = make_food()
    monkeypatch.setattr(meals_repository, "get_food", lambda food_id: food)
    monkeypatch.setattr(meals_repository, "search_foods", lambda query, **kw: [food])
    monkeypatch.setattr(assistant_repository, "get_open_draft", lambda user_id: None)
    monkeypatch.setattr(
        assistant_repository, "get_or_create_today_session", lambda user_id: SimpleNamespace(id="session-1")
    )
    monkeypatch.setattr(assistant_repository, "get_idempotency_record", lambda key: None)
    monkeypatch.setattr(assistant_repository, "save_idempotency_record", lambda *a, **kw: None)
    monkeypatch.setattr(assistant_repository, "find_cached_message", lambda user_id, h: None)
    monkeypatch.setattr(
        assistant_repository,
        "create_chat_message",
        lambda session_id, user_id, data: SimpleNamespace(id="msg-1", **data),
    )

    def create_draft_with_expiry_check(user_id, session_id, draft_data, items_data):
        item = make_item(food, **{k: v for k, v in items_data[0].items() if k != "foodId"})
        return make_draft([item], userId=user_id, sessionId=session_id, **draft_data)

    monkeypatch.setattr(
        assistant_repository, "create_draft_with_expiry_check", create_draft_with_expiry_check
    )

    response = client.post(
        "/api/v1/assistant/messages",
        data={"clientMessageId": "m1", "content": "200g rice"},
        content_type="application/json",
        HTTP_AUTHORIZATION=guest,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["tier"] == "PARSER"
    assert body["intent"] == "LOG_NEW"
    assert body["draft"]["totals"]["caloriesKcal"] == 260
    assert body["requestId"]


def test_sending_a_new_meal_message_while_a_draft_is_open_asks_for_clarification(
    client, guest, monkeypatch
):
    food = make_food()
    existing_draft = make_draft([make_item(food)])
    monkeypatch.setattr(assistant_repository, "get_open_draft", lambda user_id: existing_draft)
    monkeypatch.setattr(assistant_repository, "expire_draft_if_stale", lambda d: d)
    monkeypatch.setattr(assistant_repository, "get_idempotency_record", lambda key: None)
    monkeypatch.setattr(assistant_repository, "save_idempotency_record", lambda *a, **kw: None)
    monkeypatch.setattr(
        assistant_repository, "get_or_create_today_session", lambda user_id: SimpleNamespace(id="session-1")
    )
    monkeypatch.setattr(
        assistant_repository,
        "create_chat_message",
        lambda session_id, user_id, data: SimpleNamespace(id="msg-1", **data),
    )

    response = client.post(
        "/api/v1/assistant/messages",
        data={"clientMessageId": "m2", "content": "1 piece boiled egg"},
        content_type="application/json",
        HTTP_AUTHORIZATION=guest,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["needsClarification"] == {"reason": "open_draft", "candidates": ["ADD", "NEW"]}
    assert body["draft"]["id"] == existing_draft.id
