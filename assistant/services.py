"""Meal Assistant draft & chat use cases (PRD §7, §9, §12.1, §12.2, §12.5) -
Chunk 2a's structured (non-text) mutation API, plus Chunk 2b's text pipeline
on top of it. Views stay thin: they validate, call one of these, and render -
see meals/services.py for the pattern this follows, which this module builds
directly on top of (food resolution, the confirm -> LoggedMeal handoff)
rather than duplicating it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from django.conf import settings

import chatparser
from common.middleware import current_request_id
from common.exceptions import (
    DraftNotOpenError,
    DraftVersionConflictError,
    EstimatedDishNotEditableError,
    IdempotencyKeyReuseError,
    NotFoundError,
    OpenDraftExistsError,
    OperationExpiredError,
    StaleOperationError,
    UnresolvableQuantityError,
)
from chatparser.units import UNIT_WORDS
from engine.rounding import round_int
from llm import LLMCallError, LLMConfigurationError, call_large_model, call_small_model
from llm.prompts import SYSTEM_PROMPT
from meals import repository as meals_repository
from meals import services as meals_services
from meals.serializers import serialize_logged_meal
from onboarding import repository as onboarding_repository
from nutrition import (
    CATEGORY_FALLBACK_GRAMS,
    SIZE_QUALIFIER_MULTIPLIERS,
    ZERO_VECTOR,
    NutrientVector,
    UnknownServingUnitError,
    estimated_dish_nutrition,
    resolve_grams,
    sum_nutrition,
)

from . import repository
from .serializers import (
    IntentEnvelopeSerializer,
    item_nutrient_vector,
    serialize_daily_totals,
    serialize_draft,
)

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

_GREETING_REPLY = "Hey \U0001F44B What did you eat?"
_NO_FOOD_REPLY = "I couldn't work out what food that was — try naming the dish, or search for it."
# §7.6.1's own worked example ("Misal Pav... ~300g") - used for an estimated
# dish whose item has no stated quantity+unit, or an unrecognized one (only
# g/kg are meaningful for a dish with no serving-unit table of its own).
_DISH_DEFAULT_SERVING_GRAMS = 300.0

# -- non-logging intents (Chunk 6a, §5.5) - short, scripted, ends by pointing
# back to logging or the right surface, per §5.5's own design principle.
_APP_HELP_REPLY = (
    "I'm just for logging meals — you can change goals, units, and reminders from "
    "Settings, or check the Help Center for anything else."
)
_DIARY_QUERY_TREND_REPLY = "For trends over time, check Insights — I only answer for today here."
_DIARY_QUERY_NO_MEALS_REPLY = "You haven't logged anything today yet — what did you eat?"
_NUTRITION_QA_NO_FOOD_REPLY = "Tell me a specific food and I'll look up its numbers."
_NUTRITION_QA_NOT_FOUND_REPLY = "I couldn't find that in the catalog — try searching for it in Meals."
_ADVICE_SEEKING_REPLY = (
    "I can't give dietary or medical advice — that's best discussed with a registered "
    "dietitian or your doctor. I can help you log what you eat though!"
)
_UNCLEAR_REPLY = "Not sure I follow — what did you eat?"
_OTHER_REPLY = "I stick to logging meals — what did you eat?"

# -- WELLBEING_FLAG (Chunk 6b, §5.6) -----------------------------------------
# PLACEHOLDER COPY - pending clinical/trust-and-safety review (§5.6's own
# framing: "written here as a requirement, not a finished policy"). No
# numbers of any kind (calorie targets, deficit maths, fasting durations),
# no lecturing, no diagnosis - just a short, supportive acknowledgement.
_WELLBEING_FLAG_REPLY = (
    "That sounds really hard, and I'm glad you shared it. I'm not the right place for this, "
    "but you don't have to sit with it alone — reaching out to someone you trust or a support "
    "line can help. I'm still here whenever you want to log something."
)


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


def _resolve_draft_item(food: Any, quantity: float, unit: str, state: Optional[str]) -> Tuple[Dict[str, Any], NutrientVector]:
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


_TOTALED_RESOLUTIONS = ("RESOLVED", "ESTIMATED_DISH")


def _draft_totals(draft: Any) -> NutrientVector:
    # An estimated dish counts toward totals at its midpoint (§7.6.1) -
    # excluding it would understate the day, a worse error than a flagged
    # approximation.
    return sum_nutrition(
        item_nutrient_vector(item) for item in draft.items if item.resolution in _TOTALED_RESOLUTIONS
    )


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


# -- quantity-resolution ladder write-back (Chunk 4b, §5.1.1a) ---------------


def _record_serving_observations(user_id: str, items_payload: List[Dict[str, Any]]) -> None:
    """"Every portion edit writes to a per-user serving profile" (§5.1.1a).
    Called from every choke point a `MealDraftItem` payload is actually
    persisted through (create, add, and edit), so it fires regardless of
    which surface produced the item. An `ASSUMED` item is the ladder's own
    guess, not a real observation - recording it back would let a wrong
    guess reinforce itself, so only `EXPLICIT` (stated) items count."""
    for item_payload in items_payload:
        if item_payload.get("resolution") != "RESOLVED":
            continue
        if item_payload.get("quantitySource") != "EXPLICIT":
            continue
        repository.record_serving_observation(
            user_id, item_payload["foodId"], item_payload["state"], item_payload["grams"]
        )


# -- draft lifecycle ----------------------------------------------------------


def _create_draft_from_items(
    user_id: str,
    name: str,
    slot: str,
    parse_tier: str,
    confidence: float,
    items_payload: List[Dict[str, Any]],
    vectors: List[NutrientVector],
) -> Any:
    """Shared core (Chunk 2a's structured `create_draft` and Chunk 2b's text
    pipeline both funnel through this): given already-resolved
    `MealDraftItem` payloads - RESOLVED and/or UNRESOLVED, an UNRESOLVED item
    contributing `ZERO_VECTOR` - and their nutrient vectors, create the draft
    via the transactional expiry-checked write. Returns the raw created row."""
    session = repository.get_or_create_today_session(user_id)
    draft_data = dict(
        name=name,
        slot=slot,
        parseTier=parse_tier,
        confidence=confidence,
        expiresAt=_now() + timedelta(hours=DRAFT_TTL_HOURS),
        **_totals_payload(sum_nutrition(vectors)),
    )
    created = repository.create_draft_with_expiry_check(user_id, session.id, draft_data, items_payload)
    if created is None:
        existing = repository.get_open_draft(user_id)
        raise OpenDraftExistsError(details={"draft": serialize_draft(existing)})
    _record_serving_observations(user_id, items_payload)
    repository.record_draft_operation(
        created.id,
        "CREATE",
        {"name": name, "slot": slot, "parseTier": parse_tier, "itemCount": len(items_payload)},
        created.version,
    )
    return created


def create_draft(user_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """POST /drafts (§4's "fully quantified" use case, chat-shaped). One open
    draft per user (§9, §12.2) - enforced by transactional lazy expiry at
    write time; see assistant.repository.create_draft_with_expiry_check for
    why there's no DB-level constraint backing it yet. `opId` (optional,
    §12.12) makes a queued-offline retry of this call idempotent."""

    def _do() -> Dict[str, Any]:
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
        created = _create_draft_from_items(user_id, data["name"], slot, "MANUAL", 1.0, items_payload, vectors)
        logger.info("draft created user=%s draft=%s items=%s", user_id, created.id, len(items_payload))
        return serialize_draft(created)

    return _idempotent(
        data.get("opId"),
        user_id,
        {"op": "CREATE_DRAFT", "name": data["name"], "slot": data.get("slot"), "items": data["items"]},
        201,
        _do,
    )


def fetch_draft(user_id: str, draft_id: str) -> Dict[str, Any]:
    draft = repository.get_draft(user_id, draft_id)
    if draft is None:
        raise NotFoundError("Draft not found.", code="draft_not_found")
    draft = repository.expire_draft_if_stale(draft)
    return serialize_draft(draft)


def update_draft(user_id: str, draft_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    def _do() -> Dict[str, Any]:
        _load_open_draft_for_mutation(user_id, draft_id, data["version"])
        fields = {k: v for k, v in data.items() if k in ("name", "slot")}
        patch = dict(fields, version={"increment": 1})
        updated = repository.update_draft(draft_id, patch)
        # No CONFIRM/DISCARD/EXPIRE op value (§12.2's literal DraftOperation
        # sketch) - a version-only bump from elsewhere never reaches this
        # function, so `fields` is never empty here in practice, but the guard
        # keeps this honest if that ever changes.
        if "slot" in fields:
            repository.record_draft_operation(draft_id, "SET_SLOT", fields, updated.version)
        if "name" in fields:
            repository.record_draft_operation(draft_id, "RENAME", fields, updated.version)
        return serialize_draft(updated)

    return _idempotent(
        data.get("opId"),
        user_id,
        {"op": "SET_SLOT", "draftId": draft_id, "name": data.get("name"), "slot": data.get("slot")},
        200,
        _do,
    )


def discard_draft(user_id: str, draft_id: str, version: int) -> Dict[str, Any]:
    _load_open_draft_for_mutation(user_id, draft_id, version)
    updated = repository.update_draft(draft_id, {"status": "DISCARDED", "version": {"increment": 1}})
    logger.info("draft discarded user=%s draft=%s", user_id, draft_id)
    return serialize_draft(updated)


# -- draft items (Adjust Portion persistence, §5.2.1) ------------------------


def _add_item_payload(user_id: str, draft_id: str, item_payload: Dict[str, Any]) -> Any:
    """Shared core: persist an already-built `MealDraftItem` payload
    (RESOLVED or UNRESOLVED) and recompute totals. The caller is responsible
    for having already validated the draft is OPEN and at the expected
    version - `add_draft_item` (structured input) and Chunk 2b's text-driven
    ADD_ITEM both do that themselves before reaching here, since they resolve
    the item differently (exact `foodId` vs. a fuzzy-matched food name)."""
    repository.create_draft_item(draft_id, item_payload)
    _record_serving_observations(user_id, [item_payload])
    updated = _recompute_totals(user_id, draft_id, bump_version=True)
    repository.record_draft_operation(draft_id, "ADD_ITEM", item_payload, updated.version)
    return updated


def add_draft_item(user_id: str, draft_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    def _do() -> Dict[str, Any]:
        _load_open_draft_for_mutation(user_id, draft_id, data["version"])
        food = meals_repository.get_food(data["foodId"])
        if food is None:
            raise NotFoundError(
                "Food not found.", code="food_not_found", details={"foodId": data["foodId"]}
            )
        item_payload, _ = _resolve_draft_item(food, data["quantity"], data["unit"], data.get("state"))
        updated = _add_item_payload(user_id, draft_id, item_payload)
        logger.info("draft item added user=%s draft=%s", user_id, draft_id)
        return serialize_draft(updated)

    return _idempotent(
        data.get("opId"),
        user_id,
        {
            "op": "ADD_ITEM",
            "draftId": draft_id,
            "foodId": data["foodId"],
            "quantity": data["quantity"],
            "unit": data["unit"],
            "state": data.get("state"),
        },
        201,
        _do,
    )


def update_draft_item(
    user_id: str, draft_id: str, item_id: str, data: Dict[str, Any]
) -> Dict[str, Any]:
    def _do() -> Dict[str, Any]:
        _load_open_draft_for_mutation(user_id, draft_id, data["version"])
        item = repository.get_draft_item(user_id, draft_id, item_id)
        if item is None:
            raise NotFoundError("Item not found.", code="draft_item_not_found")
        if item.resolution == "ESTIMATED_DISH":
            # Adjust Portion doesn't apply to an estimated dish (§7.6.1's own
            # mockup offers only "Break into ingredients"/"Find this food," never
            # a quantity slider) - reject explicitly rather than crash below on
            # a `None` food.
            raise EstimatedDishNotEditableError()

        food = item.food
        quantity = data.get("quantity", item.quantity)
        unit = data.get("unit", item.unit)
        state = data.get("state", item.state)
        patch, _ = _resolve_draft_item(food, quantity, unit, state)
        # `defaultGrams` is the baseline Adjust Portion deltas are computed
        # against (§5.2.1) - fixed at creation, never moved by an edit.
        patch.pop("defaultGrams")
        repository.update_draft_item(item_id, patch)
        _record_serving_observations(user_id, [patch])

        updated = _recompute_totals(user_id, draft_id, bump_version=True)
        repository.record_draft_operation(draft_id, "EDIT_ITEM", dict(patch, itemId=item_id), updated.version)
        logger.info("draft item updated user=%s draft=%s item=%s", user_id, draft_id, item_id)
        return serialize_draft(updated)

    return _idempotent(
        data.get("opId"),
        user_id,
        {
            "op": "EDIT_ITEM",
            "draftId": draft_id,
            "itemId": item_id,
            "quantity": data.get("quantity"),
            "unit": data.get("unit"),
            "state": data.get("state"),
        },
        200,
        _do,
    )


def delete_draft_item(
    user_id: str, draft_id: str, item_id: str, version: int, op_id: Optional[str] = None
) -> Dict[str, Any]:
    def _do() -> Dict[str, Any]:
        _load_open_draft_for_mutation(user_id, draft_id, version)
        item = repository.get_draft_item(user_id, draft_id, item_id)
        if item is None:
            raise NotFoundError("Item not found.", code="draft_item_not_found")

        repository.delete_draft_item(item_id)
        updated = _recompute_totals(user_id, draft_id, bump_version=True)
        repository.record_draft_operation(draft_id, "REMOVE_ITEM", {"itemId": item_id}, updated.version)
        logger.info("draft item removed user=%s draft=%s item=%s", user_id, draft_id, item_id)
        return serialize_draft(updated)

    return _idempotent(
        op_id, user_id, {"op": "REMOVE_ITEM", "draftId": draft_id, "itemId": item_id}, 200, _do
    )


# -- confirm (§9, §12.1, §12.5) -----------------------------------------------


def _request_hash(payload: Dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _request_id_or_none() -> Optional[str]:
    """Correlation chain (§12.9, Chunk 8b). `"-"` is `common.middleware`'s own
    sentinel for "no request in flight" - normalized to `None` here so the
    stored column means "attached to a real request" or nothing, never a
    placeholder string that looks like data."""
    request_id = current_request_id()
    return request_id if request_id != "-" else None


def _idempotent(
    op_id: Optional[str],
    user_id: str,
    request_payload: Dict[str, Any],
    status_code: int,
    fn: Callable[[], Dict[str, Any]],
    ttl_hours: Optional[float] = None,
) -> Dict[str, Any]:
    """Shared opId-based idempotency (§12.1, §12.12) - a double-tap (or a
    queued-offline retry, §12.12) with the same key replays the original
    response rather than re-running `fn`, which is what makes every mutating
    endpoint safe for a client to blindly retry. `op_id=None` runs `fn()`
    directly with no bookkeeping at all - idempotency is opt-in per request
    (every structured endpoint's own `opId` field is optional), so a caller
    that never sends one - today's online path - is completely unaffected.
    `ttl_hours` defaults to `settings.REPLAY_WINDOW_HOURS` (§12.12's
    `MAX_QUEUE_AGE + retry tail`); `send_message` is the one caller that
    passes its own shorter window, since descriptive/AI chat input isn't a
    queueable `opType` at all."""
    if op_id is None:
        return fn()
    if ttl_hours is None:
        ttl_hours = settings.REPLAY_WINDOW_HOURS

    request_hash = _request_hash(request_payload)
    record = repository.get_idempotency_record(op_id)
    if record is not None and record.expiresAt > _now():
        if record.requestHash != request_hash:
            raise IdempotencyKeyReuseError()
        return dict(record.responseBody)

    response = fn()
    repository.save_idempotency_record(
        op_id, user_id, request_hash, response, status_code, _now() + timedelta(hours=ttl_hours)
    )
    return response


def _today_start() -> datetime:
    return _now().replace(hour=0, minute=0, second=0, microsecond=0)


def _daily_totals(user_id: str) -> NutrientVector:
    todays_meals = meals_repository.list_logged_meals(user_id, logged_after=_today_start())
    vectors = [
        NutrientVector(m.caloriesKcal, m.proteinG, m.carbsG, m.fatG, m.fiberG) for m in todays_meals
    ]
    return sum_nutrition(vectors)


def confirm_draft(
    user_id: str,
    draft_id: str,
    idempotency_key: str,
    version: int,
    meal_timestamp: Optional[datetime] = None,
    stale_confirmed: bool = False,
    nutrition_snapshot: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """POST /drafts/{id}/confirm. Idempotency guards logging (§12.1) - a
    double-tap with the same key replays the original response rather than
    creating a second LoggedMeal; the draft's own OPEN->CONFIRMED transition
    is the second, state-machine-level line of defense once the idempotency
    record itself has expired (§7, `IdempotencyRecord.expiresAt`).

    §12.12's queued-replay fields, all optional and `None`/`False` on today's
    online path: `meal_timestamp` is when the user actually ate (not when
    this call reached the server) - validated against the staleness/expiry
    windows *before* anything is written, then becomes `LoggedMeal.loggedAt`
    directly, so a backdated offline meal lands on the day it happened, not
    the day it synced. `nutrition_snapshot` is what the client last showed
    the user; a difference from the server's freshly-recomputed total beyond
    `NUTRITION_DRIFT_EPSILON_KCAL` surfaces a one-time note in the response
    (never persisted - §12.5's server-authority recompute is unaffected
    either way, this only decides whether to tell the client about it)."""

    def _do() -> Dict[str, Any]:
        if meal_timestamp is not None:
            age = _now() - meal_timestamp
            if age > timedelta(days=settings.MAX_QUEUE_AGE_DAYS):
                raise OperationExpiredError()
            if age > timedelta(days=settings.STALE_QUEUE_AGE_DAYS) and not stale_confirmed:
                raise StaleOperationError()

        draft = _load_open_draft_for_mutation(user_id, draft_id, version)

        # Server authority (§12.5): recompute fresh from the current catalog
        # rather than trusting the draft's live-computed-but-still-client-visible
        # numbers. Only RESOLVED/ESTIMATED_DISH items convert into LoggedMealItem
        # rows - UNRESOLVED never does, same as before this chunk.
        items_payload: List[Dict[str, Any]] = []
        vectors: List[NutrientVector] = []
        for item in draft.items:
            if item.resolution == "RESOLVED" and item.food is not None:
                resolved, vector = meals_services.resolve_item(
                    item.food, item.quantity, item.unit, item.state
                )
                items_payload.append(resolved)
                vectors.append(vector)
            elif item.resolution == "ESTIMATED_DISH":
                # Recomputed against the *current* DishCategoryProfile, exactly
                # like a RESOLVED item recomputes against the current Food row -
                # never the draft's own stored range (§12.5). A since-removed
                # profile drops the item silently, same posture as a
                # since-deleted food in the RESOLVED branch above.
                resolved_dish = meals_services.resolve_estimated_dish_item(
                    item.dishCategory, item.quantity, item.unit, item.grams, item.rawText
                )
                if resolved_dish is not None:
                    items_payload.append(resolved_dish[0])
                    vectors.append(resolved_dish[1])

        totals = sum_nutrition(vectors)
        meal_data = dict(
            name=draft.name,
            slot=draft.slot,
            source="CHAT_AI",
            catalogVersion=meals_repository.get_catalog_version(),
            nutritionEngineVersion=settings.NUTRITION_ENGINE_VERSION,
            **_totals_payload(totals),
        )
        if meal_timestamp is not None:
            meal_data["loggedAt"] = meal_timestamp
        logged_meal = meals_repository.create_logged_meal(user_id, meal_data, items_payload)
        repository.update_draft(draft_id, {"status": "CONFIRMED", "version": {"increment": 1}})

        recomputed_from_snapshot = False
        if nutrition_snapshot is not None:
            snapshot_kcal = nutrition_snapshot.get("caloriesKcal")
            if (
                snapshot_kcal is not None
                and abs(totals.calories_kcal - snapshot_kcal) > settings.NUTRITION_DRIFT_EPSILON_KCAL
            ):
                recomputed_from_snapshot = True

        logger.info(
            "draft confirmed user=%s draft=%s meal=%s", user_id, draft_id, logged_meal.id
        )
        return {
            "loggedMeal": serialize_logged_meal(logged_meal),
            "dailyTotals": serialize_daily_totals(_daily_totals(user_id)),
            "recomputedFromClientSnapshot": recomputed_from_snapshot,
        }

    return _idempotent(
        idempotency_key,
        user_id,
        {
            "op": "CONFIRM_LOG",
            "draftId": draft_id,
            "version": version,
            "mealTimestamp": meal_timestamp.isoformat() if meal_timestamp else None,
        },
        201,
        _do,
    )


# -- Chunk 2b: text pipeline (§7, §7.5, §12.6) -------------------------------


def _resolve_food_by_name(query: str) -> Tuple[Optional[Any], float, str]:
    """Best-matching catalog food for free text, with its score and band
    (§12.6). Candidates come from every food in the catalog, scored in Python
    via `difflib` (the Chunk 2a decision on trigram vs. pure-Python)."""
    candidates = meals_repository.search_foods("", limit=500)
    if not candidates:
        return None, 0.0, "LOW"
    scored = [(food, chatparser.score_food_match(query, food.name)) for food in candidates]
    best_food, best_score = max(scored, key=lambda pair: pair[1])
    return best_food, best_score, chatparser.band_for_score(best_score)


def _unresolved_item(phrase: "chatparser.ParsedItemPhrase") -> Dict[str, Any]:
    return {"resolution": "UNRESOLVED", "rawText": phrase.raw_text}


def _resolve_composite_by_name(query: str) -> Optional[Any]:
    """Exact, normalized match against every composite's `name`/`aliases`
    (§7.6 - "a catalog lookup, not a language task"). Deliberately NOT the
    fuzzy partial-ratio scorer individual foods use: a single word that's
    genuinely part of a composite's name ("dal" in "Dal Chawal") scores a
    perfect partial-ratio match, which would wrongly expand a plain mention
    of the standalone food into the composite. A curator adds real shorthand
    ("biryani") as an alias instead of the system guessing from fragments."""
    normalized_query = query.strip().lower()
    for composite in meals_repository.get_composite_foods():
        names = {composite.name.strip().lower()} | {a.strip().lower() for a in composite.aliases}
        if normalized_query in names:
            return composite
    return None


def _expand_composite(
    composite: Any,
    quantity: float,
    quantity_source: str = "EXPLICIT",
    mass_source: str = "DIRECT",
) -> List[Tuple[Dict[str, Any], NutrientVector]]:
    """A matched dish + a serving count -> one `RESOLVED` payload per
    component. Each component's mass is `quantity * composite.servingGrams *
    component.ratioOfServing`, passed through `meals.services.resolve_item`
    (unit "g", already computed as grams) rather than computed by hand, so a
    component whose curated `state` differs from its food's own
    `defaultState` still goes through the same tested yield conversion
    everything else does."""
    total_grams = quantity * composite.servingGrams
    items: List[Tuple[Dict[str, Any], NutrientVector]] = []
    for component in composite.components:
        component_grams = total_grams * component.ratioOfServing
        component_state = component.state if component.state != "UNSPECIFIED" else None
        resolved, vector = meals_services.resolve_item(
            component.food, component_grams, "g", component_state
        )
        payload = {
            "resolution": "RESOLVED",
            "foodId": resolved["foodId"],
            "rawText": "{} ({})".format(component.food.name, composite.name),
            "quantity": resolved["quantity"],
            "unit": resolved["unit"],
            "grams": resolved["grams"],
            "state": resolved["state"],
            "defaultGrams": resolved["grams"],
            "prep": component.prep,
            "quantitySource": quantity_source,
            "massSource": mass_source,
            # The *dish* was matched, not this component individually -
            # attributing a fuzzy score to it would misrepresent what
            # actually happened.
            "matchScore": None,
            "matchBand": None,
        }
        items.append((payload, vector))
    return items


def _build_items_from_phrase(
    phrase: "chatparser.ParsedItemPhrase",
) -> List[Tuple[Dict[str, Any], NutrientVector]]:
    """One `ParsedItemPhrase` -> one or more `MealDraftItem` payloads (plural:
    a composite match expands to every component) plus their nutrient
    vectors. Composite dish name -> expand (§7.6). Otherwise, individual food
    matching as before: HIGH/MEDIUM band -> RESOLVED; LOW/no match, or a
    matched food whose serving table doesn't have this unit -> UNRESOLVED and
    filed to the miss queue (§9, I8). Never raises: a parse-quality problem
    degrades to an unresolved item, not an error (§12.13's spirit)."""
    composite = _resolve_composite_by_name(phrase.food_text)
    if composite is not None:
        logger.info(
            "composite expanded dish=%s components=%s curated=%s",
            composite.name,
            len(composite.components),
            composite.isCurated,
        )
        return _expand_composite(
            composite,
            phrase.quantity,
            quantity_source=phrase.quantity_source,
            mass_source=phrase.mass_source or "DIRECT",
        )

    food, score, band = _resolve_food_by_name(phrase.food_text)
    if food is None or band == "LOW":
        meals_repository.file_food_miss(phrase.food_text)
        return [(_unresolved_item(phrase), ZERO_VECTOR)]

    try:
        resolved, vector = meals_services.resolve_item(food, phrase.quantity, phrase.unit, phrase.state)
    except UnresolvableQuantityError:
        meals_repository.file_food_miss(phrase.food_text)
        return [(_unresolved_item(phrase), ZERO_VECTOR)]

    payload = {
        "resolution": "RESOLVED",
        "foodId": resolved["foodId"],
        "rawText": phrase.raw_text,
        "quantity": resolved["quantity"],
        "unit": resolved["unit"],
        "grams": resolved["grams"],
        "state": resolved["state"],
        "defaultGrams": resolved["grams"],
        "prep": phrase.prep,
        "quantitySource": phrase.quantity_source,
        "massSource": phrase.mass_source or _mass_source(resolved["unit"]),
        "matchScore": score,
        "matchBand": band,
    }
    return [(payload, vector)]


def _resolve_target_ref(text: str, draft_items: List[Any]) -> Tuple[Optional[Any], bool]:
    """Fuzzy-matches an edit/remove target against the *open draft's own
    item names* (not the whole catalog) - §7.5. Returns `(item, is_ambiguous)`:
    0 matches, or a close second-best, means ambiguous - no mutation, ask
    once rather than silently editing the wrong row."""
    candidates = [i for i in draft_items if i.resolution == "RESOLVED" and i.food is not None]
    if not candidates:
        return None, True

    scored = sorted(
        ((item, chatparser.score_food_match(text, item.food.name)) for item in candidates),
        key=lambda pair: pair[1],
        reverse=True,
    )
    best_item, best_score = scored[0]
    if best_score < chatparser.MEDIUM_THRESHOLD:
        return None, True
    if len(scored) > 1 and (best_score - scored[1][1]) < 0.1:
        return None, True
    return best_item, False


def _derive_meal_name(
    items_payload: List[Dict[str, Any]],
    vectors: List[NutrientVector],
    fallback_food_text: Optional[str],
    slot: str,
) -> str:
    """§5.1.3's default naming template: `"{Slot} — {top item by calorie
    contribution}"`, title-cased, at most 4 words. This is the "simple
    meals" case only - the fuller "dominant protein/grain + form factor"
    phrasing needs a `Food.category`/form-factor concept the catalog doesn't
    have yet, so "top item by calories" is an honestly-scoped proxy for
    "dominant" rather than a guess at the full algorithm. Used for every
    new-meal draft (T1 and T2 both) - naming was never tier-specific in the
    PRD; `fallback_food_text` only fires when nothing resolved at all."""
    best_food_id: Optional[str] = None
    best_calories = -1.0
    for item_payload, vector in zip(items_payload, vectors):
        if item_payload["resolution"] != "RESOLVED":
            continue
        if vector.calories_kcal > best_calories:
            best_calories = vector.calories_kcal
            best_food_id = item_payload["foodId"]

    sample = fallback_food_text
    if best_food_id is not None:
        food = meals_repository.get_food(best_food_id)
        if food is not None:
            sample = food.name

    if not sample:
        return "{} meal".format(slot.title())

    words = sample.title().split()[:4]
    return "{} — {}".format(slot.title(), " ".join(words))


@dataclass
class _Outcome:
    tier: str
    intent: str
    assistant_text: str
    draft: Optional[Dict[str, Any]] = None
    draft_id: Optional[str] = None
    unconsumed_text: List[str] = field(default_factory=list)
    needs_clarification: Optional[Dict[str, Any]] = None
    parse_snapshot: Optional[Dict[str, Any]] = None
    quota_exceeded: bool = False
    gamification_suppressed: bool = False
    wellbeing_resources: Optional[List[Dict[str, str]]] = None


def _apply_edit(user_id: str, draft: Any, edit: "chatparser.ParsedEdit") -> _Outcome:
    if edit.intent == "SET_SLOT":
        updated = update_draft(user_id, draft.id, {"slot": edit.slot, "version": draft.version})
        return _Outcome(
            tier="PARSER",
            intent="SET_SLOT",
            assistant_text="Got it — logged as {}.".format(edit.slot.title()),
            draft=updated,
            draft_id=draft.id,
        )

    if edit.intent == "ADD_ITEM":
        _load_open_draft_for_mutation(user_id, draft.id, draft.version)
        items = _build_items_from_phrase(edit.item)
        updated_row = None
        for item_payload, _ in items:
            updated_row = _add_item_payload(user_id, draft.id, item_payload)
        all_resolved = all(ip["resolution"] == "RESOLVED" for ip, _ in items)
        assistant_text = (
            "Added it to your meal."
            if all_resolved
            else "I couldn't find that food — search for it or tap to edit."
        )
        return _Outcome(
            tier="PARSER",
            intent="ADD_ITEM",
            assistant_text=assistant_text,
            draft=serialize_draft(updated_row),
            draft_id=draft.id,
        )

    # EDIT_ITEM / REMOVE_ITEM both resolve targetRef against the draft's own items.
    target_item, ambiguous = _resolve_target_ref(edit.target_text, draft.items)
    if ambiguous:
        candidates = sorted({i.food.name for i in draft.items if i.resolution == "RESOLVED" and i.food})
        assistant_text = (
            "Which one — {}?".format(" or ".join(candidates))
            if candidates
            else "I'm not sure which item you mean."
        )
        return _Outcome(
            tier="PARSER",
            intent=edit.intent,
            assistant_text=assistant_text,
            draft=serialize_draft(draft),
            draft_id=draft.id,
            needs_clarification={"reason": "ambiguous_target", "candidates": candidates},
        )

    if edit.intent == "EDIT_ITEM":
        updated = update_draft_item(
            user_id,
            draft.id,
            target_item.id,
            {"quantity": edit.quantity, "unit": edit.unit, "version": draft.version},
        )
        assistant_text = "Updated {}.".format(target_item.food.name)
    else:  # REMOVE_ITEM
        updated = delete_draft_item(user_id, draft.id, target_item.id, draft.version)
        assistant_text = "Removed {}.".format(target_item.food.name)

    return _Outcome(tier="PARSER", intent=edit.intent, assistant_text=assistant_text, draft=updated, draft_id=draft.id)


def _apply_ai_edit(user_id: str, draft: Any, envelope: Dict[str, Any]) -> Optional[_Outcome]:
    """The T2-envelope counterpart to `_apply_edit` (Chunk 5a, §7.5) - used
    when an open-draft message misses both T1's edit grammar and T1's
    new-item grammar. Returns `None` for anything not actionable here (a
    LOG_NEW/OTHER-classified envelope, or a missing targetRef/slot/items) so
    the caller falls back to `_NO_FOOD_REPLY` rather than guessing."""
    intent = envelope["intent"]

    if intent == "SET_SLOT":
        if not envelope.get("slot"):
            return None
        updated = update_draft(user_id, draft.id, {"slot": envelope["slot"], "version": draft.version})
        return _Outcome(
            tier="LLM_SMALL",
            intent="SET_SLOT",
            assistant_text="Got it — logged as {}.".format(envelope["slot"].title()),
            draft=updated,
            draft_id=draft.id,
        )

    if intent == "ADD_ITEM":
        if not envelope["items"]:
            return None
        _load_open_draft_for_mutation(user_id, draft.id, draft.version)
        items_payload, _vectors, unconsumed = _resolve_envelope_items_with_dish(envelope, user_id)
        if not items_payload:
            return None
        updated_row = None
        for item_payload in items_payload:
            updated_row = _add_item_payload(user_id, draft.id, item_payload)
        all_resolved = all(ip["resolution"] == "RESOLVED" for ip in items_payload)
        assistant_text = (
            "Added it to your meal."
            if all_resolved
            else "I couldn't find that food — search for it or tap to edit."
        )
        return _Outcome(
            tier="LLM_SMALL",
            intent="ADD_ITEM",
            assistant_text=assistant_text,
            draft=serialize_draft(updated_row),
            draft_id=draft.id,
            unconsumed_text=unconsumed,
        )

    if intent not in ("EDIT_ITEM", "REMOVE_ITEM"):
        # LOG_NEW/OTHER/etc. on an open-draft message - not actionable here;
        # the ADD/NEW clarification dance stays a T1-only concept for now
        # (Chunk 4a's original router-scope note).
        return None

    target_ref = envelope.get("targetRef")
    if not target_ref:
        return None

    target_item, ambiguous = _resolve_target_ref(target_ref, draft.items)
    if ambiguous:
        candidates = sorted({i.food.name for i in draft.items if i.resolution == "RESOLVED" and i.food})
        assistant_text = (
            "Which one — {}?".format(" or ".join(candidates))
            if candidates
            else "I'm not sure which item you mean."
        )
        return _Outcome(
            tier="LLM_SMALL",
            intent=intent,
            assistant_text=assistant_text,
            draft=serialize_draft(draft),
            draft_id=draft.id,
            needs_clarification={"reason": "ambiguous_target", "candidates": candidates},
        )

    if intent == "REMOVE_ITEM":
        updated = delete_draft_item(user_id, draft.id, target_item.id, draft.version)
        return _Outcome(
            tier="LLM_SMALL",
            intent="REMOVE_ITEM",
            assistant_text="Removed {}.".format(target_item.food.name),
            draft=updated,
            draft_id=draft.id,
        )

    # EDIT_ITEM - the new quantity/unit/state is carried on the envelope's
    # own item entry (§7.3's schema has nowhere else to put it); that item's
    # `food` field is ignored here, redundant with targetRef.
    if not envelope["items"]:
        return None
    phrase = _llm_item_to_phrase(envelope["items"][0])
    if phrase is None:
        return None  # no usable quantity+unit to apply - fall back rather than guess
    updated = update_draft_item(
        user_id,
        draft.id,
        target_item.id,
        {"quantity": phrase.quantity, "unit": phrase.unit, "version": draft.version},
    )
    return _Outcome(
        tier="LLM_SMALL",
        intent="EDIT_ITEM",
        assistant_text="Updated {}.".format(target_item.food.name),
        draft=updated,
        draft_id=draft.id,
    )


def _apply_add_phrases(
    user_id: str, draft: Any, phrases: List["chatparser.ParsedItemPhrase"], unconsumed: List[str]
) -> _Outcome:
    _load_open_draft_for_mutation(user_id, draft.id, draft.version)
    updated_row = None
    for phrase in phrases:
        for item_payload, _ in _build_items_from_phrase(phrase):
            updated_row = _add_item_payload(user_id, draft.id, item_payload)
    return _Outcome(
        tier="PARSER",
        intent="ADD_ITEM",
        assistant_text="Added it to your meal.",
        draft=serialize_draft(updated_row),
        draft_id=draft.id,
        unconsumed_text=unconsumed,
    )


_COST_SETTINGS_BY_TIER = {
    "LLM_SMALL": (
        "OPENAI_SMALL_MODEL_INPUT_COST_PER_1M_MICROS",
        "OPENAI_SMALL_MODEL_OUTPUT_COST_PER_1M_MICROS",
    ),
    "LLM_LARGE": (
        "OPENAI_LARGE_MODEL_INPUT_COST_PER_1M_MICROS",
        "OPENAI_LARGE_MODEL_OUTPUT_COST_PER_1M_MICROS",
    ),
}


def _cost_micros(tier: str, prompt_tokens: int, output_tokens: int) -> int:
    input_setting, output_setting = _COST_SETTINGS_BY_TIER[tier]
    input_cost = prompt_tokens * getattr(settings, input_setting) / 1_000_000
    output_cost = output_tokens * getattr(settings, output_setting) / 1_000_000
    return int(round(input_cost + output_cost))


def _envelope_confidence(envelope: Dict[str, Any]) -> float:
    items = envelope.get("items") or []
    return sum(item["confidence"] for item in items) / len(items) if items else 0.0


def _call_llm(
    user_id: str,
    content: str,
    tier: str,
    call_fn: Any,
    counted_to_quota: bool,
) -> Optional[Dict[str, Any]]:
    """Escalates one message to a model (§7.1's exception layer - T2,
    triggered when T1 finds zero phrases, or T3, triggered by a
    low-confidence T2 result; see `_process_new_meal`), validates the
    response through `IntentEnvelopeSerializer` (§12.4: any failure -> treat
    as a parse miss, never a partial draft mutation), and writes a
    `ParseEvent` for the attempt regardless of outcome (§9, I9) -
    `counted_to_quota` records whether *this* call is the one that consumed
    the message's single quota unit (§5.1.4: quota is per-message, not
    per-model-call, so a T3 follow-up on the same message is always
    `False`). Returns the validated envelope dict, or `None` on any failure -
    a provider outage or a malformed response both degrade gracefully,
    never a 500 (§11)."""
    input_hash = chatparser.hash_normalized(content)
    started = time.monotonic()

    try:
        response = call_fn(SYSTEM_PROMPT, content)
    except LLMConfigurationError as exc:
        # A deploy-time misconfiguration (no API key), not a per-call
        # failure - still degrades gracefully rather than a 500 (§11), but
        # at error level since it needs ops attention rather than being an
        # expected, occasional provider hiccup.
        logger.error("llm call skipped, provider not configured user=%s tier=%s: %s", user_id, tier, exc)
        repository.create_parse_event(
            user_id,
            {
                "inputHash": input_hash,
                "tier": tier,
                "intent": "OTHER",
                "countedToQuota": counted_to_quota,
                "latencyMs": int((time.monotonic() - started) * 1000),
                "confidence": 0.0,
                "requestId": _request_id_or_none(),
            },
        )
        return None
    except LLMCallError as exc:
        logger.warning("llm call failed user=%s tier=%s: %s", user_id, tier, exc)
        repository.create_parse_event(
            user_id,
            {
                "inputHash": input_hash,
                "tier": tier,
                "intent": "OTHER",
                "countedToQuota": counted_to_quota,
                "latencyMs": int((time.monotonic() - started) * 1000),
                "confidence": 0.0,
                "requestId": _request_id_or_none(),
            },
        )
        return None

    serializer = IntentEnvelopeSerializer(data=response.raw_envelope)
    if not serializer.is_valid():
        logger.warning(
            "llm envelope failed validation user=%s tier=%s errors=%s", user_id, tier, serializer.errors
        )
        repository.create_parse_event(
            user_id,
            {
                "inputHash": input_hash,
                "tier": tier,
                "intent": "OTHER",
                "countedToQuota": counted_to_quota,
                "model": response.model,
                "promptTokens": response.prompt_tokens,
                "outputTokens": response.output_tokens,
                "costMicros": _cost_micros(tier, response.prompt_tokens, response.output_tokens),
                "latencyMs": response.latency_ms,
                "confidence": 0.0,
                "requestId": _request_id_or_none(),
            },
        )
        return None

    envelope = serializer.validated_data
    confidence = _envelope_confidence(envelope)
    repository.create_parse_event(
        user_id,
        {
            "inputHash": input_hash,
            "tier": tier,
            "intent": envelope["intent"],
            "countedToQuota": counted_to_quota,
            "model": response.model,
            "promptTokens": response.prompt_tokens,
            "outputTokens": response.output_tokens,
            "costMicros": _cost_micros(tier, response.prompt_tokens, response.output_tokens),
            "latencyMs": response.latency_ms,
            "confidence": confidence,
            "requestId": _request_id_or_none(),
        },
    )
    return envelope


def _resolve_assumed_grams(
    food: Any, state: Optional[str], size_qualifier: Optional[str], user_id: str
) -> Optional[Tuple[float, str]]:
    """Quantity-resolution ladder (§5.1.1a) for an already-matched food with
    no stated amount - first match wins:
    1. This user's own history for this food+state, once it has >=3 recent
       observations (a size qualifier this message is ignored when history
       fires - a personal, already-calibrated number beats a generic
       small/large modifier).
    2. The food's canonical serving (`defaultServingGrams`) or, absent that,
       a flat category default (`CATEGORY_FALLBACK_GRAMS`) - whichever base
       applies, scaled by the stated size qualifier (0.7x/1.0x/1.4x, a no-op
       when none was stated).
    Returns `None` when neither the user's history nor the catalog has
    anything to assume - the caller reports the item unconsumed, the same
    honest fallback used everywhere a food can't be resolved."""
    pref = repository.get_serving_preference(user_id, food.id, state or "UNSPECIFIED")
    if pref is not None and len(pref.recentGrams) >= 3:
        return pref.medianGrams, "USER_HISTORY"

    base = food.defaultServingGrams
    mass_source = "CATALOG_SERVING"
    if base is None:
        base = CATEGORY_FALLBACK_GRAMS.get(food.category)
        mass_source = "CATEGORY_FALLBACK"
    if base is None:
        return None

    multiplier = SIZE_QUALIFIER_MULTIPLIERS.get(size_qualifier, 1.0)
    return base * multiplier, mass_source


