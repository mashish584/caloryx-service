"""Meal Assistant draft use cases (PRD §9, §12.1, §12.2, §12.5) - Chunk 2a:
structured (non-text) endpoints only. Views stay thin: they validate, call
one of these, and render - see meals/services.py for the pattern this follows,
which this module builds directly on top of (food resolution, the confirm ->
LoggedMeal handoff) rather than duplicating it.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from common.exceptions import (
    DraftNotOpenError,
    DraftVersionConflictError,
    IdempotencyKeyReuseError,
    NotFoundError,
    OpenDraftExistsError,
)
from meals import repository as meals_repository
from meals import services as meals_services
from meals.serializers import serialize_logged_meal
from nutrition import NutrientVector, sum_nutrition

from . import repository
from .serializers import item_nutrient_vector, serialize_daily_totals, serialize_draft

logger = logging.getLogger(__name__)

DRAFT_TTL_HOURS = 24
# Chunk 7 widens this to the full offline REPLAY_WINDOW once the queue exists
# (see the schema comment on IdempotencyRecord.expiresAt) - the two must
# eventually derive from one config value, not be tuned independently.
IDEMPOTENCY_TTL_HOURS = 24

# §5.1.2 default meal-slot windows, by hour (inclusive). Anything outside all
# three windows is a snack.
_SLOT_WINDOWS = [
    (4, 10, "BREAKFAST"),
    (11, 15, "LUNCH"),
    (16, 21, "DINNER"),
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _infer_slot(local_hour: Optional[int]) -> str:
    """§5.1.2: infer from local time when the client doesn't send an explicit
    slot. `local_hour` is the client's own local hour (0-23); falling back to
    UTC server time when it's absent is a documented simplification - true
    device-local inference is a client concern, this just needs *a* signal."""
    hour = local_hour if local_hour is not None else _now().hour
    for start, end, slot in _SLOT_WINDOWS:
        if start <= hour <= end:
            return slot
    return "SNACK"


def _mass_source(unit: str) -> str:
    return "DIRECT" if unit.strip().lower() in {"g", "kg"} else "HOUSEHOLD_TABLE"


def _resolve_draft_item(
    food: Any, quantity: float, unit: str, state: Optional[str]
) -> Tuple[Dict[str, Any], NutrientVector]:
    """Structured item input -> a `MealDraftItem` create payload plus its
    nutrient vector. Reuses `meals.services.resolve_item` for the actual
    quantity/unit/yield math (already tested in Chunk 1) and reshapes the
    result for the draft schema: no stored nutrition columns (computed live,
    see MealDraftItem's schema comment), provenance fields instead (§5.1.1a)."""
    resolved, vector = meals_services.resolve_item(food, quantity, unit, state)
    payload = {
        "resolution": "RESOLVED",
        "foodId": resolved["foodId"],
        "rawText": "{}{} {}".format(resolved["quantity"], resolved["unit"], food.name),
        "quantity": resolved["quantity"],
        "unit": resolved["unit"],
        "grams": resolved["grams"],
        "state": resolved["state"],
        "defaultGrams": resolved["grams"],
        "quantitySource": "EXPLICIT",
        "massSource": _mass_source(resolved["unit"]),
    }
    return payload, vector


def _totals_payload(totals: NutrientVector) -> Dict[str, Any]:
    return {
        "caloriesKcal": totals.calories_kcal,
        "proteinG": totals.protein_g,
        "carbsG": totals.carbs_g,
        "fatG": totals.fat_g,
        "fiberG": totals.fiber_g,
    }


def _draft_totals(draft: Any) -> NutrientVector:
    return sum_nutrition(item_nutrient_vector(item) for item in draft.items)


def _recompute_totals(user_id: str, draft_id: str, *, bump_version: bool) -> Any:
    draft = repository.get_draft(user_id, draft_id)
    patch = _totals_payload(_draft_totals(draft))
    if bump_version:
        patch["version"] = {"increment": 1}
    return repository.update_draft(draft_id, patch)


def _load_open_draft_for_mutation(user_id: str, draft_id: str, version: int) -> Any:
    """Shared precondition for every mutating call: exists and is owned,
    lazily expired if stale, OPEN, and at the version the caller last saw."""
    draft = repository.get_draft(user_id, draft_id)
    if draft is None:
        raise NotFoundError("Draft not found.", code="draft_not_found")

    draft = repository.expire_draft_if_stale(draft)
    if draft.status != "OPEN":
        raise DraftNotOpenError(details={"draft": serialize_draft(draft)})
    if draft.version != version:
        raise DraftVersionConflictError(details={"draft": serialize_draft(draft)})
    return draft


# -- draft lifecycle ----------------------------------------------------------


def create_draft(user_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """POST /drafts (§4's "fully quantified" use case, chat-shaped). One open
    draft per user (§9, §12.2) - enforced by transactional lazy expiry at
    write time; see assistant.repository.create_draft_with_expiry_check for
    why there's no DB-level constraint backing it yet."""
    items_input = data["items"]
    items_payload: List[Dict[str, Any]] = []
    vectors: List[NutrientVector] = []
    for raw in items_input:
        food = meals_repository.get_food(raw["foodId"])
        if food is None:
            raise NotFoundError(
                "Food not found.", code="food_not_found", details={"foodId": raw["foodId"]}
            )
        item_payload, vector = _resolve_draft_item(food, raw["quantity"], raw["unit"], raw.get("state"))
        items_payload.append(item_payload)
        vectors.append(vector)

    slot = data.get("slot") or _infer_slot(data.get("localHour"))
    session = repository.get_or_create_today_session(user_id)
    draft_data = dict(
        name=data["name"],
        slot=slot,
        expiresAt=_now() + timedelta(hours=DRAFT_TTL_HOURS),
        **_totals_payload(sum_nutrition(vectors)),
    )

    created = repository.create_draft_with_expiry_check(
        user_id, session.id, draft_data, items_payload
    )
    if created is None:
        existing = repository.get_open_draft(user_id)
        raise OpenDraftExistsError(details={"draft": serialize_draft(existing)})

    logger.info("draft created user=%s draft=%s items=%s", user_id, created.id, len(items_payload))
    return serialize_draft(created)


def fetch_draft(user_id: str, draft_id: str) -> Dict[str, Any]:
    draft = repository.get_draft(user_id, draft_id)
    if draft is None:
        raise NotFoundError("Draft not found.", code="draft_not_found")
    draft = repository.expire_draft_if_stale(draft)
    return serialize_draft(draft)


def update_draft(user_id: str, draft_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    _load_open_draft_for_mutation(user_id, draft_id, data["version"])
    patch = {k: v for k, v in data.items() if k in ("name", "slot")}
    patch["version"] = {"increment": 1}
    updated = repository.update_draft(draft_id, patch)
    return serialize_draft(updated)


def discard_draft(user_id: str, draft_id: str, version: int) -> Dict[str, Any]:
    _load_open_draft_for_mutation(user_id, draft_id, version)
    updated = repository.update_draft(draft_id, {"status": "DISCARDED", "version": {"increment": 1}})
    logger.info("draft discarded user=%s draft=%s", user_id, draft_id)
    return serialize_draft(updated)


# -- draft items (Adjust Portion persistence, §5.2.1) ------------------------


def add_draft_item(user_id: str, draft_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    _load_open_draft_for_mutation(user_id, draft_id, data["version"])
    food = meals_repository.get_food(data["foodId"])
    if food is None:
        raise NotFoundError(
            "Food not found.", code="food_not_found", details={"foodId": data["foodId"]}
        )
    item_payload, _ = _resolve_draft_item(food, data["quantity"], data["unit"], data.get("state"))
    repository.create_draft_item(draft_id, item_payload)

    updated = _recompute_totals(user_id, draft_id, bump_version=True)
    logger.info("draft item added user=%s draft=%s", user_id, draft_id)
    return serialize_draft(updated)


def update_draft_item(
    user_id: str, draft_id: str, item_id: str, data: Dict[str, Any]
) -> Dict[str, Any]:
    _load_open_draft_for_mutation(user_id, draft_id, data["version"])
    item = repository.get_draft_item(user_id, draft_id, item_id)
    if item is None:
        raise NotFoundError("Item not found.", code="draft_item_not_found")

    food = item.food
    quantity = data.get("quantity", item.quantity)
    unit = data.get("unit", item.unit)
    state = data.get("state", item.state)
    patch, _ = _resolve_draft_item(food, quantity, unit, state)
    # `defaultGrams` is the baseline Adjust Portion deltas are computed
    # against (§5.2.1) - fixed at creation, never moved by an edit.
    patch.pop("defaultGrams")
    repository.update_draft_item(item_id, patch)

    updated = _recompute_totals(user_id, draft_id, bump_version=True)
    logger.info("draft item updated user=%s draft=%s item=%s", user_id, draft_id, item_id)
    return serialize_draft(updated)


def delete_draft_item(user_id: str, draft_id: str, item_id: str, version: int) -> Dict[str, Any]:
    _load_open_draft_for_mutation(user_id, draft_id, version)
    item = repository.get_draft_item(user_id, draft_id, item_id)
    if item is None:
        raise NotFoundError("Item not found.", code="draft_item_not_found")

    repository.delete_draft_item(item_id)
    updated = _recompute_totals(user_id, draft_id, bump_version=True)
    logger.info("draft item removed user=%s draft=%s item=%s", user_id, draft_id, item_id)
    return serialize_draft(updated)


# -- confirm (§9, §12.1, §12.5) -----------------------------------------------


def _request_hash(payload: Dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _today_start() -> datetime:
    return _now().replace(hour=0, minute=0, second=0, microsecond=0)


def _daily_totals(user_id: str) -> NutrientVector:
    todays_meals = meals_repository.list_logged_meals(user_id, logged_after=_today_start())
    vectors = [
        NutrientVector(m.caloriesKcal, m.proteinG, m.carbsG, m.fatG, m.fiberG) for m in todays_meals
    ]
    return sum_nutrition(vectors)


def confirm_draft(
    user_id: str, draft_id: str, idempotency_key: str, version: int
) -> Dict[str, Any]:
    """POST /drafts/{id}/confirm. Idempotency guards logging (§12.1) - a
    double-tap with the same key replays the original response rather than
    creating a second LoggedMeal; the draft's own OPEN->CONFIRMED transition
    is the second, state-machine-level line of defense once the idempotency
    record itself has expired (§7, `IdempotencyRecord.expiresAt`)."""
    request_hash = _request_hash({"draftId": draft_id, "version": version})

    record = repository.get_idempotency_record(idempotency_key)
    if record is not None and record.expiresAt > _now():
        if record.requestHash != request_hash:
            raise IdempotencyKeyReuseError()
        return dict(record.responseBody)

    draft = _load_open_draft_for_mutation(user_id, draft_id, version)

    # Server authority (§12.5): recompute fresh from the current catalog
    # rather than trusting the draft's live-computed-but-still-client-visible
    # numbers. Only RESOLVED items convert into LoggedMealItem rows -
    # UNRESOLVED is unreachable in Chunk 2a, but the filter is here so this
    # doesn't need rewriting once Chunk 2b can produce one.
    items_payload: List[Dict[str, Any]] = []
    vectors: List[NutrientVector] = []
    for item in draft.items:
        if item.resolution != "RESOLVED" or item.food is None:
            continue
        resolved, vector = meals_services.resolve_item(item.food, item.quantity, item.unit, item.state)
        items_payload.append(resolved)
        vectors.append(vector)

    meal_data = dict(
        name=draft.name,
        slot=draft.slot,
        source="CHAT_AI",
        **_totals_payload(sum_nutrition(vectors)),
    )
    logged_meal = meals_repository.create_logged_meal(user_id, meal_data, items_payload)
    repository.update_draft(draft_id, {"status": "CONFIRMED", "version": {"increment": 1}})

    response = {
        "loggedMeal": serialize_logged_meal(logged_meal),
        "dailyTotals": serialize_daily_totals(_daily_totals(user_id)),
    }
    repository.save_idempotency_record(
        idempotency_key,
        user_id,
        request_hash,
        response,
        201,
        _now() + timedelta(hours=IDEMPOTENCY_TTL_HOURS),
    )
    logger.info(
        "draft confirmed user=%s draft=%s meal=%s", user_id, draft_id, logged_meal.id
    )
    return response
