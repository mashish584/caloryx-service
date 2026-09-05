"""Meal Assistant draft use cases - PRD §9, §12.1, §12.2, §12.5. Prisma is
never reached; `assistant.repository` and `meals.repository` are the seams
(see tests/test_meals_services.py for the pattern this follows).
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from assistant import repository, services
from chatparser import hash_normalized, normalize_text
from common.exceptions import (
    DraftNotOpenError,
    DraftVersionConflictError,
    IdempotencyKeyReuseError,
    NotFoundError,
    OpenDraftExistsError,
)
from engine.rounding import round_int
from meals import repository as meals_repository
from nutrition import NutrientVector, item_nutrition

REAL_PAST = datetime(2020, 1, 1, tzinfo=timezone.utc)
REAL_FUTURE = datetime(2099, 1, 1, tzinfo=timezone.utc)


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
        draftId="draft-1",
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


def make_unresolved_item(**overrides):
    fields = dict(
        id="item-1",
        draftId="draft-1",
        resolution="UNRESOLVED",
        foodId=None,
        food=None,
        rawText="unresolved item",
        quantity=None,
        unit=None,
        grams=None,
        state="UNSPECIFIED",
        defaultGrams=None,
        prep=None,
        sizeQualifier=None,
        quantitySource=None,
        massSource=None,
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


def item_from_payload(state, item_data):
    """Builds a fake `MealDraftItem` row from a service-produced payload dict
    (RESOLVED - has `foodId` - or UNRESOLVED - doesn't), the way the real
    repository's `create_draft_item`/`create_draft_with_expiry_check` would."""
    item_id = "item-{}".format(state.next_item_id)
    state.next_item_id += 1
    if item_data.get("resolution") == "UNRESOLVED":
        return make_unresolved_item(id=item_id, **item_data)
    food = state.foods[item_data["foodId"]]
    return make_item(food, id=item_id, **{k: v for k, v in item_data.items() if k != "foodId"})


def make_draft(items, **overrides):
    fields = dict(
        id="draft-1",
        userId="user-1",
        sessionId="session-1",
        name="Lunch",
        slot="LUNCH",
        # Totals default to 0 here - real calls always pass explicit totals
        # (via _totals_payload) as overrides, so this default is never what a
        # test actually asserts against.
        caloriesKcal=0.0,
        proteinG=0.0,
        carbsG=0.0,
        fatG=0.0,
        fiberG=None,
        parseTier="MANUAL",
        confidence=1.0,
        version=1,
        status="OPEN",
        expiresAt=REAL_FUTURE,
        items=items,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


@pytest.fixture
def seam(monkeypatch):
    state = SimpleNamespace(
        foods={},
        composites={},
        food_misses=[],
        draft=None,
        next_item_id=1,
        next_draft_id=1,
        idempotency={},
        logged_meals=[],
        create_logged_meal_calls=0,
        messages=[],
    )

    # -- assistant.repository ------------------------------------------------

    monkeypatch.setattr(
        repository, "get_or_create_today_session", lambda user_id: SimpleNamespace(id="session-1")
    )

    def create_draft_with_expiry_check(user_id, session_id, draft_data, items_data):
        if state.draft is not None and state.draft.status == "OPEN":
            if state.draft.expiresAt >= datetime.now(timezone.utc):
                return None
            state.draft = SimpleNamespace(**{**state.draft.__dict__, "status": "EXPIRED"})

        items = [item_from_payload(state, item_data) for item_data in items_data]

        state.draft = make_draft(
            items,
            id="draft-{}".format(state.next_draft_id),
            userId=user_id,
            sessionId=session_id,
            **draft_data
        )
        state.next_draft_id += 1
        return state.draft

    monkeypatch.setattr(repository, "create_draft_with_expiry_check", create_draft_with_expiry_check)

    def get_open_draft(user_id):
        if state.draft is not None and state.draft.status == "OPEN" and state.draft.userId == user_id:
            return state.draft
        return None

    monkeypatch.setattr(repository, "get_open_draft", get_open_draft)

    def get_draft(user_id, draft_id):
        if state.draft is not None and state.draft.id == draft_id and state.draft.userId == user_id:
            return state.draft
        return None

    monkeypatch.setattr(repository, "get_draft", get_draft)

    def expire_draft_if_stale(draft):
        if draft.status == "OPEN" and draft.expiresAt < datetime.now(timezone.utc):
            state.draft = SimpleNamespace(**{**draft.__dict__, "status": "EXPIRED"})
            return state.draft
        return draft

    monkeypatch.setattr(repository, "expire_draft_if_stale", expire_draft_if_stale)

    def update_draft(draft_id, data):
        current = dict(state.draft.__dict__)
        for key, value in data.items():
            if isinstance(value, dict) and "increment" in value:
                current[key] = current.get(key, 0) + value["increment"]
            else:
                current[key] = value
        state.draft = SimpleNamespace(**current)
        return state.draft

    monkeypatch.setattr(repository, "update_draft", update_draft)

    def create_draft_item(draft_id, item_data):
        item = item_from_payload(state, item_data)
        state.draft = SimpleNamespace(
            **{**state.draft.__dict__, "items": state.draft.items + [item]}
        )
        return item

    monkeypatch.setattr(repository, "create_draft_item", create_draft_item)

    def get_draft_item(user_id, draft_id, item_id):
        draft = get_draft(user_id, draft_id)
        if draft is None:
            return None
        return next((i for i in draft.items if i.id == item_id), None)

    monkeypatch.setattr(repository, "get_draft_item", get_draft_item)

    def update_draft_item(item_id, data):
        items = list(state.draft.items)
        for idx, item in enumerate(items):
            if item.id == item_id:
                merged = dict(item.__dict__)
                merged.update(data)
                merged["food"] = item.food  # `data` carries foodId, not the relation
                items[idx] = SimpleNamespace(**merged)
                state.draft = SimpleNamespace(**{**state.draft.__dict__, "items": items})
                return items[idx]
        raise AssertionError("item not found in fake repository")

    monkeypatch.setattr(repository, "update_draft_item", update_draft_item)

    def delete_draft_item(item_id):
        items = [i for i in state.draft.items if i.id != item_id]
        state.draft = SimpleNamespace(**{**state.draft.__dict__, "items": items})

    monkeypatch.setattr(repository, "delete_draft_item", delete_draft_item)

    monkeypatch.setattr(repository, "get_idempotency_record", lambda key: state.idempotency.get(key))

    def save_idempotency_record(key, user_id, request_hash, response_body, status_code, expires_at):
        state.idempotency[key] = SimpleNamespace(
            key=key,
            userId=user_id,
            requestHash=request_hash,
            responseBody=response_body,
            statusCode=status_code,
            expiresAt=expires_at,
        )

    monkeypatch.setattr(repository, "save_idempotency_record", save_idempotency_record)

    # -- assistant.repository: chat messages (Chunk 2b) ----------------------

    def create_chat_message(session_id, user_id, data):
        message = SimpleNamespace(
            id="msg-{}".format(len(state.messages) + 1),
            sessionId=session_id,
            userId=user_id,
            createdAt=datetime.now(timezone.utc),
            **data,
        )
        state.messages.append(message)
        return message

    monkeypatch.setattr(repository, "create_chat_message", create_chat_message)

    def find_cached_message(user_id, normalized_hash):
        for message in reversed(state.messages):
            if (
                message.userId == user_id
                and message.role == "USER"
                and message.normalizedHash == normalized_hash
                and message.parseSnapshot is not None
            ):
                return message
        return None

    monkeypatch.setattr(repository, "find_cached_message", find_cached_message)

    # -- meals.repository (confirm -> LoggedMeal handoff) --------------------

    monkeypatch.setattr(meals_repository, "get_food", lambda food_id: state.foods.get(food_id))
    monkeypatch.setattr(
        meals_repository, "search_foods", lambda query, **kw: list(state.foods.values())
    )
    monkeypatch.setattr(
        meals_repository, "get_composite_foods", lambda: list(state.composites.values())
    )

    def file_food_miss(raw_text, locale=""):
        state.food_misses.append(raw_text)

    monkeypatch.setattr(meals_repository, "file_food_miss", file_food_miss)

    def create_logged_meal(user_id, meal_data, items_data):
        state.create_logged_meal_calls += 1
        items = [
            SimpleNamespace(
                id="lmi-{}".format(i),
                foodId=item["foodId"],
                food=state.foods[item["foodId"]],
                **{k: v for k, v in item.items() if k != "foodId"},
            )
            for i, item in enumerate(items_data)
        ]
        meal = SimpleNamespace(
            id="meal-{}".format(state.create_logged_meal_calls),
            userId=user_id,
            loggedAt=datetime.now(timezone.utc),
            items=items,
            **meal_data,
        )
        state.logged_meals.append(meal)
        return meal

    monkeypatch.setattr(meals_repository, "create_logged_meal", create_logged_meal)
    monkeypatch.setattr(
        meals_repository, "list_logged_meals", lambda user_id, **kw: state.logged_meals
    )

    return state


def create_lunch_draft(seam, **item_overrides):
    seam.foods.setdefault("food-rice", make_food())
    item = dict(foodId="food-rice", quantity=200.0, unit="g")
    item.update(item_overrides)
    return services.create_draft("user-1", {"name": "Lunch", "slot": "LUNCH", "items": [item]})


def make_egg_food(**overrides):
    fields = dict(
        id="food-egg",
        name="Boiled Egg",
        source="CALORYX_CURATED",
        defaultState="COOKED",
        rawToCookedYield=None,
        caloriesKcalPer100g=155.0,
        proteinGPer100g=13.0,
        carbsGPer100g=1.1,
        fatGPer100g=11.0,
        fiberGPer100g=0.0,
        servingUnits=[serving_unit("piece", 50.0, "COUNTABLE")],
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def make_component(food, ratio, state="UNSPECIFIED", prep=None, is_optional=False):
    return SimpleNamespace(
        id="component-{}".format(food.id),
        foodId=food.id,
        food=food,
        ratioOfServing=ratio,
        state=state,
        prep=prep,
        isOptional=is_optional,
    )


def make_composite(**overrides):
    fields = dict(
        id="composite-1",
        name="Egg Fried Rice",
        aliases=["egg rice"],
        servingGrams=250.0,
        servingLabel="1 plate",
        isCurated=True,
        components=[],
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


# -- create_draft --------------------------------------------------------


def test_create_draft_computes_totals_from_items(seam):
    payload = create_lunch_draft(seam)
    assert payload["totals"]["caloriesKcal"] == 260  # 130 kcal/100g * 200g
    assert payload["parseTier"] == "MANUAL"
    assert payload["confidence"] == 1.0
    assert payload["version"] == 1
    assert payload["status"] == "OPEN"
    assert payload["items"][0]["perGram"]["kcal"] == pytest.approx(1.3)


def test_create_draft_infers_slot_from_local_hour_when_slot_omitted(seam):
    seam.foods["food-rice"] = make_food()
    payload = services.create_draft(
        "user-1",
        {"name": "Breakfast", "items": [{"foodId": "food-rice", "quantity": 100.0, "unit": "g"}], "localHour": 8},
    )
    assert payload["slot"] == "BREAKFAST"


def test_create_draft_raises_when_one_is_already_open(seam):
    first = create_lunch_draft(seam)
    with pytest.raises(OpenDraftExistsError) as exc_info:
        services.create_draft(
            "user-1",
            {"name": "Snack", "slot": "SNACK", "items": [{"foodId": "food-rice", "quantity": 50.0, "unit": "g"}]},
        )
    assert exc_info.value.details["draft"]["id"] == first["id"]


def test_create_draft_lazily_expires_a_stale_open_draft_then_succeeds(seam):
    seam.foods["food-rice"] = make_food()
    seam.draft = make_draft(
        [make_item(seam.foods["food-rice"])], status="OPEN", expiresAt=REAL_PAST
    )

    payload = create_lunch_draft(seam, quantity=100.0)

    assert payload["status"] == "OPEN"
    assert payload["totals"]["caloriesKcal"] == 130  # the new draft, not the stale one's totals


def test_create_draft_raises_food_not_found(seam):
    with pytest.raises(NotFoundError) as exc_info:
        services.create_draft(
            "user-1",
            {"name": "L", "slot": "LUNCH", "items": [{"foodId": "missing", "quantity": 100.0, "unit": "g"}]},
        )
    assert exc_info.value.code == "food_not_found"


# -- fetch_draft ---------------------------------------------------------


def test_fetch_draft_returns_the_same_shape_create_returned(seam):
    created = create_lunch_draft(seam)
    assert services.fetch_draft("user-1", created["id"]) == created


def test_fetch_draft_raises_not_found_for_another_users_draft(seam):
    created = create_lunch_draft(seam)
    with pytest.raises(NotFoundError):
        services.fetch_draft("someone-else", created["id"])


def test_fetch_draft_lazily_expires_a_stale_draft(seam):
    seam.foods["food-rice"] = make_food()
    seam.draft = make_draft(
        [make_item(seam.foods["food-rice"])], status="OPEN", expiresAt=REAL_PAST
    )
    payload = services.fetch_draft("user-1", seam.draft.id)
    assert payload["status"] == "EXPIRED"


# -- update_draft ----------------------------------------------------------


def test_update_draft_changes_name_and_slot_and_bumps_version(seam):
    created = create_lunch_draft(seam)
    updated = services.update_draft(
        "user-1", created["id"], {"name": "Dinner", "slot": "DINNER", "version": created["version"]}
    )
    assert updated["name"] == "Dinner"
    assert updated["slot"] == "DINNER"
    assert updated["version"] == created["version"] + 1


def test_update_draft_raises_version_conflict_on_stale_version(seam):
    created = create_lunch_draft(seam)
    with pytest.raises(DraftVersionConflictError) as exc_info:
        services.update_draft("user-1", created["id"], {"name": "X", "version": created["version"] + 5})
    assert exc_info.value.details["draft"]["id"] == created["id"]


def test_mutating_a_confirmed_draft_raises_draft_not_open(seam):
    created = create_lunch_draft(seam)
    services.confirm_draft("user-1", created["id"], "idem-1", created["version"])
    with pytest.raises(DraftNotOpenError):
        services.update_draft("user-1", created["id"], {"name": "X", "version": created["version"] + 1})


# -- draft items (Adjust Portion persistence) ---------------------------


def test_add_draft_item_recomputes_totals_and_bumps_version(seam):
    created = create_lunch_draft(seam)
    seam.foods["food-egg"] = make_food(
        id="food-egg",
        name="Boiled Egg",
        rawToCookedYield=None,
        caloriesKcalPer100g=155.0,
        proteinGPer100g=13.0,
        carbsGPer100g=1.1,
        fatGPer100g=11.0,
        fiberGPer100g=0.0,
        servingUnits=[serving_unit("piece", 50.0, "COUNTABLE")],
    )

    updated = services.add_draft_item(
        "user-1", created["id"], {"foodId": "food-egg", "quantity": 1.0, "unit": "piece", "version": created["version"]}
    )

    assert len(updated["items"]) == 2
    assert updated["version"] == created["version"] + 1
    egg = next(i for i in updated["items"] if i["foodName"] == "Boiled Egg")
    assert egg["grams"] == 50.0
    assert egg["caloriesKcal"] == round_int(155.0 * 0.5)
    assert updated["totals"]["caloriesKcal"] == round_int(260.0 + 77.5)


def test_add_draft_item_raises_food_not_found(seam):
    created = create_lunch_draft(seam)
    with pytest.raises(NotFoundError) as exc_info:
        services.add_draft_item(
            "user-1", created["id"], {"foodId": "missing", "quantity": 1.0, "unit": "g", "version": created["version"]}
        )
    assert exc_info.value.code == "food_not_found"


def test_update_draft_item_changes_quantity_and_keeps_default_grams(seam):
    created = create_lunch_draft(seam, quantity=100.0)
    item_id = created["items"][0]["id"]

    updated = services.update_draft_item(
        "user-1", created["id"], item_id, {"quantity": 300.0, "version": created["version"]}
    )

    item = updated["items"][0]
    assert item["quantity"] == 300.0
    assert item["grams"] == 300.0
    assert item["caloriesKcal"] == round_int(130.0 * 3)
    assert item["defaultGrams"] == 100.0  # fixed at creation, unmoved by the edit
    assert updated["version"] == created["version"] + 1


def test_update_draft_item_raises_not_found_for_a_missing_item(seam):
    created = create_lunch_draft(seam)
    with pytest.raises(NotFoundError) as exc_info:
        services.update_draft_item(
            "user-1", created["id"], "no-such-item", {"quantity": 50.0, "version": created["version"]}
        )
    assert exc_info.value.code == "draft_item_not_found"


def test_delete_draft_item_recomputes_totals_to_zero_when_last_item_removed(seam):
    created = create_lunch_draft(seam)
    item_id = created["items"][0]["id"]

    updated = services.delete_draft_item("user-1", created["id"], item_id, created["version"])

    assert updated["items"] == []
    assert updated["totals"]["caloriesKcal"] == 0


# -- discard ----------------------------------------------------------------


def test_discard_draft_sets_status_discarded(seam):
    created = create_lunch_draft(seam)
    updated = services.discard_draft("user-1", created["id"], created["version"])
    assert updated["status"] == "DISCARDED"


# -- confirm (§9, §12.1, §12.5) ------------------------------------------


def test_confirm_draft_creates_a_logged_meal_and_marks_the_draft_confirmed(seam):
    created = create_lunch_draft(seam)

    response = services.confirm_draft("user-1", created["id"], "idem-1", created["version"])

    assert response["loggedMeal"]["source"] == "CHAT_AI"
    assert response["loggedMeal"]["totals"]["caloriesKcal"] == 260
    assert seam.draft.status == "CONFIRMED"
    assert seam.create_logged_meal_calls == 1


def test_confirm_draft_replay_with_the_same_key_and_body_returns_the_same_response(seam):
    created = create_lunch_draft(seam)
    first = services.confirm_draft("user-1", created["id"], "idem-1", created["version"])
    second = services.confirm_draft("user-1", created["id"], "idem-1", created["version"])

    assert second == first
    assert seam.create_logged_meal_calls == 1  # not double-logged


def test_confirm_draft_replay_with_the_same_key_but_different_content_is_rejected(seam):
    created = create_lunch_draft(seam)
    services.confirm_draft("user-1", created["id"], "idem-1", created["version"])

    other = services.create_draft(
        "user-1",
        {"name": "Snack", "slot": "SNACK", "items": [{"foodId": "food-rice", "quantity": 50.0, "unit": "g"}]},
    )
    with pytest.raises(IdempotencyKeyReuseError):
        services.confirm_draft("user-1", other["id"], "idem-1", other["version"])


def test_confirm_draft_raises_when_the_draft_is_already_confirmed(seam):
    created = create_lunch_draft(seam)
    services.confirm_draft("user-1", created["id"], "idem-1", created["version"])
    with pytest.raises(DraftNotOpenError):
        services.confirm_draft("user-1", created["id"], "idem-2", created["version"] + 1)


def test_confirm_draft_raises_on_version_mismatch(seam):
    created = create_lunch_draft(seam)
    with pytest.raises(DraftVersionConflictError):
        services.confirm_draft("user-1", created["id"], "idem-1", created["version"] + 99)


def test_confirm_draft_includes_todays_totals(seam):
    created = create_lunch_draft(seam)
    response = services.confirm_draft("user-1", created["id"], "idem-1", created["version"])
    assert response["dailyTotals"]["caloriesKcal"] == 260


# -- send_message (Chunk 2b: §7, §7.5, §12.6) --------------------------------


def send(seam, content, client_message_id="m1", **kw):
    seam.foods.setdefault("food-rice", make_food())
    return services.send_message(
        "user-1", {"clientMessageId": client_message_id, "content": content, **kw}
    )


def test_send_message_short_circuits_on_a_greeting_and_persists_both_messages(seam):
    response = send(seam, "hey there")

    assert response["tier"] == "PRECLASSIFIER"
    assert response["intent"] == "OTHER"
    assert response["draft"] is None
    assert len(seam.messages) == 2
    user_msg, assistant_msg = seam.messages
    assert (user_msg.role, assistant_msg.role) == ("USER", "ASSISTANT")
    assert user_msg.normalizedHash is None  # nothing worth caching for a greeting


def test_send_message_with_no_food_identified_creates_no_draft(seam):
    response = send(seam, "what a lovely day")
    assert response["draft"] is None
    assert response["intent"] == "OTHER"
    assert response["unconsumedText"] == ["what a lovely day"]


def test_send_message_creates_a_draft_with_mixed_resolved_and_unresolved_items(seam):
    response = send(seam, "200g rice and 50g xyzzyplonk")

    assert response["tier"] == "PARSER"
    assert response["intent"] == "LOG_NEW"
    items = response["draft"]["items"]
    assert len(items) == 2
    resolved = next(i for i in items if i["resolution"] == "RESOLVED")
    unresolved = next(i for i in items if i["resolution"] == "UNRESOLVED")
    assert resolved["foodName"] == "Cooked White Rice"
    assert unresolved["foodId"] is None
    assert unresolved["rawText"] == "50g xyzzyplonk"
    # Confidence is a coverage ratio (1 of 2 phrases resolved), not the full
    # §7.2 formula (Chunk 4).
    assert response["draft"]["confidence"] == pytest.approx(0.5)


def test_send_message_applies_yield_conversion_for_a_raw_stated_item(seam):
    """Regression: a plain (non-composite) item stated in a state other than
    its food's default must still serialize with correctly yield-converted
    nutrition - not just the composite-expansion path exercised elsewhere.
    Rice defaults to COOKED with a 3.0 yield, so 100g stated raw is a 300g
    cooked-equivalent basis for the nutrition lookup."""
    response = send(seam, "100g raw rice")

    item = response["draft"]["items"][0]
    assert item["state"] == "RAW"
    assert item["grams"] == pytest.approx(100.0)
    expected = item_nutrition(NutrientVector(130.0, 2.7, 28.2, 0.3, 0.4), 300.0)
    assert item["caloriesKcal"] == round_int(expected.calories_kcal)


def test_send_message_t0_cache_hit_skips_the_grammar(seam):
    seam.foods["food-rice"] = make_food()
    content = "200g rice"
    seam.messages.append(
        SimpleNamespace(
            id="msg-old",
            userId="user-1",
            role="USER",
            normalizedHash=hash_normalized(normalize_text(content)),
            parseSnapshot={
                "name": "Lunch — Rice",
                "slot": "LUNCH",
                "items": [{"foodId": "food-rice", "quantity": 200.0, "unit": "g", "state": None}],
            },
        )
    )

    response = send(seam, content)

    assert response["tier"] == "CACHE"
    assert response["intent"] == "LOG_NEW"
    assert response["draft"]["totals"]["caloriesKcal"] == 260


def test_send_message_edit_item_updates_the_open_draft(seam):
    create_lunch_draft(seam)  # 200g rice
    response = send(seam, "rice was actually 100g", client_message_id="m2")

    assert response["intent"] == "EDIT_ITEM"
    item = response["draft"]["items"][0]
    assert item["grams"] == 100.0
    assert item["caloriesKcal"] == round_int(130.0)


def test_send_message_remove_item_deletes_from_the_open_draft(seam):
    create_lunch_draft(seam)
    response = send(seam, "remove the rice", client_message_id="m2")

    assert response["intent"] == "REMOVE_ITEM"
    assert response["draft"]["items"] == []


def test_send_message_add_item_via_edit_grammar_appends_to_the_open_draft(seam):
    create_lunch_draft(seam)
    seam.foods["food-egg"] = make_egg_food()

    response = send(seam, "add 1 piece boiled egg", client_message_id="m2")

    assert response["intent"] == "ADD_ITEM"
    assert len(response["draft"]["items"]) == 2
    egg = next(i for i in response["draft"]["items"] if i["foodName"] == "Boiled Egg")
    assert egg["grams"] == 50.0


def test_send_message_set_slot_updates_the_open_draft(seam):
    create_lunch_draft(seam)
    response = send(seam, "this was breakfast", client_message_id="m2")

    assert response["intent"] == "SET_SLOT"
    assert response["draft"]["slot"] == "BREAKFAST"


def test_send_message_edit_with_no_confident_target_is_ambiguous(seam):
    create_lunch_draft(seam)  # only "Cooked White Rice" on the draft
    response = send(seam, "xyzzyplonk was actually 100g", client_message_id="m2")

    assert response["draft"]["items"][0]["grams"] == 200.0  # unchanged
    assert response["needsClarification"]["reason"] == "ambiguous_target"


def test_send_message_edit_with_two_close_matches_is_ambiguous(seam):
    chicken_breast = make_food(
        id="food-chicken-breast", name="Grilled Chicken Breast", servingUnits=[]
    )
    chicken_curry = make_food(id="food-chicken-curry", name="Chicken Curry", servingUnits=[])
    seam.draft = make_draft(
        [make_item(chicken_breast, id="item-1"), make_item(chicken_curry, id="item-2")],
        caloriesKcal=0.0,
        proteinG=0.0,
        carbsG=0.0,
        fatG=0.0,
    )

    response = send(seam, "chicken was actually 150g", client_message_id="m2")

    assert response["needsClarification"]["reason"] == "ambiguous_target"
    assert set(response["needsClarification"]["candidates"]) == {
        "Grilled Chicken Breast",
        "Chicken Curry",
    }


def test_send_message_new_meal_while_draft_open_asks_add_or_new(seam):
    create_lunch_draft(seam)
    seam.foods["food-egg"] = make_egg_food()

    response = send(seam, "1 piece boiled egg", client_message_id="m2")

    assert response["needsClarification"] == {"reason": "open_draft", "candidates": ["ADD", "NEW"]}
    assert len(response["draft"]["items"]) == 1  # untouched


def test_send_message_on_open_draft_add_appends_the_new_items(seam):
    create_lunch_draft(seam)
    seam.foods["food-egg"] = make_egg_food()

    response = send(seam, "1 piece boiled egg", client_message_id="m2", onOpenDraft="ADD")

    assert response["intent"] == "ADD_ITEM"
    assert len(response["draft"]["items"]) == 2


def test_send_message_on_open_draft_new_discards_the_old_one(seam):
    old = create_lunch_draft(seam)
    seam.foods["food-egg"] = make_egg_food()

    response = send(seam, "1 piece boiled egg", client_message_id="m2", onOpenDraft="NEW")

    assert response["intent"] == "LOG_NEW"
    assert response["draft"]["id"] != old["id"]
    assert len(response["draft"]["items"]) == 1
    assert response["draft"]["items"][0]["foodName"] == "Boiled Egg"


def test_send_message_idempotency_replay_returns_the_same_response(seam):
    first = send(seam, "200g rice", client_message_id="m1")
    second = send(seam, "200g rice", client_message_id="m1")
    assert second == first
    # No second draft/message pair was created on replay.
    assert len(seam.messages) == 2


def test_send_message_idempotency_key_reuse_with_different_content_is_rejected(seam):
    send(seam, "200g rice", client_message_id="m1")
    with pytest.raises(IdempotencyKeyReuseError):
        send(seam, "200g chicken", client_message_id="m1")


# -- composite foods (Chunk 3, §7.6) -----------------------------------------


def test_send_message_expands_a_composite_dish_into_its_components(seam):
    rice = make_food()
    egg = make_egg_food()
    seam.foods["food-rice"] = rice
    seam.foods["food-egg"] = egg
    seam.composites["composite-1"] = make_composite(
        components=[make_component(rice, 0.7, state="COOKED"), make_component(egg, 0.3, state="COOKED")]
    )

    response = services.send_message(
        "user-1", {"clientMessageId": "m1", "content": "1 plate egg fried rice"}
    )

    assert response["intent"] == "LOG_NEW"
    items = response["draft"]["items"]
    assert len(items) == 2
    rice_item = next(i for i in items if i["foodName"] == "Cooked White Rice")
    egg_item = next(i for i in items if i["foodName"] == "Boiled Egg")

    assert rice_item["grams"] == pytest.approx(175.0)  # 250 * 0.7
    assert egg_item["grams"] == pytest.approx(75.0)  # 250 * 0.3
    assert rice_item["matchBand"] is None  # the dish was matched, not this component
    assert "(Egg Fried Rice)" in rice_item["rawText"]

    expected_rice = item_nutrition(NutrientVector(130.0, 2.7, 28.2, 0.3, 0.4), 175.0)
    expected_egg = item_nutrition(NutrientVector(155.0, 13.0, 1.1, 11.0, 0.0), 75.0)
    assert rice_item["caloriesKcal"] == round_int(expected_rice.calories_kcal)
    assert egg_item["caloriesKcal"] == round_int(expected_egg.calories_kcal)
    assert response["draft"]["confidence"] == 1.0  # both components resolved


def test_send_message_matches_a_composite_by_alias(seam):
    rice = make_food()
    egg = make_egg_food()
    seam.foods["food-rice"] = rice
    seam.foods["food-egg"] = egg
    seam.composites["composite-1"] = make_composite(
        components=[make_component(rice, 0.7), make_component(egg, 0.3)]
    )

    response = services.send_message(
        "user-1", {"clientMessageId": "m1", "content": "1 plate egg rice"}
    )

    assert len(response["draft"]["items"]) == 2


def test_a_single_shared_word_does_not_trigger_composite_expansion(seam):
    """The regression this chunk's whole design hinges on: partial-ratio
    fuzzy matching would score "dal" a perfect match against "Dal Chawal"
    (§7.6's matching is an exact name/alias lookup precisely to avoid this) -
    a bare "dal" mention must resolve to the plain food, not the composite."""
    dal = make_food(
        id="food-dal",
        name="Toor Dal, Cooked",
        rawToCookedYield=None,
        servingUnits=[serving_unit("katori", 150.0)],
    )
    rice = make_food()
    seam.foods["food-dal"] = dal
    seam.foods["food-rice"] = rice
    seam.composites["composite-dal-chawal"] = make_composite(
        id="composite-dal-chawal",
        name="Dal Chawal",
        aliases=["dal rice"],
        servingGrams=300.0,
        components=[make_component(dal, 0.5, state="COOKED"), make_component(rice, 0.5, state="COOKED")],
    )

    response = services.send_message("user-1", {"clientMessageId": "m1", "content": "1 katori dal"})

    items = response["draft"]["items"]
    assert len(items) == 1
    assert items[0]["foodName"] == "Toor Dal, Cooked"


def test_composite_component_state_differing_from_default_applies_yield_conversion(seam):
    rice = make_food()  # defaultState=COOKED, rawToCookedYield=3.0
    seam.foods["food-rice"] = rice
    # Deliberately not named starting with "raw"/"cooked"/etc - the T1 grammar
    # strips a leading state/prep word from the phrase *before* composite
    # matching ever sees the remaining text (§7.5), so a dish name starting
    # with one of those words would never resolve here. That's a grammar
    # property, not something this test is checking.
    seam.composites["composite-1"] = make_composite(
        name="Farmhouse Rice Bowl",
        servingGrams=100.0,
        components=[make_component(rice, 1.0, state="RAW")],
    )

    response = services.send_message(
        "user-1", {"clientMessageId": "m1", "content": "1 plate farmhouse rice bowl"}
    )

    item = response["draft"]["items"][0]
    # The as-curated (raw) mass is what's stored - not the cooked-equivalent
    # basis used for the nutrition lookup (mirrors how a stated quantity is
    # never overwritten by its yield-converted basis anywhere else, §8).
    assert item["grams"] == pytest.approx(100.0)
    assert item["state"] == "RAW"
    expected = item_nutrition(NutrientVector(130.0, 2.7, 28.2, 0.3, 0.4), 300.0)
    assert item["caloriesKcal"] == round_int(expected.calories_kcal)


def test_send_message_files_a_miss_for_the_food_text_of_an_unresolved_phrase(seam):
    services.send_message("user-1", {"clientMessageId": "m1", "content": "50g xyzzyplonk"})
    assert seam.food_misses == ["xyzzyplonk"]