def _resolve_llm_item_without_quantity(
    llm_item: Dict[str, Any], user_id: str
) -> List[Tuple[Dict[str, Any], NutrientVector]]:
    """A T2 item with no stated quantity/unit -> the quantity-resolution
    ladder, applied to whichever food/composite its name matches. A
    composite defaults to one serving (scaled by any stated size qualifier)
    since it already carries its own canonical `servingGrams` - it never
    touches `UserServingPreference`/`Food.category`, both per-`Food`
    concepts. Returns `[]` when nothing matches, or a matched food's ladder
    has nothing to assume - the caller reports the item unconsumed, exactly
    like any other unresolvable mention."""
    food_text = llm_item["food"]
    size_qualifier = llm_item.get("sizeQualifier")
    multiplier = SIZE_QUALIFIER_MULTIPLIERS.get(size_qualifier, 1.0)

    composite = _resolve_composite_by_name(food_text)
    if composite is not None:
        return _expand_composite(
            composite, 1.0 * multiplier, quantity_source="ASSUMED", mass_source="CATALOG_SERVING"
        )

    food, score, band = _resolve_food_by_name(food_text)
    if food is None or band == "LOW":
        meals_repository.file_food_miss(food_text)
        return []

    state = llm_item.get("state")
    normalized_state = state.upper() if state else None
    assumed = _resolve_assumed_grams(food, normalized_state, size_qualifier, user_id)
    if assumed is None:
        # The food matched fine - it's the serving data that's missing, not
        # the food itself, so this is not a miss-queue case.
        return []
    grams, mass_source = assumed

    phrase = chatparser.ParsedItemPhrase(
        raw_text=food_text,
        quantity=grams,
        unit="g",
        state=normalized_state,
        prep=llm_item.get("prep"),
        food_text=food_text,
        quantity_source="ASSUMED",
        mass_source=mass_source,
    )
    return _build_items_from_phrase(phrase)


