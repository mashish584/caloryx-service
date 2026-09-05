"""Meal Assistant draft use cases - PRD §9, §12.1, §12.2, §12.5. Prisma is
never reached; `assistant.repository` and `meals.repository` are the seams
(see tests/test_meals_services.py for the pattern this follows).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from django.test import override_settings

from assistant import repository, services
from chatparser import hash_normalized, normalize_text
from common.exceptions import (
    DraftNotOpenError,
    DraftVersionConflictError,
    EstimatedDishNotEditableError,
    IdempotencyKeyReuseError,
    NotFoundError,
    OpenDraftExistsError,
    OperationExpiredError,
    StaleOperationError,
)
from engine.rounding import round_int
from llm import LLMCallError, LLMConfigurationError, LLMResponse
from meals import repository as meals_repository
from onboarding import repository as onboarding_repository
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
        defaultServingGrams=None,
        category=None,
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


def make_estimated_dish_item(**overrides):
    fields = dict(
        id="item-1",
        draftId="draft-1",
        resolution="ESTIMATED_DISH",
        foodId=None,
        food=None,
        rawText="estimated dish",
        quantity=300.0,
        unit="g",
        grams=300.0,
        state="UNSPECIFIED",
        defaultGrams=300.0,
        prep=None,
        sizeQualifier=None,
        quantitySource=None,
        massSource=None,
        matchScore=None,
        matchBand=None,
        dishCategory="SPICED_CURRY",
        kcalLow=300.0,
        kcalHigh=550.0,
        kcalMidpoint=425.0,
        profileVersion=1,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def item_from_payload(state, item_data):
    """Builds a fake `MealDraftItem` row from a service-produced payload dict
    (RESOLVED - has `foodId`; ESTIMATED_DISH - has `dishCategory`; or
    UNRESOLVED - neither), the way the real repository's
    `create_draft_item`/`create_draft_with_expiry_check` would."""
    item_id = "item-{}".format(state.next_item_id)
    state.next_item_id += 1
    if item_data.get("resolution") == "UNRESOLVED":
        return make_unresolved_item(id=item_id, **item_data)
    if item_data.get("resolution") == "ESTIMATED_DISH":
        return make_estimated_dish_item(id=item_id, **item_data)
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
        parse_events=[],
        serving_preferences={},
        record_serving_observation_calls=[],
        quota_counters={},
        global_cache={},
        global_cache_hit_calls=[],
        draft_operations=[],
        dish_category_profiles={},
        profiles={},
        gamification_suppressed_sessions=[],
        catalog_version=1,
        circuit_breaker=SimpleNamespace(consecutiveFailures=0, openedAt=None),
    )

    monkeypatch.setattr(onboarding_repository, "get_profile", lambda user_id, **kw: state.profiles.get(user_id))

    # -- assistant.repository ------------------------------------------------

    monkeypatch.setattr(
        repository, "get_or_create_today_session", lambda user_id: SimpleNamespace(id="session-1")
    )

    def set_session_gamification_suppressed(session_id):
        state.gamification_suppressed_sessions.append(session_id)

    monkeypatch.setattr(
        repository, "set_session_gamification_suppressed", set_session_gamification_suppressed
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

    # -- assistant.repository: parse telemetry (Chunk 4a) --------------------

    def create_parse_event(user_id, data):
        event = SimpleNamespace(
            id="parse-event-{}".format(len(state.parse_events) + 1),
            userId=user_id,
            createdAt=datetime.now(timezone.utc),
            **data,
        )
        state.parse_events.append(event)
        return event

    monkeypatch.setattr(repository, "create_parse_event", create_parse_event)

    # -- assistant.repository: quantity-resolution ladder (Chunk 4b) ---------

    def get_serving_preference(user_id, food_id, pref_state):
        return state.serving_preferences.get((user_id, food_id, pref_state))

    monkeypatch.setattr(repository, "get_serving_preference", get_serving_preference)

    def record_serving_observation(user_id, food_id, pref_state, grams):
        import statistics as statistics_module

        state.record_serving_observation_calls.append((user_id, food_id, pref_state, grams))
        key = (user_id, food_id, pref_state)
        existing = state.serving_preferences.get(key)
        recent = (list(existing.recentGrams) if existing else []) + [grams]
        recent = recent[-10:]
        pref = SimpleNamespace(
            id="pref-{}".format(len(state.serving_preferences) + 1) if existing is None else existing.id,
            userId=user_id,
            foodId=food_id,
            state=pref_state,
            recentGrams=recent,
            medianGrams=statistics_module.median(recent),
            observations=len(recent),
        )
        state.serving_preferences[key] = pref
        return pref

    monkeypatch.setattr(repository, "record_serving_observation", record_serving_observation)

    # -- assistant.repository: AI quota (Chunk 4c) ---------------------------

    def get_quota_counter(user_id):
        return state.quota_counters.get(user_id)

    monkeypatch.setattr(repository, "get_quota_counter", get_quota_counter)

    def try_consume_quota(user_id, limit, window):
        now = datetime.now(timezone.utc)
        counter = state.quota_counters.get(user_id)
        if counter is None or counter.windowStart <= now - window:
            counter = SimpleNamespace(userId=user_id, windowStart=now, count=1)
            state.quota_counters[user_id] = counter
            return counter, True
        if counter.count >= limit:
            return counter, False
        counter.count += 1
        return counter, True

    monkeypatch.setattr(repository, "try_consume_quota", try_consume_quota)

    # -- assistant.repository: cost circuit breaker (Chunk 8d) ---------------

    def circuit_breaker_is_open(cooldown):
        row = state.circuit_breaker
        if row.openedAt is None:
            return False
        return datetime.now(timezone.utc) - row.openedAt < cooldown

    monkeypatch.setattr(repository, "circuit_breaker_is_open", circuit_breaker_is_open)

    def record_llm_call_failure(threshold):
        state.circuit_breaker.consecutiveFailures += 1
        if state.circuit_breaker.consecutiveFailures >= threshold:
            state.circuit_breaker.openedAt = datetime.now(timezone.utc)
        return state.circuit_breaker

    monkeypatch.setattr(repository, "record_llm_call_failure", record_llm_call_failure)

    def record_llm_call_success():
        state.circuit_breaker.consecutiveFailures = 0
        state.circuit_breaker.openedAt = None
        return state.circuit_breaker

    monkeypatch.setattr(repository, "record_llm_call_success", record_llm_call_success)

    # -- assistant.repository: L2 global parse cache (Chunk 4c) --------------

    def get_global_cache(normalized_hash):
        return state.global_cache.get(normalized_hash)

    monkeypatch.setattr(repository, "get_global_cache", get_global_cache)

    def save_global_cache(normalized_hash, snapshot):
        entry = SimpleNamespace(normalizedHash=normalized_hash, snapshot=snapshot, hitCount=0)
        state.global_cache[normalized_hash] = entry
        return entry

    monkeypatch.setattr(repository, "save_global_cache", save_global_cache)

    def bump_global_cache_hit(normalized_hash):
        state.global_cache_hit_calls.append(normalized_hash)
        entry = state.global_cache.get(normalized_hash)
        if entry is not None:
            entry.hitCount += 1

    monkeypatch.setattr(repository, "bump_global_cache_hit", bump_global_cache_hit)

    # -- assistant.repository: draft operation audit log (Chunk 5a) ----------

    def record_draft_operation(draft_id, op, payload, version):
        entry = SimpleNamespace(draftId=draft_id, op=op, actor="user", payload=payload, version=version)
        state.draft_operations.append(entry)
        return entry

    monkeypatch.setattr(repository, "record_draft_operation", record_draft_operation)

    # -- meals.repository (confirm -> LoggedMeal handoff) --------------------

    monkeypatch.setattr(meals_repository, "get_food", lambda food_id: state.foods.get(food_id))
    monkeypatch.setattr(
        meals_repository, "search_foods", lambda query, **kw: list(state.foods.values())
    )
    monkeypatch.setattr(
        meals_repository, "get_composite_foods", lambda: list(state.composites.values())
    )
    monkeypatch.setattr(meals_repository, "get_catalog_version", lambda: state.catalog_version)

    def file_food_miss(raw_text, locale=""):
        state.food_misses.append(raw_text)

    monkeypatch.setattr(meals_repository, "file_food_miss", file_food_miss)
    monkeypatch.setattr(
        meals_repository,
        "get_dish_category_profile",
        lambda category: state.dish_category_profiles.get(category),
    )

    def create_logged_meal(user_id, meal_data, items_data):
        state.create_logged_meal_calls += 1
        items = []
        for i, item in enumerate(items_data):
            fields = dict(
                id="lmi-{}".format(i),
                foodId=None,
                dishCategory=None,
                kcalLow=None,
                kcalHigh=None,
                profileVersion=None,
                rawText=None,
                food=state.foods.get(item["foodId"]) if "foodId" in item else None,
            )
            fields.update(item)
            items.append(SimpleNamespace(**fields))
        meal_fields = dict(
            id="meal-{}".format(state.create_logged_meal_calls),
            userId=user_id,
            loggedAt=datetime.now(timezone.utc),
            items=items,
        )
        meal_fields.update(meal_data)  # meal_data's own loggedAt (if any) wins
        meal = SimpleNamespace(**meal_fields)
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
    # Chunk 6a: SOCIAL is now distinguished from OTHER (previously every
    # T-1 short-circuit, greeting included, was tagged OTHER).
    assert response["intent"] == "SOCIAL"
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
            normalizedHash=services._versioned_cache_key(hash_normalized(normalize_text(content))),
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


# -- T2 escalation (Chunk 4a: §7.1-§7.3, §9, §12.4, §12.8) -------------------


def llm_envelope(
    intent="LOG_NEW", slot=None, meal_name=None, items=None, target_ref=None, dish_category=None
):
    return {
        "intent": intent,
        "targetRef": target_ref,
        "slot": slot,
        "mealName": meal_name,
        "dishCategory": dish_category,
        "items": items if items is not None else [],
    }


def llm_item(food, quantity=None, unit=None, state=None, prep=None, confidence=0.9, size_qualifier=None):
    return {
        "food": food,
        "quantity": quantity,
        "unit": unit,
        "state": state,
        "prep": prep,
        "sizeQualifier": size_qualifier,
        "confidence": confidence,
    }


def stub_llm_response(envelope, prompt_tokens=100, output_tokens=50, model="gpt-4o-mini"):
    return LLMResponse(
        raw_envelope=envelope,
        prompt_tokens=prompt_tokens,
        output_tokens=output_tokens,
        latency_ms=42,
        model=model,
    )


def stub_call_small_model(monkeypatch, result=None, exc=None):
    """`result`/`exc` may be a single value or a list consumed call-by-call
    (only the list form is used today, but keeping the shape symmetric costs
    nothing and matches how a multi-call test would extend it)."""
    calls = []

    def fake(system_prompt, user_content):
        calls.append((system_prompt, user_content))
        if exc is not None:
            raise exc
        return result

    monkeypatch.setattr(services, "call_small_model", fake)
    return calls


def stub_call_large_model(monkeypatch, result=None, exc=None):
    calls = []

    def fake(system_prompt, user_content):
        calls.append((system_prompt, user_content))
        if exc is not None:
            raise exc
        return result

    monkeypatch.setattr(services, "call_large_model", fake)
    return calls


def test_call_llm_sends_redacted_content_but_hashes_the_original(seam, monkeypatch):
    """§12.14 (Chunk 8c): the model never sees raw PII, but the ParseEvent's
    `inputHash` - and everything else keyed on the message - is unaffected."""
    envelope = llm_envelope(items=[llm_item("grilled chicken salad", confidence=0.5)])
    calls = stub_call_small_model(monkeypatch, result=stub_llm_response(envelope))
    content = "grilled chicken salad, call me at 987-654-3210"

    send(seam, content)

    assert len(calls) == 1
    system_prompt, user_content = calls[0]
    assert "987-654-3210" not in user_content
    assert "[redacted-phone]" in user_content
    assert "grilled chicken salad" in user_content  # the food text itself is untouched
    assert seam.parse_events[0].inputHash == hash_normalized(normalize_text(content))


def test_send_message_escalates_to_t2_when_t1_finds_nothing(seam, monkeypatch):
    chicken = make_food(
        id="food-chicken",
        name="Grilled Chicken Breast",
        defaultState="COOKED",
        rawToCookedYield=1.0,
        servingUnits=[],
    )
    seam.foods["food-chicken"] = chicken
    envelope = llm_envelope(
        items=[llm_item("grilled chicken breast", quantity=150, unit="g", state="cooked", prep="grilled", confidence=0.9)]
    )
    calls = stub_call_small_model(monkeypatch, result=stub_llm_response(envelope))

    response = send(seam, "grilled chicken salad with a tahini dressing")

    assert len(calls) == 1  # T1 found zero phrases -> exactly one T2 call
    assert response["tier"] == "LLM_SMALL"
    assert response["intent"] == "LOG_NEW"
    items = response["draft"]["items"]
    assert len(items) == 1
    assert items[0]["foodName"] == "Grilled Chicken Breast"
    assert items[0]["resolution"] == "RESOLVED"
    assert response["draft"]["confidence"] == pytest.approx(1.0)

    assert len(seam.parse_events) == 1
    event = seam.parse_events[0]
    assert event.tier == "LLM_SMALL"
    assert event.intent == "LOG_NEW"
    assert event.model == "gpt-4o-mini"
    assert event.promptTokens == 100
    assert event.outputTokens == 50
    assert event.costMicros == 45  # 100*0.15 + 50*0.6, in micros-per-token terms
    assert event.latencyMs == 42
    assert event.confidence == pytest.approx(0.9)


def test_send_message_t2_item_without_quantity_is_reported_as_unconsumed(seam, monkeypatch):
    envelope = llm_envelope(items=[llm_item("grilled chicken salad", confidence=0.5)])
    stub_call_small_model(monkeypatch, result=stub_llm_response(envelope))

    response = send(seam, "grilled chicken salad with a tahini dressing")

    assert response["draft"] is None
    assert response["tier"] == "LLM_SMALL"
    assert response["intent"] == "OTHER"
    assert "grilled chicken salad" in response["unconsumedText"]


def test_send_message_t2_call_failure_falls_back_gracefully(seam, monkeypatch):
    stub_call_small_model(monkeypatch, exc=LLMCallError("provider timeout"))

    response = send(seam, "grilled chicken salad with a tahini dressing")

    assert response["draft"] is None
    assert response["tier"] == "PARSER"
    assert response["intent"] == "OTHER"
    assert len(seam.parse_events) == 1
    assert seam.parse_events[0].tier == "LLM_SMALL"
    assert seam.parse_events[0].intent == "OTHER"


def test_send_message_t2_envelope_validation_failure_falls_back_gracefully(seam, monkeypatch):
    bad_envelope = llm_envelope(items=[llm_item("chicken", state="sizzling", confidence=0.5)])
    stub_call_small_model(monkeypatch, result=stub_llm_response(bad_envelope))

    response = send(seam, "grilled chicken salad with a tahini dressing")

    assert response["draft"] is None
    assert response["tier"] == "PARSER"
    assert response["intent"] == "OTHER"
    assert len(seam.parse_events) == 1
    assert seam.parse_events[0].intent == "OTHER"
    assert seam.parse_events[0].model == "gpt-4o-mini"  # the call itself succeeded


def test_send_message_t2_other_intent_gets_the_scripted_reply(seam, monkeypatch):
    """Chunk 6a: a T2-classified OTHER (or any of the other 6 non-logging
    intents) now gets its real scripted handler, tagged with the tier that
    actually produced the classification - previously (pre-6a) this fell
    back to the generic PARSER/_NO_FOOD_REPLY fallback since nothing handled
    a non-LOG_NEW envelope intent at all."""
    envelope = llm_envelope(intent="OTHER", items=[])
    stub_call_small_model(monkeypatch, result=stub_llm_response(envelope))

    response = send(seam, "grilled chicken salad with a tahini dressing")

    assert response["draft"] is None
    assert response["tier"] == "LLM_SMALL"
    assert response["intent"] == "OTHER"


def test_send_message_t2_uses_envelope_slot_and_meal_name_when_present(seam, monkeypatch):
    chicken = make_food(id="food-chicken", name="Grilled Chicken Breast", defaultState="COOKED", rawToCookedYield=1.0, servingUnits=[])
    seam.foods["food-chicken"] = chicken
    envelope = llm_envelope(
        slot="BREAKFAST",
        meal_name="Custom Meal Name",
        items=[llm_item("grilled chicken breast", quantity=150, unit="g", confidence=0.9)],
    )
    stub_call_small_model(monkeypatch, result=stub_llm_response(envelope))

    response = send(seam, "grilled chicken salad with a tahini dressing")

    assert response["draft"]["slot"] == "BREAKFAST"
    assert response["draft"]["name"] == "Custom Meal Name"


def test_send_message_t1_partial_match_does_not_call_t2(seam, monkeypatch):
    calls = stub_call_small_model(monkeypatch, result=stub_llm_response(llm_envelope()))

    send(seam, "200g rice and blah")

    assert calls == []


def test_send_message_open_draft_unrecognized_message_calls_t2_for_a_second_opinion(seam, monkeypatch):
    """Superseded by Chunk 5a: an open-draft message matching neither T1
    grammar now gets one T2 call (§7.5) before falling back - previously
    (Chunk 4a) this never escalated at all. A LOG_NEW-classified envelope
    (the default `llm_envelope()`) still isn't actionable on an open draft,
    so the outcome is unchanged even though the call now happens."""
    create_lunch_draft(seam)
    calls = stub_call_small_model(monkeypatch, result=stub_llm_response(llm_envelope()))

    response = send(seam, "what a lovely day", client_message_id="m2")

    assert len(calls) == 1
    assert response["intent"] == "OTHER"


def test_derive_meal_name_picks_the_highest_calorie_resolved_item(seam):
    seam.foods["food-rice"] = make_food(id="food-rice", name="Cooked White Rice")
    seam.foods["food-chicken"] = make_food(id="food-chicken", name="Grilled Chicken Breast")
    items_payload = [
        {"resolution": "RESOLVED", "foodId": "food-rice", "rawText": "100g rice"},
        {"resolution": "RESOLVED", "foodId": "food-chicken", "rawText": "150g chicken"},
        {"resolution": "UNRESOLVED", "rawText": "50g xyzzyplonk"},
    ]
    vectors = [
        NutrientVector(130.0, 2.7, 28.2, 0.3, 0.4),
        NutrientVector(250.0, 40.0, 0.0, 8.0, 0.0),
        NutrientVector(0.0, 0.0, 0.0, 0.0, 0.0),
    ]

    name = services._derive_meal_name(items_payload, vectors, None, "LUNCH")

    assert name == "Lunch — Grilled Chicken Breast"


def test_derive_meal_name_falls_back_when_nothing_resolved():
    name = services._derive_meal_name([], [], None, "SNACK")
    assert name == "Snack meal"


# -- quantity-resolution ladder (Chunk 4b, §5.1.1a) --------------------------


def test_resolve_assumed_grams_prefers_user_history_over_a_stated_size_qualifier(seam):
    food = make_food(id="food-rice", category="GRAIN")
    seam.serving_preferences[("user-1", "food-rice", "COOKED")] = SimpleNamespace(
        recentGrams=[100.0, 120.0, 110.0], medianGrams=110.0, observations=3
    )

    result = services._resolve_assumed_grams(food, "COOKED", "large", "user-1")

    assert result == (110.0, "USER_HISTORY")


def test_resolve_assumed_grams_ignores_history_below_three_observations(seam):
    food = make_food(id="food-rice", category="GRAIN", defaultServingGrams=None)
    seam.serving_preferences[("user-1", "food-rice", "COOKED")] = SimpleNamespace(
        recentGrams=[100.0, 120.0], medianGrams=110.0, observations=2
    )

    grams, mass_source = services._resolve_assumed_grams(food, "COOKED", None, "user-1")

    assert grams == pytest.approx(150.0)  # GRAIN category fallback, no history override
    assert mass_source == "CATEGORY_FALLBACK"


def test_resolve_assumed_grams_uses_canonical_serving_times_size_qualifier(seam):
    food = make_food(id="food-roti", defaultServingGrams=40.0, category="GRAIN")

    grams, mass_source = services._resolve_assumed_grams(food, None, "small", "user-1")

    assert grams == pytest.approx(28.0)  # 40g * 0.7
    assert mass_source == "CATALOG_SERVING"


def test_resolve_assumed_grams_uses_category_fallback_times_size_qualifier(seam):
    food = make_food(id="food-oil", defaultServingGrams=None, category="OIL")

    grams, mass_source = services._resolve_assumed_grams(food, None, "large", "user-1")

    assert grams == pytest.approx(7.0)  # 5g * 1.4
    assert mass_source == "CATEGORY_FALLBACK"


def test_resolve_assumed_grams_returns_none_when_nothing_to_assume(seam):
    food = make_food(id="food-mystery", defaultServingGrams=None, category=None)

    assert services._resolve_assumed_grams(food, None, None, "user-1") is None


def test_send_message_t2_item_without_quantity_resolves_via_the_ladder(seam, monkeypatch):
    chicken = make_food(
        id="food-chicken",
        name="Grilled Chicken Breast",
        defaultState="COOKED",
        rawToCookedYield=1.0,
        servingUnits=[],
        category="PROTEIN",
    )
    seam.foods["food-chicken"] = chicken
    envelope = llm_envelope(items=[llm_item("grilled chicken breast", confidence=0.6)])
    stub_call_small_model(monkeypatch, result=stub_llm_response(envelope))

    response = send(seam, "grilled chicken salad with a tahini dressing")

    assert response["tier"] == "LLM_SMALL"
    assert response["intent"] == "LOG_NEW"
    items = response["draft"]["items"]
    assert len(items) == 1
    item = items[0]
    assert item["resolution"] == "RESOLVED"
    assert item["quantitySource"] == "ASSUMED"
    assert item["massSource"] == "CATEGORY_FALLBACK"
    assert item["grams"] == pytest.approx(100.0)
    # An ASSUMED item is the ladder's own guess, not a real observation.
    assert seam.record_serving_observation_calls == []


def test_send_message_t2_item_without_quantity_applies_a_size_qualifier(seam, monkeypatch):
    oil = make_food(
        id="food-oil",
        name="Olive Oil",
        defaultState="UNSPECIFIED",
        rawToCookedYield=None,
        servingUnits=[],
        category="OIL",
    )
    seam.foods["food-oil"] = oil
    envelope = llm_envelope(items=[llm_item("olive oil", size_qualifier="large", confidence=0.6)])
    stub_call_small_model(monkeypatch, result=stub_llm_response(envelope))

    response = send(seam, "a large drizzle of olive oil on my salad")

    item = response["draft"]["items"][0]
    assert item["grams"] == pytest.approx(7.0)  # 5g OIL fallback * 1.4
    assert item["massSource"] == "CATEGORY_FALLBACK"


def test_send_message_t2_quantity_less_composite_defaults_to_one_serving(seam, monkeypatch):
    rice = make_food()
    chicken = make_food(id="food-chicken", name="Grilled Chicken Breast")
    seam.foods.update({"food-rice": rice, "food-chicken": chicken})
    seam.composites["composite-1"] = make_composite(
        name="Chicken Biryani",
        aliases=["biryani"],
        servingGrams=350.0,
        components=[make_component(rice, 0.65), make_component(chicken, 0.30)],
    )
    envelope = llm_envelope(items=[llm_item("biryani", confidence=0.6)])
    stub_call_small_model(monkeypatch, result=stub_llm_response(envelope))

    response = send(seam, "I had some biryani for lunch today with friends")

    items = response["draft"]["items"]
    assert len(items) == 2
    assert all(i["resolution"] == "RESOLVED" for i in items)
    assert all(i["quantitySource"] == "ASSUMED" for i in items)
    total_grams = sum(i["grams"] for i in items)
    assert total_grams == pytest.approx(350.0 * 0.95)  # 1 serving, ratios sum to 0.95


def test_send_message_t2_quantity_less_item_with_no_ladder_data_stays_unconsumed(seam, monkeypatch):
    mystery = make_food(
        id="food-mystery", name="Mystery Paste", defaultServingGrams=None, category=None, servingUnits=[]
    )
    seam.foods["food-mystery"] = mystery
    envelope = llm_envelope(items=[llm_item("mystery paste", confidence=0.4)])
    stub_call_small_model(monkeypatch, result=stub_llm_response(envelope))

    response = send(seam, "some mystery paste on the side today please")

    assert response["draft"] is None
    assert response["tier"] == "LLM_SMALL"
    assert response["intent"] == "OTHER"
    assert "mystery paste" in response["unconsumedText"]


def test_explicit_structured_create_records_a_serving_observation(seam):
    create_lunch_draft(seam, quantity=200.0)  # 200g rice, food-rice, state COOKED

    assert seam.record_serving_observation_calls == [("user-1", "food-rice", "COOKED", 200.0)]


def test_explicit_t1_text_log_records_a_serving_observation(seam):
    send(seam, "200g rice")

    assert seam.record_serving_observation_calls == [("user-1", "food-rice", "COOKED", 200.0)]


def test_explicit_edit_item_correction_records_a_serving_observation(seam):
    create_lunch_draft(seam)  # 200g rice
    seam.record_serving_observation_calls.clear()  # drop the create's own observation

    send(seam, "rice was actually 100g", client_message_id="m2")

    assert seam.record_serving_observation_calls == [("user-1", "food-rice", "COOKED", 100.0)]


# -- AI quota, T3 escalation, L2 cache (Chunk 4c, §5.1.4, §7.1, §7.4, §12.8) -


def _chicken_food(**overrides):
    fields = dict(
        id="food-chicken",
        name="Grilled Chicken Breast",
        defaultState="COOKED",
        rawToCookedYield=1.0,
        category="PROTEIN",
        servingUnits=[],
    )
    fields.update(overrides)
    return make_food(**fields)


@override_settings(AI_QUOTA_LIMIT=2)
def test_send_message_quota_allows_up_to_the_limit_then_blocks(seam, monkeypatch):
    seam.foods["food-chicken"] = _chicken_food()
    envelope = llm_envelope(
        items=[llm_item("grilled chicken breast", quantity=150, unit="g", confidence=0.9)]
    )
    calls = stub_call_small_model(monkeypatch, result=stub_llm_response(envelope))

    r1 = send(seam, "grilled chicken salad with a tahini dressing", client_message_id="m1")
    seam.draft = None  # each send below is meant to start fresh, no open draft in the way
    r2 = send(seam, "grilled chicken bowl with a spicy dressing", client_message_id="m2")
    seam.draft = None
    r3 = send(seam, "grilled chicken wrap with a herb dressing", client_message_id="m3")

    assert r1["quotaExceeded"] is False
    assert r2["quotaExceeded"] is False
    assert r3["quotaExceeded"] is True
    assert r3["tier"] == "PARSER"
    assert r3["intent"] == "OTHER"
    assert r3["draft"] is None
    assert len(calls) == 2  # the third message never reached the model


def test_send_message_t0_cache_hit_does_not_consume_quota(seam, monkeypatch):
    seam.foods["food-chicken"] = _chicken_food()
    envelope = llm_envelope(
        items=[llm_item("grilled chicken breast", quantity=150, unit="g", confidence=0.9)]
    )
    calls = stub_call_small_model(monkeypatch, result=stub_llm_response(envelope))

    send(seam, "grilled chicken salad with a tahini dressing", client_message_id="m1")
    seam.draft = None  # no open draft in the way of the second, cache-hit send
    response2 = send(seam, "grilled chicken salad with a tahini dressing", client_message_id="m2")

    assert len(calls) == 1  # second send hit the per-user (T0) cache
    assert response2["tier"] == "CACHE"
    assert seam.quota_counters["user-1"].count == 1


def test_versioned_cache_key_changes_with_the_catalog_version(seam):
    base = hash_normalized(normalize_text("200g rice"))
    seam.catalog_version = 1
    key_v1 = services._versioned_cache_key(base)
    seam.catalog_version = 2
    key_v2 = services._versioned_cache_key(base)

    assert key_v1 != key_v2


@override_settings(PARSER_VERSION=2)
def test_versioned_cache_key_changes_with_the_parser_version(seam):
    base = hash_normalized(normalize_text("200g rice"))
    with override_settings(PARSER_VERSION=1):
        key_v1 = services._versioned_cache_key(base)
    key_v2 = services._versioned_cache_key(base)

    assert key_v1 != key_v2


def test_send_message_t0_cache_hit_is_invalidated_by_a_catalog_version_bump(seam, monkeypatch):
    seam.foods["food-chicken"] = _chicken_food()
    envelope = llm_envelope(
        items=[llm_item("grilled chicken breast", quantity=150, unit="g", confidence=0.9)]
    )
    calls = stub_call_small_model(monkeypatch, result=stub_llm_response(envelope))
    content = "grilled chicken salad with a tahini dressing"

    send(seam, content, client_message_id="m1")
    seam.draft = None
    seam.catalog_version = 2  # a curator bumped the catalog between the two sends

    response = send(seam, content, client_message_id="m2")

    assert len(calls) == 2  # the old T0 entry no longer matches - re-parsed, not replayed
    assert response["tier"] != "CACHE"


def test_send_message_l2_cache_hit_for_a_different_user_skips_the_llm_call(seam, monkeypatch):
    seam.foods["food-chicken"] = _chicken_food()
    envelope = llm_envelope(
        items=[llm_item("grilled chicken breast", quantity=150, unit="g", confidence=0.9)]
    )
    calls = stub_call_small_model(monkeypatch, result=stub_llm_response(envelope))
    content = "grilled chicken salad with a tahini dressing"

    send(seam, content, client_message_id="m1")
    assert len(calls) == 1
    assert seam.global_cache  # a fully-resolved parse writes a global-cache row
    # The fake `create_draft_with_expiry_check` tracks one draft slot shared
    # across every user (not real per-user Postgres semantics) - reset it so
    # user-2's own draft creation below isn't blocked by user-1's still-open
    # one; this test is about the global cache, not per-user draft isolation.
    seam.draft = None

    response = services.send_message("user-2", {"clientMessageId": "m2", "content": content})

    assert len(calls) == 1  # no second model call for user-2's identical text
    assert response["tier"] == "CACHE"
    assert "user-2" not in seam.quota_counters
    assert seam.global_cache_hit_calls


def test_global_cache_entry_falls_through_when_the_cached_food_no_longer_resolves(seam, monkeypatch):
    seam.foods["food-chicken"] = _chicken_food()
    content = "grilled chicken salad with a tahini dressing"
    normalized_hash = services._versioned_cache_key(hash_normalized(normalize_text(content)))
    seam.global_cache[normalized_hash] = SimpleNamespace(
        normalizedHash=normalized_hash,
        snapshot={
            "name": "Lunch — Ghost",
            "slot": "LUNCH",
            "items": [{"foodId": "food-ghost", "quantity": 100.0, "unit": "g", "state": "COOKED"}],
        },
        hitCount=0,
    )
    envelope = llm_envelope(
        items=[llm_item("grilled chicken breast", quantity=150, unit="g", confidence=0.9)]
    )
    calls = stub_call_small_model(monkeypatch, result=stub_llm_response(envelope))

    response = send(seam, content)

    assert len(calls) == 1  # fell through to a fresh T2 call
    assert response["tier"] == "LLM_SMALL"


def test_l2_cache_entry_is_unreachable_after_a_catalog_version_bump(seam, monkeypatch):
    seam.foods["food-chicken"] = _chicken_food()
    envelope = llm_envelope(
        items=[llm_item("grilled chicken breast", quantity=150, unit="g", confidence=0.9)]
    )
    calls = stub_call_small_model(monkeypatch, result=stub_llm_response(envelope))
    content = "grilled chicken salad with a tahini dressing"

    send(seam, content, client_message_id="m1")
    assert len(calls) == 1
    assert seam.global_cache  # user-1's parse wrote a global-cache row under v1's key
    seam.draft = None
    seam.catalog_version = 2  # a curator bumps the catalog before user-2 ever sends this

    response = services.send_message("user-2", {"clientMessageId": "m2", "content": content})

    assert len(calls) == 2  # the v1-keyed entry doesn't match under v2 - a fresh call, not a hit
    assert response["tier"] != "CACHE"


def test_send_message_escalates_to_t3_on_low_confidence_t2_result(seam, monkeypatch):
    seam.foods["food-chicken"] = _chicken_food()
    low_confidence = llm_envelope(
        items=[llm_item("grilled chicken breast", quantity=150, unit="g", confidence=0.3)]
    )
    high_confidence = llm_envelope(
        items=[llm_item("grilled chicken breast", quantity=150, unit="g", confidence=0.95)]
    )
    small_calls = stub_call_small_model(
        monkeypatch, result=stub_llm_response(low_confidence, model="gpt-4o-mini")
    )
    large_calls = stub_call_large_model(
        monkeypatch, result=stub_llm_response(high_confidence, model="gpt-4o")
    )

    response = send(seam, "grilled chicken salad with a tahini dressing")

    assert len(small_calls) == 1
    assert len(large_calls) == 1
    assert response["tier"] == "LLM_LARGE"
    events_by_tier = {e.tier: e.countedToQuota for e in seam.parse_events}
    assert events_by_tier["LLM_SMALL"] is True
    assert events_by_tier["LLM_LARGE"] is False


def test_send_message_does_not_escalate_to_t3_on_high_confidence_t2_result(seam, monkeypatch):
    seam.foods["food-chicken"] = _chicken_food()
    envelope = llm_envelope(
        items=[llm_item("grilled chicken breast", quantity=150, unit="g", confidence=0.9)]
    )
    stub_call_small_model(monkeypatch, result=stub_llm_response(envelope))
    large_calls = stub_call_large_model(monkeypatch, result=stub_llm_response(envelope))

    response = send(seam, "grilled chicken salad with a tahini dressing")

    assert large_calls == []
    assert response["tier"] == "LLM_SMALL"


def test_send_message_does_not_escalate_to_t3_on_a_t2_call_failure(seam, monkeypatch):
    stub_call_small_model(monkeypatch, exc=LLMCallError("provider timeout"))
    large_calls = stub_call_large_model(monkeypatch, result=stub_llm_response(llm_envelope()))

    response = send(seam, "grilled chicken salad with a tahini dressing")

    assert large_calls == []
    assert response["tier"] == "PARSER"
    assert response["intent"] == "OTHER"


# -- conversational edits via AI + DraftOperation audit log (Chunk 5a) -------


def test_send_message_ai_edit_set_slot_updates_the_open_draft(seam, monkeypatch):
    create_lunch_draft(seam)
    envelope = llm_envelope(intent="SET_SLOT", slot="BREAKFAST")
    stub_call_small_model(monkeypatch, result=stub_llm_response(envelope))

    response = send(seam, "this was actually breakfast, my bad", client_message_id="m2")

    assert response["intent"] == "SET_SLOT"
    assert response["draft"]["slot"] == "BREAKFAST"


def test_send_message_ai_edit_add_item_appends_to_the_open_draft(seam, monkeypatch):
    create_lunch_draft(seam)  # 200g rice
    seam.foods["food-chicken"] = _chicken_food()
    envelope = llm_envelope(
        intent="ADD_ITEM",
        items=[llm_item("grilled chicken breast", quantity=150, unit="g", confidence=0.9)],
    )
    stub_call_small_model(monkeypatch, result=stub_llm_response(envelope))

    response = send(seam, "throw in some grilled chicken too please", client_message_id="m2")

    assert response["intent"] == "ADD_ITEM"
    items = response["draft"]["items"]
    assert len(items) == 2
    assert any(i["foodName"] == "Grilled Chicken Breast" for i in items)


def test_send_message_ai_edit_edit_item_updates_the_target(seam, monkeypatch):
    create_lunch_draft(seam)  # 200g rice
    envelope = llm_envelope(
        intent="EDIT_ITEM",
        target_ref="the rice",
        items=[llm_item("rice", quantity=100, unit="g", confidence=0.9)],
    )
    stub_call_small_model(monkeypatch, result=stub_llm_response(envelope))

    response = send(seam, "actually I only had half of that rice", client_message_id="m2")

    assert response["intent"] == "EDIT_ITEM"
    assert response["draft"]["items"][0]["quantity"] == 100.0


def test_send_message_ai_edit_remove_item_deletes_the_target(seam, monkeypatch):
    create_lunch_draft(seam)  # 200g rice
    envelope = llm_envelope(intent="REMOVE_ITEM", target_ref="the rice")
    stub_call_small_model(monkeypatch, result=stub_llm_response(envelope))

    response = send(seam, "scratch that, I didn't actually eat the rice", client_message_id="m2")

    assert response["intent"] == "REMOVE_ITEM"
    assert response["draft"]["items"] == []


def test_send_message_ai_edit_ambiguous_target_asks_for_clarification(seam, monkeypatch):
    create_lunch_draft(seam)  # only "Cooked White Rice" on the draft
    envelope = llm_envelope(intent="REMOVE_ITEM", target_ref="xyzzyplonk")
    stub_call_small_model(monkeypatch, result=stub_llm_response(envelope))

    response = send(seam, "get rid of that thing please", client_message_id="m2")

    assert response["needsClarification"]["reason"] == "ambiguous_target"
    assert response["draft"]["items"][0]["grams"] == 200.0  # unchanged


def test_ai_edit_falls_back_gracefully_on_call_failure(seam, monkeypatch):
    create_lunch_draft(seam)
    stub_call_small_model(monkeypatch, exc=LLMCallError("boom"))

    response = send(seam, "this was actually breakfast, my bad", client_message_id="m2")

    assert response["tier"] == "PARSER"
    assert response["intent"] == "OTHER"


def test_ai_edit_falls_back_gracefully_when_envelope_is_not_actionable(seam, monkeypatch):
    create_lunch_draft(seam)
    envelope = llm_envelope(intent="LOG_NEW", items=[])  # not actionable on an open draft
    stub_call_small_model(monkeypatch, result=stub_llm_response(envelope))

    response = send(seam, "this was actually breakfast, my bad", client_message_id="m2")

    assert response["intent"] == "OTHER"


def test_ai_edit_call_is_quota_exempt(seam, monkeypatch):
    create_lunch_draft(seam)
    envelope = llm_envelope(intent="SET_SLOT", slot="BREAKFAST")
    stub_call_small_model(monkeypatch, result=stub_llm_response(envelope))

    send(seam, "this was actually breakfast, my bad", client_message_id="m2")

    assert "user-1" not in seam.quota_counters
    edit_events = [e for e in seam.parse_events if e.intent == "SET_SLOT"]
    assert len(edit_events) == 1
    assert edit_events[0].countedToQuota is False


def test_ai_edit_does_not_escalate_to_t3_even_on_low_confidence(seam, monkeypatch):
    create_lunch_draft(seam)
    seam.foods["food-chicken"] = _chicken_food()
    envelope = llm_envelope(
        intent="ADD_ITEM",
        items=[llm_item("grilled chicken breast", quantity=150, unit="g", confidence=0.1)],
    )
    stub_call_small_model(monkeypatch, result=stub_llm_response(envelope))
    large_calls = stub_call_large_model(monkeypatch, result=stub_llm_response(envelope))

    send(seam, "throw in some grilled chicken too please", client_message_id="m2")

    assert large_calls == []


def test_draft_operations_are_recorded_for_every_mutation_type(seam):
    created = create_lunch_draft(seam)  # CREATE
    item_id = created["items"][0]["id"]
    version = created["version"]

    services.add_draft_item(
        "user-1", created["id"], {"foodId": "food-rice", "quantity": 50.0, "unit": "g", "version": version}
    )
    version += 1

    services.update_draft_item("user-1", created["id"], item_id, {"quantity": 100.0, "version": version})
    version += 1

    services.update_draft("user-1", created["id"], {"slot": "DINNER", "version": version})
    version += 1

    services.delete_draft_item("user-1", created["id"], item_id, version)

    ops = [op.op for op in seam.draft_operations]
    assert ops == ["CREATE", "ADD_ITEM", "EDIT_ITEM", "SET_SLOT", "REMOVE_ITEM"]
    assert seam.draft_operations[0].version == 1
    assert seam.draft_operations[3].payload == {"slot": "DINNER"}


# -- estimated-dish handling (Chunk 5b, §7.6.1) ------------------------------


def make_dish_profile(**overrides):
    fields = dict(caloriesKcalP25Per100g=120.0, caloriesKcalP75Per100g=220.0, catalogVersion=1)
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_resolve_estimated_dish_with_a_stated_serving(seam):
    seam.dish_category_profiles["SPICED_CURRY"] = make_dish_profile()

    result = services._resolve_estimated_dish(
        "SPICED_CURRY", llm_item("misal pav", quantity=250, unit="g", confidence=0.7)
    )

    assert result is not None
    payload, vector = result
    assert payload["resolution"] == "ESTIMATED_DISH"
    assert payload["dishCategory"] == "SPICED_CURRY"
    assert payload["grams"] == pytest.approx(250.0)
    assert payload["kcalLow"] == pytest.approx(300.0)  # 120 * 2.5
    assert payload["kcalHigh"] == pytest.approx(550.0)  # 220 * 2.5
    assert payload["kcalMidpoint"] == pytest.approx(425.0)
    assert vector.calories_kcal == pytest.approx(425.0)
    assert vector.protein_g is None
    assert seam.food_misses == ["misal pav"]  # files regardless of outcome (§7.6)


def test_resolve_estimated_dish_defaults_serving_when_unstated(seam):
    seam.dish_category_profiles["SPICED_CURRY"] = make_dish_profile()

    payload, _vector = services._resolve_estimated_dish(
        "SPICED_CURRY", llm_item("misal pav", confidence=0.7)
    )

    assert payload["grams"] == pytest.approx(300.0)
    assert payload["quantity"] == pytest.approx(300.0)
    assert payload["unit"] == "g"


def test_resolve_estimated_dish_defaults_serving_on_an_unrecognized_unit(seam):
    seam.dish_category_profiles["SPICED_CURRY"] = make_dish_profile()

    payload, _vector = services._resolve_estimated_dish(
        "SPICED_CURRY", llm_item("misal pav", quantity=2, unit="bowls", confidence=0.7)
    )

    assert payload["grams"] == pytest.approx(300.0)


def test_resolve_estimated_dish_returns_none_for_an_unknown_category(seam):
    result = services._resolve_estimated_dish(
        "SPICED_CURRY", llm_item("some mystery dish", confidence=0.5)
    )

    assert result is None
    assert seam.food_misses == ["some mystery dish"]  # still files, even unresolved


def test_send_message_log_new_with_dish_category_creates_an_estimated_dish_item(seam, monkeypatch):
    seam.dish_category_profiles["SPICED_CURRY"] = make_dish_profile()
    envelope = llm_envelope(
        dish_category="SPICED_CURRY",
        items=[llm_item("misal pav", quantity=300, unit="g", confidence=0.6)],
    )
    stub_call_small_model(monkeypatch, result=stub_llm_response(envelope))

    response = send(seam, "I had some misal pav for lunch today")

    assert response["intent"] == "LOG_NEW"
    items = response["draft"]["items"]
    assert len(items) == 1
    item = items[0]
    assert item["isEstimatedDish"] is True
    assert item["dishCategory"] == "SPICED_CURRY"
    assert item["kcalLow"] == round_int(360.0)  # 120 * 3
    assert item["kcalHigh"] == round_int(660.0)  # 220 * 3
    assert item["proteinG"] is None
    assert response["draft"]["totals"]["caloriesKcal"] == round_int(510.0)  # midpoint
    assert response["draft"]["totals"]["proteinG"] is None  # nothing else on the draft


def test_send_message_ai_edit_add_item_with_dish_category(seam, monkeypatch):
    create_lunch_draft(seam)  # 200g rice open draft
    seam.dish_category_profiles["FRIED_SNACK"] = make_dish_profile(
        caloriesKcalP25Per100g=250.0, caloriesKcalP75Per100g=400.0
    )
    envelope = llm_envelope(
        intent="ADD_ITEM",
        dish_category="FRIED_SNACK",
        items=[llm_item("some pakoras", confidence=0.6)],
    )
    stub_call_small_model(monkeypatch, result=stub_llm_response(envelope))

    response = send(seam, "also throw in some pakoras on the side", client_message_id="m2")

    assert response["intent"] == "ADD_ITEM"
    items = response["draft"]["items"]
    assert len(items) == 2
    dish_item = next(i for i in items if i["isEstimatedDish"])
    assert dish_item["dishCategory"] == "FRIED_SNACK"


def test_update_draft_item_rejects_an_estimated_dish_item(seam):
    seam.dish_category_profiles["SPICED_CURRY"] = make_dish_profile()
    payload, vector = services._resolve_estimated_dish(
        "SPICED_CURRY", llm_item("misal pav", quantity=300, unit="g", confidence=0.6)
    )
    created = services._create_draft_from_items(
        "user-1", "Lunch", "LUNCH", "LLM_SMALL", 1.0, [payload], [vector]
    )

    with pytest.raises(EstimatedDishNotEditableError):
        services.update_draft_item(
            "user-1", created.id, created.items[0].id, {"quantity": 500.0, "version": created.version}
        )


def test_confirm_draft_converts_an_estimated_dish_item_recomputed_against_current_profile(seam):
    seam.dish_category_profiles["SPICED_CURRY"] = make_dish_profile()
    payload, vector = services._resolve_estimated_dish(
        "SPICED_CURRY", llm_item("misal pav", quantity=300, unit="g", confidence=0.6)
    )
    created = services._create_draft_from_items(
        "user-1", "Lunch", "LUNCH", "LLM_SMALL", 1.0, [payload], [vector]
    )

    # The profile changes between draft creation and confirm - confirm must
    # recompute fresh (§12.5), not trust the draft's own stored range.
    seam.dish_category_profiles["SPICED_CURRY"] = make_dish_profile(
        caloriesKcalP25Per100g=200.0, caloriesKcalP75Per100g=300.0, catalogVersion=2
    )

    response = services.confirm_draft("user-1", created.id, "idem-1", created.version)

    item = response["loggedMeal"]["items"][0]
    assert item["isEstimatedDish"] is True
    assert item["caloriesKcal"] == round_int(750.0)  # (200+300)/2 * 3
    assert item["proteinG"] is None


def test_confirm_draft_drops_an_estimated_dish_item_when_its_profile_is_gone(seam):
    seam.dish_category_profiles["SPICED_CURRY"] = make_dish_profile()
    payload, vector = services._resolve_estimated_dish(
        "SPICED_CURRY", llm_item("misal pav", quantity=300, unit="g", confidence=0.6)
    )
    created = services._create_draft_from_items(
        "user-1", "Lunch", "LUNCH", "LLM_SMALL", 1.0, [payload], [vector]
    )

    del seam.dish_category_profiles["SPICED_CURRY"]

    response = services.confirm_draft("user-1", created.id, "idem-1", created.version)

    assert response["loggedMeal"]["items"] == []


# -- non-logging intents (Chunk 6a, §5.5) ------------------------------------


def make_logged_meal_row(**overrides):
    fields = dict(
        caloriesKcal=500.0, proteinG=20.0, carbsG=50.0, fatG=10.0, fiberG=5.0,
        loggedAt=datetime.now(timezone.utc),
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def make_plan(**overrides):
    fields = dict(caloriesKcal=2000, proteinG=150, carbsG=200, fatG=60, fiberG=30)
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_send_message_app_help_intent(seam):
    response = send(seam, "how do i change my calorie goal")

    assert response["tier"] == "PRECLASSIFIER"
    assert response["intent"] == "APP_HELP"
    assert response["draft"] is None


def test_send_message_advice_seeking_intent(seam):
    response = send(seam, "should i try keto")

    assert response["tier"] == "PRECLASSIFIER"
    assert response["intent"] == "ADVICE_SEEKING"
    assert response["draft"] is None


def test_send_message_unclear_intent(seam):
    response = send(seam, "yes")

    assert response["tier"] == "PRECLASSIFIER"
    assert response["intent"] == "UNCLEAR"


def test_diary_query_with_no_meals_logged_today(seam):
    response = send(seam, "how many calories do i have left")

    assert response["intent"] == "DIARY_QUERY"
    assert "haven't logged" in response["assistantText"].lower()


def test_diary_query_reports_totals_without_a_plan(seam):
    seam.logged_meals.append(make_logged_meal_row())

    response = send(seam, "how many calories do i have left")

    assert response["intent"] == "DIARY_QUERY"
    assert "500" in response["assistantText"]


def test_diary_query_reports_remaining_against_a_plan_target(seam):
    seam.logged_meals.append(make_logged_meal_row())
    seam.profiles["user-1"] = SimpleNamespace(plan=make_plan())

    response = send(seam, "how many calories do i have left")

    assert "500" in response["assistantText"]
    assert "1500" in response["assistantText"]  # 2000 - 500


def test_diary_query_trend_question_deep_links_to_insights(seam):
    seam.logged_meals.append(make_logged_meal_row())

    response = send(seam, "how many calories did i eat this week")

    assert response["intent"] == "DIARY_QUERY"
    assert "insights" in response["assistantText"].lower()


def test_nutrition_qa_answers_a_high_confidence_match(seam):
    response = send(seam, "how much protein is in cooked white rice")

    assert response["intent"] == "NUTRITION_QA"
    assert "Cooked White Rice" in response["assistantText"]
    assert "per 100g" in response["assistantText"]


def test_nutrition_qa_deflects_on_no_catalog_match(seam):
    response = send(seam, "how much protein is in xyzzyplonk")

    assert response["intent"] == "NUTRITION_QA"
    assert "couldn't find" in response["assistantText"].lower()


def test_send_message_t2_classifies_diary_query_when_t1_misses(seam, monkeypatch):
    seam.logged_meals.append(make_logged_meal_row(caloriesKcal=300.0))
    envelope = llm_envelope(intent="DIARY_QUERY", items=[])
    stub_call_small_model(monkeypatch, result=stub_llm_response(envelope))

    response = send(seam, "hows my day looking food wise")

    assert response["tier"] == "LLM_SMALL"
    assert response["intent"] == "DIARY_QUERY"
    assert "300" in response["assistantText"]


def test_send_message_t2_classifies_nutrition_qa_using_envelope_items(seam, monkeypatch):
    envelope = llm_envelope(
        intent="NUTRITION_QA", items=[llm_item("cooked white rice", confidence=0.8)]
    )
    stub_call_small_model(monkeypatch, result=stub_llm_response(envelope))

    response = send(seam, "tell me about the protein content of rice please")

    assert response["tier"] == "LLM_SMALL"
    assert response["intent"] == "NUTRITION_QA"
    assert "Cooked White Rice" in response["assistantText"]


def test_send_message_t2_non_logging_intent_does_not_refund_quota(seam, monkeypatch):
    envelope = llm_envelope(intent="OTHER", items=[])
    stub_call_small_model(monkeypatch, result=stub_llm_response(envelope))

    send(seam, "grilled chicken salad with a tahini dressing")

    assert seam.quota_counters["user-1"].count == 1


# -- WELLBEING_FLAG (Chunk 6b, §5.6) -----------------------------------------


def test_send_message_wellbeing_flag_from_t2_is_confirmed_by_t3(seam, monkeypatch):
    t2_envelope = llm_envelope(intent="WELLBEING_FLAG", items=[])
    t3_envelope = llm_envelope(intent="LOG_NEW", items=[])  # T3 disagreeing shouldn't matter
    stub_call_small_model(monkeypatch, result=stub_llm_response(t2_envelope))
    large_calls = stub_call_large_model(monkeypatch, result=stub_llm_response(t3_envelope))

    response = send(seam, "i've been skipping meals all week and feel awful")

    assert len(large_calls) == 1
    assert response["tier"] == "LLM_LARGE"
    assert response["intent"] == "WELLBEING_FLAG"
    assert response["draft"] is None
    assert response["gamificationSuppressed"] is True
    assert response["wellbeingResources"]
    assert seam.gamification_suppressed_sessions == ["session-1"]


def test_send_message_wellbeing_flag_from_t2_survives_a_t3_call_failure(seam, monkeypatch):
    t2_envelope = llm_envelope(intent="WELLBEING_FLAG", items=[])
    stub_call_small_model(monkeypatch, result=stub_llm_response(t2_envelope))
    stub_call_large_model(monkeypatch, exc=LLMCallError("boom"))

    response = send(seam, "i've been skipping meals all week and feel awful")

    assert response["intent"] == "WELLBEING_FLAG"


def test_send_message_wellbeing_flag_surfaced_by_t3_after_low_confidence_t2(seam, monkeypatch):
    t2_envelope = llm_envelope(intent="LOG_NEW", items=[llm_item("something", confidence=0.1)])
    t3_envelope = llm_envelope(intent="WELLBEING_FLAG", items=[])
    stub_call_small_model(monkeypatch, result=stub_llm_response(t2_envelope))
    stub_call_large_model(monkeypatch, result=stub_llm_response(t3_envelope))

    response = send(seam, "i had something weird today")

    assert response["intent"] == "WELLBEING_FLAG"


def test_send_message_wellbeing_flag_consumes_quota_only_once(seam, monkeypatch):
    t2_envelope = llm_envelope(intent="WELLBEING_FLAG", items=[])
    stub_call_small_model(monkeypatch, result=stub_llm_response(t2_envelope))
    stub_call_large_model(monkeypatch, result=stub_llm_response(llm_envelope()))

    send(seam, "i've been skipping meals all week and feel awful")

    assert seam.quota_counters["user-1"].count == 1


def test_wellbeing_check_all_messages_off_by_default_skips_the_model(seam, monkeypatch):
    large_calls = stub_call_large_model(
        monkeypatch, result=stub_llm_response(llm_envelope(intent="WELLBEING_FLAG"))
    )

    response = send(seam, "200g rice, i don't deserve to eat this")

    assert large_calls == []
    assert response["intent"] == "LOG_NEW"
    assert response["draft"] is not None


def test_wellbeing_check_all_messages_on_intercepts_a_flagged_message(seam, monkeypatch):
    stub_call_large_model(monkeypatch, result=stub_llm_response(llm_envelope(intent="WELLBEING_FLAG")))

    with override_settings(WELLBEING_CHECK_ALL_MESSAGES=True):
        response = send(seam, "200g rice, i don't deserve to eat this")

    assert response["intent"] == "WELLBEING_FLAG"
    assert response["draft"] is None


def test_wellbeing_check_all_messages_on_but_not_confirmed_logs_normally(seam, monkeypatch):
    large_calls = stub_call_large_model(
        monkeypatch, result=stub_llm_response(llm_envelope(intent="LOG_NEW"))
    )

    with override_settings(WELLBEING_CHECK_ALL_MESSAGES=True):
        response = send(seam, "200g rice, i don't deserve to eat this")

    assert len(large_calls) == 1
    assert response["intent"] == "LOG_NEW"
    assert response["draft"] is not None


# -- offline queue & sync (Chunk 7, §12.12) ----------------------------------


def test_idempotent_replays_the_stored_response_for_a_repeated_op_id(seam):
    calls = []

    def fn():
        calls.append(1)
        return {"ok": True}

    r1 = services._idempotent("op-1", "user-1", {"a": 1}, 200, fn)
    r2 = services._idempotent("op-1", "user-1", {"a": 1}, 200, fn)

    assert r1 == r2 == {"ok": True}
    assert len(calls) == 1


def test_idempotent_raises_on_mismatched_payload_reuse(seam):
    services._idempotent("op-2", "user-1", {"a": 1}, 200, lambda: {"ok": True})

    with pytest.raises(IdempotencyKeyReuseError):
        services._idempotent("op-2", "user-1", {"a": 2}, 200, lambda: {"ok": True})


def test_idempotent_without_an_op_id_always_reruns(seam):
    calls = []

    def fn():
        calls.append(1)
        return {"ok": True}

    services._idempotent(None, "user-1", {"a": 1}, 200, fn)
    services._idempotent(None, "user-1", {"a": 1}, 200, fn)

    assert len(calls) == 2


def test_create_draft_with_a_repeated_op_id_does_not_create_twice(seam):
    seam.foods["food-rice"] = make_food()
    payload = {
        "name": "Lunch",
        "slot": "LUNCH",
        "items": [{"foodId": "food-rice", "quantity": 200.0, "unit": "g"}],
        "opId": "op-create-1",
    }

    r1 = services.create_draft("user-1", payload)
    r2 = services.create_draft("user-1", payload)

    assert r1 == r2
    assert seam.next_draft_id == 2  # only one draft was actually created


def test_add_draft_item_with_a_repeated_op_id_does_not_add_twice(seam):
    created = create_lunch_draft(seam)
    payload = {
        "foodId": "food-rice",
        "quantity": 50.0,
        "unit": "g",
        "version": created["version"],
        "opId": "op-add-1",
    }

    r1 = services.add_draft_item("user-1", created["id"], payload)
    r2 = services.add_draft_item("user-1", created["id"], payload)

    assert r1 == r2
    assert len(r1["items"]) == 2  # rice + one added item, not two


def test_update_draft_item_with_a_repeated_op_id_does_not_mutate_twice(seam):
    created = create_lunch_draft(seam, quantity=100.0)
    item_id = created["items"][0]["id"]
    payload = {"quantity": 300.0, "version": created["version"], "opId": "op-edit-1"}

    r1 = services.update_draft_item("user-1", created["id"], item_id, payload)
    r2 = services.update_draft_item("user-1", created["id"], item_id, payload)

    assert r1 == r2
    assert r1["items"][0]["quantity"] == 300.0


def test_delete_draft_item_with_a_repeated_op_id_does_not_remove_twice(seam):
    created = create_lunch_draft(seam)
    item_id = created["items"][0]["id"]

    r1 = services.delete_draft_item("user-1", created["id"], item_id, created["version"], "op-remove-1")
    r2 = services.delete_draft_item("user-1", created["id"], item_id, created["version"], "op-remove-1")

    assert r1 == r2
    assert r1["items"] == []


def test_update_draft_with_a_repeated_op_id_does_not_rename_twice(seam):
    created = create_lunch_draft(seam)
    payload = {"name": "Dinner", "version": created["version"], "opId": "op-slot-1"}

    r1 = services.update_draft("user-1", created["id"], payload)
    r2 = services.update_draft("user-1", created["id"], payload)

    assert r1 == r2
    assert r1["version"] == created["version"] + 1  # bumped once, not twice


def test_confirm_draft_with_a_hard_expired_meal_timestamp_is_rejected(seam):
    created = create_lunch_draft(seam)
    old_timestamp = datetime.now(timezone.utc) - timedelta(days=40)

    with pytest.raises(OperationExpiredError):
        services.confirm_draft(
            "user-1", created["id"], "idem-1", created["version"], meal_timestamp=old_timestamp
        )

    assert seam.create_logged_meal_calls == 0


def test_confirm_draft_with_a_stale_meal_timestamp_requires_confirmation(seam):
    created = create_lunch_draft(seam)
    stale_timestamp = datetime.now(timezone.utc) - timedelta(days=10)

    with pytest.raises(StaleOperationError):
        services.confirm_draft(
            "user-1", created["id"], "idem-1", created["version"], meal_timestamp=stale_timestamp
        )
    assert seam.create_logged_meal_calls == 0

    response = services.confirm_draft(
        "user-1",
        created["id"],
        "idem-2",
        created["version"],
        meal_timestamp=stale_timestamp,
        stale_confirmed=True,
    )
    assert response["loggedMeal"]["loggedAt"] == stale_timestamp.isoformat()


def test_confirm_draft_with_a_recent_meal_timestamp_needs_no_confirmation(seam):
    created = create_lunch_draft(seam)
    recent_timestamp = datetime.now(timezone.utc) - timedelta(days=1)

    response = services.confirm_draft(
        "user-1", created["id"], "idem-1", created["version"], meal_timestamp=recent_timestamp
    )

    assert response["loggedMeal"]["loggedAt"] == recent_timestamp.isoformat()


def test_confirm_draft_flags_a_significant_nutrition_snapshot_drift(seam):
    created = create_lunch_draft(seam)  # 200g cooked rice = 260 kcal

    response = services.confirm_draft(
        "user-1",
        created["id"],
        "idem-1",
        created["version"],
        nutrition_snapshot={"caloriesKcal": 100.0},
    )

    assert response["recomputedFromClientSnapshot"] is True


def test_confirm_draft_does_not_flag_a_close_nutrition_snapshot(seam):
    created = create_lunch_draft(seam)  # 200g cooked rice = 260 kcal

    response = services.confirm_draft(
        "user-1",
        created["id"],
        "idem-1",
        created["version"],
        nutrition_snapshot={"caloriesKcal": 258.0},
    )

    assert response["recomputedFromClientSnapshot"] is False


def test_confirm_draft_without_any_offline_fields_is_unchanged(seam):
    created = create_lunch_draft(seam)

    response = services.confirm_draft("user-1", created["id"], "idem-1", created["version"])

    assert response["recomputedFromClientSnapshot"] is False


@override_settings(NUTRITION_ENGINE_VERSION=5)
def test_confirm_draft_stamps_the_current_catalog_and_nutrition_engine_version(seam):
    seam.catalog_version = 4
    created = create_lunch_draft(seam)

    services.confirm_draft("user-1", created["id"], "idem-1", created["version"])

    logged_meal = seam.logged_meals[-1]
    assert logged_meal.catalogVersion == 4
    assert logged_meal.nutritionEngineVersion == 5


# -- correlation chain (Chunk 8b, §12.9) --------------------------------------


def test_send_message_records_the_request_id_on_both_chat_messages(seam, monkeypatch):
    monkeypatch.setattr(services, "current_request_id", lambda: "req-abc123")

    send(seam, "200g rice")

    assert len(seam.messages) == 2
    assert all(m.requestId == "req-abc123" for m in seam.messages)


def test_send_message_records_no_request_id_outside_a_request(seam):
    # `current_request_id`'s real default ("-", no middleware in this test)
    # is normalized to None - a service function called directly (as every
    # test in this file does) has no request in flight.
    send(seam, "200g rice")

    assert len(seam.messages) == 2
    assert all(m.requestId is None for m in seam.messages)


def test_call_llm_records_the_request_id_on_a_successful_parse_event(seam, monkeypatch):
    monkeypatch.setattr(services, "current_request_id", lambda: "req-success")
    seam.foods["food-chicken"] = _chicken_food()
    envelope = llm_envelope(
        items=[llm_item("grilled chicken breast", quantity=150, unit="g", confidence=0.9)]
    )
    stub_call_small_model(monkeypatch, result=stub_llm_response(envelope))

    send(seam, "grilled chicken salad with a tahini dressing")

    assert len(seam.parse_events) == 1
    assert seam.parse_events[0].requestId == "req-success"


def test_call_llm_records_the_request_id_on_a_call_failure(seam, monkeypatch):
    monkeypatch.setattr(services, "current_request_id", lambda: "req-failure")
    stub_call_small_model(monkeypatch, exc=LLMCallError("provider timeout"))

    send(seam, "grilled chicken salad with a tahini dressing")

    assert len(seam.parse_events) == 1
    assert seam.parse_events[0].requestId == "req-failure"


def test_call_llm_records_the_request_id_on_a_validation_failure(seam, monkeypatch):
    monkeypatch.setattr(services, "current_request_id", lambda: "req-invalid")
    bad_envelope = llm_envelope(items=[llm_item("chicken", state="sizzling", confidence=0.5)])
    stub_call_small_model(monkeypatch, result=stub_llm_response(bad_envelope))

    send(seam, "grilled chicken salad with a tahini dressing")

    assert len(seam.parse_events) == 1
    assert seam.parse_events[0].requestId == "req-invalid"


# -- cost circuit breaker (Chunk 8d, §11, §12.8) ------------------------------


@override_settings(AI_CIRCUIT_BREAKER_FAILURE_THRESHOLD=3)
def test_circuit_breaker_trips_after_the_threshold_and_skips_the_next_call(seam, monkeypatch):
    calls = stub_call_small_model(monkeypatch, exc=LLMCallError("provider timeout"))

    for i in range(3):
        send(seam, "grilled chicken salad with a tahini dressing", client_message_id="m{}".format(i))
    assert len(calls) == 3
    assert seam.circuit_breaker.openedAt is not None

    response = send(seam, "grilled chicken salad with a tahini dressing", client_message_id="m-final")

    assert len(calls) == 3  # the 4th attempt never reached call_fn at all
    assert response["tier"] == "PARSER"
    assert response["intent"] == "OTHER"
    assert len(seam.parse_events) == 4
    assert seam.parse_events[-1].latencyMs == 0
    assert not hasattr(seam.parse_events[-1], "model")  # no real call was ever attempted


@override_settings(AI_CIRCUIT_BREAKER_FAILURE_THRESHOLD=3)
def test_circuit_breaker_resets_on_a_successful_call_before_reaching_the_threshold(seam, monkeypatch):
    stub_call_small_model(monkeypatch, exc=LLMCallError("provider timeout"))
    send(seam, "grilled chicken salad with a tahini dressing", client_message_id="m1")
    send(seam, "grilled chicken salad with a tahini dressing", client_message_id="m2")
    assert seam.circuit_breaker.consecutiveFailures == 2

    seam.foods["food-chicken"] = _chicken_food()
    envelope = llm_envelope(
        items=[llm_item("grilled chicken breast", quantity=150, unit="g", confidence=0.9)]
    )
    stub_call_small_model(monkeypatch, result=stub_llm_response(envelope))
    send(seam, "grilled chicken salad with a tahini dressing", client_message_id="m3")

    assert seam.circuit_breaker.consecutiveFailures == 0
    assert seam.circuit_breaker.openedAt is None

    # A fresh run of failures needs the full threshold again from zero.
    calls = stub_call_small_model(monkeypatch, exc=LLMCallError("provider timeout"))
    send(seam, "grilled chicken salad with a tahini dressing", client_message_id="m4")
    send(seam, "grilled chicken salad with a tahini dressing", client_message_id="m5")
    assert len(calls) == 2
    assert seam.circuit_breaker.openedAt is None  # still below threshold


def test_llm_configuration_error_never_opens_the_circuit_breaker(seam, monkeypatch):
    def fake(system_prompt, user_content):
        raise LLMConfigurationError("OPENAI_API_KEY must be set to call the LLM provider.")

    monkeypatch.setattr(services, "call_small_model", fake)

    for i in range(10):
        send(seam, "grilled chicken salad with a tahini dressing", client_message_id="m{}".format(i))

    assert seam.circuit_breaker.consecutiveFailures == 0
    assert seam.circuit_breaker.openedAt is None


@override_settings(AI_CIRCUIT_BREAKER_FAILURE_THRESHOLD=1, AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS=60)
def test_circuit_breaker_reopens_on_a_failed_probe_after_cooldown(seam, monkeypatch):
    stub_call_small_model(monkeypatch, exc=LLMCallError("provider timeout"))
    send(seam, "grilled chicken salad with a tahini dressing", client_message_id="m1")
    assert seam.circuit_breaker.openedAt is not None

    # Backdate past the cooldown so the next call is attempted for real again.
    seam.circuit_breaker.openedAt = datetime.now(timezone.utc) - timedelta(seconds=120)

    calls = stub_call_small_model(monkeypatch, exc=LLMCallError("still down"))
    send(seam, "grilled chicken salad with a tahini dressing", client_message_id="m2")

    assert len(calls) == 1  # the probe call was actually attempted
    assert seam.circuit_breaker.openedAt is not None  # re-opened, cooldown restarted
    assert seam.circuit_breaker.openedAt > datetime.now(timezone.utc) - timedelta(seconds=5)


@override_settings(AI_CIRCUIT_BREAKER_FAILURE_THRESHOLD=1, AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS=60)
def test_circuit_breaker_closes_on_a_successful_probe_after_cooldown(seam, monkeypatch):
    stub_call_small_model(monkeypatch, exc=LLMCallError("provider timeout"))
    send(seam, "grilled chicken salad with a tahini dressing", client_message_id="m1")
    assert seam.circuit_breaker.openedAt is not None

    seam.circuit_breaker.openedAt = datetime.now(timezone.utc) - timedelta(seconds=120)

    seam.foods["food-chicken"] = _chicken_food()
    envelope = llm_envelope(
        items=[llm_item("grilled chicken breast", quantity=150, unit="g", confidence=0.9)]
    )
    calls = stub_call_small_model(monkeypatch, result=stub_llm_response(envelope))
    send(seam, "grilled chicken salad with a tahini dressing", client_message_id="m2")

    assert len(calls) == 1
    assert seam.circuit_breaker.consecutiveFailures == 0
    assert seam.circuit_breaker.openedAt is None


def test_validation_failure_neither_opens_nor_resets_the_circuit_breaker(seam, monkeypatch):
    bad_envelope = llm_envelope(items=[llm_item("chicken", state="sizzling", confidence=0.5)])
    stub_call_small_model(monkeypatch, result=stub_llm_response(bad_envelope))

    send(seam, "grilled chicken salad with a tahini dressing")

    assert seam.circuit_breaker.consecutiveFailures == 0
    assert seam.circuit_breaker.openedAt is None