def _llm_item_to_phrase(llm_item: Dict[str, Any]) -> Optional["chatparser.ParsedItemPhrase"]:
    """One validated envelope item -> the same `ParsedItemPhrase` T1's own
    regex grammar already produces, when the model reported a quantity *and*
    a unit that normalizes through the same `UNIT_WORDS` vocabulary T1 uses
    (the model might say "grams" in any surface form, not necessarily the
    canonical string a food's serving table expects). This is what lets
    every convertible item flow through the unmodified
    `_build_items_from_phrase` (composite matching, confidence banding,
    miss-queue filing - all of it) with zero new resolution code. Returns
    `None` when quantity/unit is missing or unrecognized - the caller
    (`_process_t2_new_meal`) falls back to `_resolve_llm_item_without_quantity`
    (the quantity-resolution ladder) instead of reporting it unconsumed
    outright."""
    quantity = llm_item.get("quantity")
    unit_text = llm_item.get("unit")
    if quantity is None or not unit_text:
        return None
    unit = UNIT_WORDS.get(unit_text.strip().lower())
    if unit is None:
        return None

    state = llm_item.get("state")
    return chatparser.ParsedItemPhrase(
        raw_text=llm_item["food"],
        quantity=quantity,
        unit=unit,
        state=state.upper() if state else None,
        prep=llm_item.get("prep"),
        food_text=llm_item["food"],
    )


def _resolve_estimated_dish(
    dish_category: str, llm_item: Dict[str, Any]
) -> Optional[Tuple[Dict[str, Any], NutrientVector]]:
    """The envelope's `dishCategory` + its associated item -> an
    `ESTIMATED_DISH` payload (§7.6.1) - never a fabricated ingredient list,
    never a model-authored number. Files the dish name to `FoodMissQueue`
    regardless of outcome (§7.6: "the dish files to FoodMissQueue
    regardless"). Returns `None` when no confident category has a curated
    profile yet ("no confident category -> no number," §7.6.1) - the caller
    reports the item unconsumed, not a crash or a guessed number."""
    dish_name = llm_item["food"]
    meals_repository.file_food_miss(dish_name)

    profile = meals_repository.get_dish_category_profile(dish_category)
    if profile is None:
        return None

    quantity = llm_item.get("quantity")
    unit = llm_item.get("unit")
    grams = None
    if quantity is not None and unit:
        try:
            grams = resolve_grams(quantity, unit, [])
        except UnknownServingUnitError:
            grams = None
    if grams is None:
        quantity, unit, grams = _DISH_DEFAULT_SERVING_GRAMS, "g", _DISH_DEFAULT_SERVING_GRAMS

    kcal_low, kcal_high, kcal_mid = estimated_dish_nutrition(
        profile.caloriesKcalP25Per100g, profile.caloriesKcalP75Per100g, grams
    )
    payload = {
        "resolution": "ESTIMATED_DISH",
        "rawText": dish_name,
        "quantity": quantity,
        "unit": unit,
        "grams": grams,
        "state": "UNSPECIFIED",
        "defaultGrams": grams,
        "dishCategory": dish_category,
        "kcalLow": kcal_low,
        "kcalHigh": kcal_high,
        "kcalMidpoint": kcal_mid,
        "profileVersion": profile.catalogVersion,
    }
    vector = NutrientVector(kcal_mid, None, None, None, None)
    return payload, vector


def _resolve_envelope_items(
    llm_items: List[Dict[str, Any]], user_id: str
) -> Tuple[List[Dict[str, Any]], List[NutrientVector], List[str]]:
    """Every item in a validated envelope -> resolved `MealDraftItem` payloads
    plus their nutrient vectors, and the food names of anything that couldn't
    be resolved at all. An item with a stated quantity+unit resolves via
    `_llm_item_to_phrase`; one without falls to the quantity-resolution
    ladder (`_resolve_llm_item_without_quantity`, §5.1.1a) before finally
    being reported unconsumed. Shared by the `LOG_NEW` path
    (`_process_t2_new_meal`) and the AI-edit `ADD_ITEM` path (`_apply_ai_edit`,
    Chunk 5a) - identical resolution logic either way."""
    items_payload: List[Dict[str, Any]] = []
    vectors: List[NutrientVector] = []
    unconsumed: List[str] = []

    for llm_item in llm_items:
        phrase = _llm_item_to_phrase(llm_item)
        if phrase is not None:
            for item_payload, vector in _build_items_from_phrase(phrase):
                items_payload.append(item_payload)
                vectors.append(vector)
            continue

        ladder_items = _resolve_llm_item_without_quantity(llm_item, user_id)
        if not ladder_items:
            unconsumed.append(llm_item["food"])
            continue
        for item_payload, vector in ladder_items:
            items_payload.append(item_payload)
            vectors.append(vector)

    return items_payload, vectors, unconsumed


def _resolve_envelope_items_with_dish(
    envelope: Dict[str, Any], user_id: str
) -> Tuple[List[Dict[str, Any]], List[NutrientVector], List[str]]:
    """`_resolve_envelope_items`, plus the estimated-dish path (§7.6.1,
    Chunk 5b): when `envelope["dishCategory"]` is set and there's at least
    one item, `items[0]` is that dish (its own `food` field is otherwise
    unused - `targetRef`-style redundancy, same reasoning as `EDIT_ITEM`'s
    item-0 in 5a) and goes through `_resolve_estimated_dish` instead of the
    normal food/ladder resolution; every remaining item is unaffected. A
    pure pass-through to `_resolve_envelope_items` when no dish is present."""
    dish_category = envelope.get("dishCategory")
    llm_items = envelope["items"]
    if not dish_category or not llm_items:
        return _resolve_envelope_items(llm_items, user_id)

    items_payload: List[Dict[str, Any]] = []
    vectors: List[NutrientVector] = []
    unconsumed: List[str] = []

    dish_item = llm_items[0]
    resolved = _resolve_estimated_dish(dish_category, dish_item)
    if resolved is not None:
        items_payload.append(resolved[0])
        vectors.append(resolved[1])
    else:
        unconsumed.append(dish_item["food"])

    rest_payload, rest_vectors, rest_unconsumed = _resolve_envelope_items(llm_items[1:], user_id)
    items_payload.extend(rest_payload)
    vectors.extend(rest_vectors)
    unconsumed.extend(rest_unconsumed)
    return items_payload, vectors, unconsumed


def _process_t2_new_meal(
    user_id: str, envelope: Dict[str, Any], t1_unconsumed: List[str], tier: str
) -> _Outcome:
    """A validated `LOG_NEW` envelope with >=1 item -> the same draft-creation
    core the T1 path uses. `tier` is whichever of LLM_SMALL/LLM_LARGE actually
    produced `envelope` (Chunk 4c: a low-confidence T2 result may have been
    overridden by a T3 call - see `_process_new_meal`)."""
    items_payload, vectors, unresolved = _resolve_envelope_items_with_dish(envelope, user_id)
    unconsumed = list(t1_unconsumed) + unresolved

    if not items_payload:
        return _Outcome(
            tier=tier, intent="OTHER", assistant_text=_NO_FOOD_REPLY, unconsumed_text=unconsumed
        )

    resolved_count = sum(1 for ip in items_payload if ip["resolution"] == "RESOLVED")
    confidence = resolved_count / len(items_payload)
    slot = envelope.get("slot") or _infer_slot(None)
    name = envelope.get("mealName") or _derive_meal_name(
        items_payload, vectors, envelope["items"][0]["food"], slot
    )

    created = _create_draft_from_items(user_id, name, slot, tier, confidence, items_payload, vectors)

    parse_snapshot = None
    if resolved_count == len(items_payload):
        # Only a fully-resolved LOG_NEW is worth caching (§7.4) - mirrors the
        # T1 path exactly, and is what makes this result L1/L2-cacheable
        # (send_message writes whatever parse_snapshot it's given; Chunk 4a
        # never populated one from this path, so a T2-resolved meal never
        # became replayable from cache before this).
        parse_snapshot = {
            "name": name,
            "slot": slot,
            "items": [
                {"foodId": ip["foodId"], "quantity": ip["quantity"], "unit": ip["unit"], "state": ip["state"]}
                for ip in items_payload
            ],
        }

    return _Outcome(
        tier=tier,
        intent="LOG_NEW",
        assistant_text="Got it — let me break that down.",
        draft=serialize_draft(created),
        draft_id=created.id,
        unconsumed_text=unconsumed,
        parse_snapshot=parse_snapshot,
    )


_QUOTA_EXCEEDED_REPLY = 'Quantified meals still work — try "200g rice, 100g chicken".'


def _replay_snapshot(
    user_id: str, snapshot: Dict[str, Any], tier: str, assistant_text: str
) -> Optional[_Outcome]:
    """A cached `{name, slot, items}` snapshot (T0 per-user, or L2 global -
    Chunk 4c - both share this shape, §7.4) -> a freshly-created draft with
    identical items, no parser or model call involved. Returns `None` when
    every cached `foodId` has since vanished from the catalog - the caller
    falls through to a fresh parse rather than creating an empty draft."""
    items_payload: List[Dict[str, Any]] = []
    vectors: List[NutrientVector] = []
    for raw in snapshot["items"]:
        food = meals_repository.get_food(raw["foodId"])
        if food is None:
            continue  # catalog changed since the cache was written
        item_payload, vector = _resolve_draft_item(food, raw["quantity"], raw["unit"], raw.get("state"))
        items_payload.append(item_payload)
        vectors.append(vector)
    if not items_payload:
        return None

    created = _create_draft_from_items(
        user_id, snapshot["name"], snapshot["slot"], tier, 1.0, items_payload, vectors
    )
    return _Outcome(
        tier=tier,
        intent="LOG_NEW",
        assistant_text=assistant_text,
        draft=serialize_draft(created),
        draft_id=created.id,
    )


def _answer_diary_query(user_id: str, normalized: str) -> str:
    """§5.5's DIARY_QUERY - a local database read, never a model call.
    Cross-day/trend questions deep-link to Insights instead of answering
    inline (§5.5: "cross-day trends or analysis -> deep link to Insights,
    don't answer inline") - no date-range analysis is built here."""
    if chatparser.is_diary_query_a_trend_question(normalized):
        return _DIARY_QUERY_TREND_REPLY

    totals = _daily_totals(user_id)
    if totals.calories_kcal == 0:
        return _DIARY_QUERY_NO_MEALS_REPLY

    profile = onboarding_repository.get_profile(user_id)
    if profile is None or profile.plan is None:
        # Onboarding incomplete - a real, common state, not an error. Report
        # what we know rather than a target that doesn't exist yet.
        return "So far today: {} kcal.".format(round_int(totals.calories_kcal))

    remaining = profile.plan.caloriesKcal - totals.calories_kcal
    return "So far today: {} kcal — {} kcal left toward your {} kcal goal.".format(
        round_int(totals.calories_kcal), round_int(remaining), profile.plan.caloriesKcal
    )


def _answer_nutrition_qa(food_text: Optional[str]) -> str:
    """§5.5's NUTRITION_QA - catalog-bound, never a judgment (§5.5's own
    dividing line: report what a food contains, never whether it's good for
    this person). `food_text` comes from the T-1 path's own extraction
    regex, or (T2 path) whatever food the model itself populated in
    `items[0]` - `None` when neither found one, answered with a deflect
    rather than a guess."""
    if not food_text:
        return _NUTRITION_QA_NO_FOOD_REPLY

    food, _score, band = _resolve_food_by_name(food_text)
    if food is None or band == "LOW":
        return _NUTRITION_QA_NOT_FOUND_REPLY

    return "{} (per 100g): {} kcal, {}g protein, {}g carbs, {}g fat.".format(
        food.name,
        round_int(food.caloriesKcalPer100g),
        round_int(food.proteinGPer100g),
        round_int(food.carbsGPer100g),
        round_int(food.fatGPer100g),
    )


def _handle_wellbeing_flag(user_id: str, tier: str) -> _Outcome:
    """§5.6 - supportive, number-free, never mutates a draft, never blocks
    logging or quota. Marks the session so a future gamification feature can
    suppress streak/deficit-praise copy for it (§5.6: "for the session")."""
    session = repository.get_or_create_today_session(user_id)
    repository.set_session_gamification_suppressed(session.id)
    return _Outcome(
        tier=tier,
        intent="WELLBEING_FLAG",
        assistant_text=_WELLBEING_FLAG_REPLY,
        gamification_suppressed=True,
        wellbeing_resources=list(settings.WELLBEING_RESOURCES),
    )


def _handle_non_logging_intent(
    user_id: str,
    intent: str,
    normalized: str,
    tier: str,
    envelope: Optional[Dict[str, Any]] = None,
) -> _Outcome:
    """Every §5.5 intent except the 5 logging ones and `WELLBEING_FLAG`
    (Chunk 6b) - none of these mutate a draft or touch quota (§5.1.4),
    whether T-1 caught it directly (`envelope=None`) or a T2/T3 envelope was
    classified this way after T-1 missed the phrasing."""
    if intent == "DIARY_QUERY":
        text = _answer_diary_query(user_id, normalized)
    elif intent == "NUTRITION_QA":
        food_text = chatparser.extract_nutrition_qa_food(normalized)
        if not food_text and envelope is not None and envelope["items"]:
            food_text = envelope["items"][0]["food"]
        text = _answer_nutrition_qa(food_text)
    elif intent == "APP_HELP":
        text = _APP_HELP_REPLY
    elif intent == "ADVICE_SEEKING":
        text = _ADVICE_SEEKING_REPLY
    elif intent == "SOCIAL":
        text = _GREETING_REPLY
    elif intent == "UNCLEAR":
        text = _UNCLEAR_REPLY
    else:  # OTHER
        text = _OTHER_REPLY
    return _Outcome(tier=tier, intent=intent, assistant_text=text)


_NON_LOGGING_INTENTS = (
    "DIARY_QUERY",
    "APP_HELP",
    "NUTRITION_QA",
    "ADVICE_SEEKING",
    "SOCIAL",
    "UNCLEAR",
    "OTHER",
)


# -- reproducibility & cache-key versioning (Chunk 8a, §12.3, §12.7) --------


def _versioned_cache_key(normalized_hash: str) -> str:
    """Folds catalog/normalization/parser versions into the raw text hash
    (§12.7) so a catalog edit or a parser/normalization deploy can't keep
    serving a stale T0/L2 interpretation. Lives here, not in `chatparser`,
    because `chatparser` is a pure, Django-free package by convention and
    can't reach `settings`/the DB itself - this is the one layer up that
    already can. A version bump just stops matching old rows going forward;
    nothing purges them, same as `IdempotencyRecord`/`MealDraft` expiry."""
    catalog_version = meals_repository.get_catalog_version()
    raw = "{}:{}:{}:{}".format(
        normalized_hash, catalog_version, settings.NORMALIZATION_VERSION, settings.PARSER_VERSION
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _process_new_meal(user_id: str, normalized: str, normalized_hash: str) -> _Outcome:
    cached = repository.find_cached_message(user_id, normalized_hash)
    if cached is not None:
        outcome = _replay_snapshot(
            user_id, cached.parseSnapshot, tier="CACHE", assistant_text="Got it — logged the same as last time."
        )
        if outcome is not None:
            return outcome
        # Cached items no longer resolve against the catalog - fall through to T1.

    phrases, unconsumed = chatparser.parse_new_item_phrases(normalized)
    if not phrases:
        # T1's grammar found nothing at all - from here, in order: L2 global
        # cache (a shared phrasing another user already paid to resolve),
        # then the quota gate, then T2, then T3 on a low-confidence T2
        # result (§7.1's exception layer; a partial T1 match stays
        # unescalated at every one of these steps - see the Chunk 4a plan's
        # router-scope note).
        global_cache = repository.get_global_cache(normalized_hash)
        if global_cache is not None:
            outcome = _replay_snapshot(
                user_id,
                global_cache.snapshot,
                tier="CACHE",
                assistant_text="Got it — logged the same as last time.",
            )
            if outcome is not None:
                repository.bump_global_cache_hit(normalized_hash)
                return outcome
            # Cached items no longer resolve against the catalog - fall through.

        window = timedelta(hours=settings.AI_QUOTA_WINDOW_HOURS)
        _counter, consumed = repository.try_consume_quota(user_id, settings.AI_QUOTA_LIMIT, window)
        if not consumed:
            logger.info("assistant_quota_blocked user=%s", user_id)
            return _Outcome(
                tier="PARSER",
                intent="OTHER",
                assistant_text=_QUOTA_EXCEEDED_REPLY,
                unconsumed_text=unconsumed,
                quota_exceeded=True,
            )

        envelope = _call_llm(
            user_id, normalized, tier="LLM_SMALL", call_fn=call_small_model, counted_to_quota=True
        )
        tier_used = "LLM_SMALL"
        t2_wellbeing = envelope is not None and envelope["intent"] == "WELLBEING_FLAG"
        if envelope is not None and (
            t2_wellbeing or _envelope_confidence(envelope) < settings.T3_ESCALATION_CONFIDENCE_THRESHOLD
        ):
            # T3 is a second opinion on a low-confidence T2 *success*, never
            # a rescue for a T2 *failure* (§7.1) - a failure already degrades
            # gracefully without compounding cost on what might be a
            # provider-wide outage. A T2 WELLBEING_FLAG is the one exception
            # to "T3 overrides T2" below - always double-checked (§5.6), but
            # never downgraded by what T3 says or whether it even succeeds
            # (false negatives matter more than cost).
            t3_envelope = _call_llm(
                user_id, normalized, tier="LLM_LARGE", call_fn=call_large_model, counted_to_quota=False
            )
            if t3_envelope is not None:
                envelope, tier_used = t3_envelope, "LLM_LARGE"
            if t2_wellbeing:
                return _handle_wellbeing_flag(user_id, tier_used)

        if envelope is not None and envelope["intent"] == "WELLBEING_FLAG":
            # T2 wasn't confident enough to trust outright, escalated for
            # that reason alone, and T3 is the one that flagged it.
            return _handle_wellbeing_flag(user_id, tier_used)

        if envelope is not None and envelope["intent"] == "LOG_NEW" and envelope["items"]:
            outcome = _process_t2_new_meal(user_id, envelope, unconsumed, tier=tier_used)
            if outcome.intent == "LOG_NEW" and outcome.parse_snapshot is not None:
                repository.save_global_cache(normalized_hash, outcome.parse_snapshot)
            return outcome
        if envelope is not None and envelope["intent"] in _NON_LOGGING_INTENTS:
            # T-1's keyword classifier missed this phrasing, but T2/T3 caught
            # it anyway - same handler, just tagged with the tier that
            # actually produced it. No quota refund (see the Chunk 6a plan).
            return _handle_non_logging_intent(
                user_id, envelope["intent"], normalized, tier=tier_used, envelope=envelope
            )
        return _Outcome(tier="PARSER", intent="OTHER", assistant_text=_NO_FOOD_REPLY, unconsumed_text=unconsumed)

    # T1 succeeded - today's free, direct-log path. Off by default
    # (settings.WELLBEING_CHECK_ALL_MESSAGES): a broad keyword net that, on a
    # hit, goes straight to T3 (§5.6 - no T2 first, this path wouldn't
    # otherwise call a model at all) for a real determination before this
    # message's food gets logged. A keyword hit T3 doesn't confirm never
    # blocks or alters the log (§5.6: never lock the user out of logging).
    if settings.WELLBEING_CHECK_ALL_MESSAGES and chatparser.has_wellbeing_signal(normalized):
        envelope = _call_llm(
            user_id, normalized, tier="LLM_LARGE", call_fn=call_large_model, counted_to_quota=False
        )
        if envelope is not None and envelope["intent"] == "WELLBEING_FLAG":
            return _handle_wellbeing_flag(user_id, "LLM_LARGE")

    items_payload = []
    vectors = []
    for phrase in phrases:
        for item_payload, vector in _build_items_from_phrase(phrase):
            items_payload.append(item_payload)
            vectors.append(vector)

    resolved_count = sum(1 for ip in items_payload if ip["resolution"] == "RESOLVED")
    confidence = resolved_count / len(items_payload)
    slot = _infer_slot(None)
    name = _derive_meal_name(items_payload, vectors, phrases[0].food_text, slot)

    created = _create_draft_from_items(user_id, name, slot, "PARSER", confidence, items_payload, vectors)

    parse_snapshot = None
    if resolved_count == len(items_payload):
        # Only a fully-resolved LOG_NEW is worth caching (§7.4: cache keys
        # for LOG_NEW only) - a snapshot with an unresolved item would just
        # replay the same miss next time.
        parse_snapshot = {
            "name": name,
            "slot": slot,
            "items": [
                {"foodId": ip["foodId"], "quantity": ip["quantity"], "unit": ip["unit"], "state": ip["state"]}
                for ip in items_payload
            ],
        }

    return _Outcome(
        tier="PARSER",
        intent="LOG_NEW",
        assistant_text="Got it — let me break that down.",
        draft=serialize_draft(created),
        draft_id=created.id,
        unconsumed_text=unconsumed,
        parse_snapshot=parse_snapshot,
    )


def _process_message(
    user_id: str, normalized: str, normalized_hash: str, on_open_draft: Optional[str]
) -> _Outcome:
    # Every non-logging intent (§5.5) makes sense regardless of draft state
    # and never reaches the quota gate - checked first, before today's
    # open-draft/edit dispatch, the same place the old greeting-only check
    # used to run.
    t1_intent = chatparser.classify_t1_intent(normalized)
    if t1_intent is not None:
        return _handle_non_logging_intent(user_id, t1_intent, normalized, tier="PRECLASSIFIER")

    open_draft = repository.get_open_draft(user_id)
    if open_draft is not None:
        open_draft = repository.expire_draft_if_stale(open_draft)
        if open_draft.status != "OPEN":
            open_draft = None

    if open_draft is not None:
        edit = chatparser.parse_edit_command(normalized)
        if edit is not None:
            return _apply_edit(user_id, open_draft, edit)

        phrases, unconsumed = chatparser.parse_new_item_phrases(normalized)
        if phrases:
            # A new-meal-shaped message while a draft is already open (§5.1.1).
            if on_open_draft == "ADD":
                return _apply_add_phrases(user_id, open_draft, phrases, unconsumed)
            if on_open_draft == "NEW":
                repository.update_draft(open_draft.id, {"status": "DISCARDED", "version": {"increment": 1}})
                # Falls through below to the fresh-draft path.
            else:
                return _Outcome(
                    tier="PARSER",
                    intent="LOG_NEW",
                    assistant_text="You already have a meal in progress — add this to it, or start a new one?",
                    draft=serialize_draft(open_draft),
                    draft_id=open_draft.id,
                    needs_clarification={"reason": "open_draft", "candidates": ["ADD", "NEW"]},
                )
        else:
            # T1's edit grammar and T1's new-item grammar both missed - one
            # T2 call for a second opinion (Chunk 5a, §7.5). Edits are
            # quota-exempt even through T2 (§5.1.4) and draft-relative, so
            # never L2-cacheable (§7.4) - no quota gate, no cache lookup, no
            # T3 escalation (see the Chunk 5a plan for why `_envelope_confidence`
            # isn't a meaningful signal for intents that validly have an
            # empty `items[]`, e.g. SET_SLOT/REMOVE_ITEM).
            envelope = _call_llm(
                user_id, normalized, tier="LLM_SMALL", call_fn=call_small_model, counted_to_quota=False
            )
            outcome = _apply_ai_edit(user_id, open_draft, envelope) if envelope is not None else None
            if outcome is not None:
                return outcome
            return _Outcome(
                tier="PARSER",
                intent="OTHER",
                assistant_text=_NO_FOOD_REPLY,
                draft=serialize_draft(open_draft),
                draft_id=open_draft.id,
                unconsumed_text=unconsumed,
            )

    return _process_new_meal(user_id, normalized, normalized_hash)


def send_message(user_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """POST /messages (§10.1 - always 200, a conversational turn rather than
    always a resource creation). Idempotent on `clientMessageId`, via the
    same `_idempotent` helper `confirm_draft` and the structured mutation
    endpoints use - kept at its own, shorter TTL (not `REPLAY_WINDOW_HOURS`):
    descriptive/AI chat input isn't a queueable `opType` at all (§12.12:
    "offline logging is only available on paths that need no server")."""
    client_message_id = data["clientMessageId"]
    content = data["content"]
    on_open_draft = data.get("onOpenDraft")

    def _do() -> Dict[str, Any]:
        normalized = chatparser.normalize_text(content)
        normalized_hash = _versioned_cache_key(chatparser.hash_normalized(normalized))

        outcome = _process_message(user_id, normalized, normalized_hash, on_open_draft)

        session = repository.get_or_create_today_session(user_id)
        # Cache keys are LOG_NEW-only (§7.4) - an edit-shaped message's text has
        # nothing worth caching against, so both fields stay null for it.
        cacheable = outcome.intent == "LOG_NEW"
        request_id = _request_id_or_none()
        user_message = repository.create_chat_message(
            session.id,
            user_id,
            {
                "role": "USER",
                "clientMessageId": client_message_id,
                "content": content,
                "normalizedHash": normalized_hash if cacheable else None,
                "tier": outcome.tier,
                "intent": outcome.intent,
                "draftId": outcome.draft_id,
                "parseSnapshot": outcome.parse_snapshot if cacheable else None,
                "requestId": request_id,
            },
        )
        repository.create_chat_message(
            session.id,
            user_id,
            {"role": "ASSISTANT", "content": outcome.assistant_text, "requestId": request_id},
        )

        return {
            "messageId": user_message.id,
            "tier": outcome.tier,
            "intent": outcome.intent,
            "assistantText": outcome.assistant_text,
            "draft": outcome.draft,
            "unconsumedText": outcome.unconsumed_text,
            "needsClarification": outcome.needs_clarification,
            "quotaExceeded": outcome.quota_exceeded,
            "gamificationSuppressed": outcome.gamification_suppressed,
            "wellbeingResources": outcome.wellbeing_resources,
        }

    return _idempotent(
        client_message_id,
        user_id,
        {"content": content, "onOpenDraft": on_open_draft},
        200,
        _do,
        ttl_hours=IDEMPOTENCY_TTL_HOURS,
    )


# -- AI quota (Chunk 4c, §5.1.4, §12.8) --------------------------------------


def fetch_quota_status(user_id: str) -> Dict[str, Any]:
    """GET /quota (§10, §5.1.4's pill) - read-only, never consumes. `resetsAt`
    is `None` when the user has no active window (never used an AI parse, or
    their last one has already expired)."""
    counter = repository.get_quota_counter(user_id)
    limit = settings.AI_QUOTA_LIMIT
    window = timedelta(hours=settings.AI_QUOTA_WINDOW_HOURS)

    if counter is None or counter.windowStart <= _now() - window:
        return {"used": 0, "limit": limit, "remaining": limit, "resetsAt": None}

    used = counter.count
    return {
        "used": used,
        "limit": limit,
        "remaining": max(0, limit - used),
        "resetsAt": counter.windowStart + window,
    }
