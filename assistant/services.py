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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import chatparser
from common.exceptions import (
    DraftNotOpenError,
    DraftVersionConflictError,
    IdempotencyKeyReuseError,
    NotFoundError,
    OpenDraftExistsError,
    UnresolvableQuantityError,
)
from meals import repository as meals_repository
from meals import services as meals_services
from meals.serializers import serialize_logged_meal
from nutrition import ZERO_VECTOR, NutrientVector, sum_nutrition

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

_GREETING_REPLY = "Hey \U0001F44B What did you eat?"
_NO_FOOD_REPLY = "I couldn't work out what food that was — try naming the dish, or search for it."


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


def _draft_totals(draft: Any) -> NutrientVector:
    return sum_nutrition(item_nutrient_vector(item) for item in draft.items if item.resolution == "RESOLVED")


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
    return created


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
    created = _create_draft_from_items(user_id, data["name"], slot, "MANUAL", 1.0, items_payload, vectors)
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


def _add_item_payload(user_id: str, draft_id: str, item_payload: Dict[str, Any]) -> Any:
    """Shared core: persist an already-built `MealDraftItem` payload
    (RESOLVED or UNRESOLVED) and recompute totals. The caller is responsible
    for having already validated the draft is OPEN and at the expected
    version - `add_draft_item` (structured input) and Chunk 2b's text-driven
    ADD_ITEM both do that themselves before reaching here, since they resolve
    the item differently (exact `foodId` vs. a fuzzy-matched food name)."""
    repository.create_draft_item(draft_id, item_payload)
    return _recompute_totals(user_id, draft_id, bump_version=True)


def add_draft_item(user_id: str, draft_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
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
    # numbers. Only RESOLVED items convert into LoggedMealItem rows.
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


def _build_item_from_phrase(
    phrase: "chatparser.ParsedItemPhrase",
) -> Tuple[Dict[str, Any], NutrientVector]:
    """One `ParsedItemPhrase` -> a `MealDraftItem` payload + its nutrient
    vector. HIGH/MEDIUM band -> RESOLVED (reusing `meals.services.
    resolve_item` for the actual gram/yield math); LOW/no match, or a matched
    food whose serving table doesn't have this unit -> UNRESOLVED. Never
    raises: a parse-quality problem degrades to an unresolved item, not an
    error (§12.13's "never silently drop, never hard-fail" spirit)."""
    food, score, band = _resolve_food_by_name(phrase.food_text)
    if food is None or band == "LOW":
        return _unresolved_item(phrase), ZERO_VECTOR

    try:
        resolved, vector = meals_services.resolve_item(food, phrase.quantity, phrase.unit, phrase.state)
    except UnresolvableQuantityError:
        return _unresolved_item(phrase), ZERO_VECTOR

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
        "quantitySource": "EXPLICIT",
        "massSource": _mass_source(resolved["unit"]),
        "matchScore": score,
        "matchBand": band,
    }
    return payload, vector


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


def _template_name(sample_food_text: Optional[str], slot: str) -> str:
    """Minimal placeholder naming - NOT the full §5.1.3 template (dominant
    protein/grain + form factor for multi-item bowls, plus the model
    `mealName` free-ride), which is Chunk 4's job. Just enough for a draft
    created from text to have a name at all."""
    if sample_food_text:
        return "{} — {}".format(slot.title(), sample_food_text.title())
    return "{} meal".format(slot.title())


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
        item_payload, _ = _build_item_from_phrase(edit.item)
        updated_row = _add_item_payload(user_id, draft.id, item_payload)
        assistant_text = (
            "Added it to your meal."
            if item_payload["resolution"] == "RESOLVED"
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


def _apply_add_phrases(
    user_id: str, draft: Any, phrases: List["chatparser.ParsedItemPhrase"], unconsumed: List[str]
) -> _Outcome:
    _load_open_draft_for_mutation(user_id, draft.id, draft.version)
    updated_row = None
    for phrase in phrases:
        item_payload, _ = _build_item_from_phrase(phrase)
        updated_row = _add_item_payload(user_id, draft.id, item_payload)
    return _Outcome(
        tier="PARSER",
        intent="ADD_ITEM",
        assistant_text="Added it to your meal.",
        draft=serialize_draft(updated_row),
        draft_id=draft.id,
        unconsumed_text=unconsumed,
    )


def _process_new_meal(user_id: str, normalized: str, normalized_hash: str) -> _Outcome:
    cached = repository.find_cached_message(user_id, normalized_hash)
    if cached is not None:
        snapshot = cached.parseSnapshot
        items_payload: List[Dict[str, Any]] = []
        vectors: List[NutrientVector] = []
        for raw in snapshot["items"]:
            food = meals_repository.get_food(raw["foodId"])
            if food is None:
                continue  # catalog changed since the cache was written
            item_payload, vector = _resolve_draft_item(food, raw["quantity"], raw["unit"], raw.get("state"))
            items_payload.append(item_payload)
            vectors.append(vector)
        if items_payload:
            created = _create_draft_from_items(
                user_id, snapshot["name"], snapshot["slot"], "CACHE", 1.0, items_payload, vectors
            )
            return _Outcome(
                tier="CACHE",
                intent="LOG_NEW",
                assistant_text="Got it — logged the same as last time.",
                draft=serialize_draft(created),
                draft_id=created.id,
            )
        # Cached items no longer resolve against the catalog - fall through to T1.

    phrases, unconsumed = chatparser.parse_new_item_phrases(normalized)
    if not phrases:
        return _Outcome(tier="PARSER", intent="OTHER", assistant_text=_NO_FOOD_REPLY, unconsumed_text=unconsumed)

    items_payload = []
    vectors = []
    for phrase in phrases:
        item_payload, vector = _build_item_from_phrase(phrase)
        items_payload.append(item_payload)
        vectors.append(vector)

    resolved_count = sum(1 for ip in items_payload if ip["resolution"] == "RESOLVED")
    confidence = resolved_count / len(items_payload)
    slot = _infer_slot(None)
    name = _template_name(phrases[0].food_text, slot)

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
    if chatparser.is_non_food_greeting(normalized):
        return _Outcome(tier="PRECLASSIFIER", intent="OTHER", assistant_text=_GREETING_REPLY)

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
    always a resource creation). Idempotent on `clientMessageId`, same
    pattern as `confirm_draft` (§12.1) - reused, not reimplemented."""
    client_message_id = data["clientMessageId"]
    content = data["content"]
    on_open_draft = data.get("onOpenDraft")

    request_hash = _request_hash({"content": content, "onOpenDraft": on_open_draft})
    record = repository.get_idempotency_record(client_message_id)
    if record is not None and record.expiresAt > _now():
        if record.requestHash != request_hash:
            raise IdempotencyKeyReuseError()
        return dict(record.responseBody)

    normalized = chatparser.normalize_text(content)
    normalized_hash = chatparser.hash_normalized(normalized)

    outcome = _process_message(user_id, normalized, normalized_hash, on_open_draft)

    session = repository.get_or_create_today_session(user_id)
    # Cache keys are LOG_NEW-only (§7.4) - an edit-shaped message's text has
    # nothing worth caching against, so both fields stay null for it.
    cacheable = outcome.intent == "LOG_NEW"
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
        },
    )
    repository.create_chat_message(
        session.id, user_id, {"role": "ASSISTANT", "content": outcome.assistant_text}
    )

    response = {
        "messageId": user_message.id,
        "tier": outcome.tier,
        "intent": outcome.intent,
        "assistantText": outcome.assistant_text,
        "draft": outcome.draft,
        "unconsumedText": outcome.unconsumed_text,
        "needsClarification": outcome.needs_clarification,
    }
    repository.save_idempotency_record(
        client_message_id,
        user_id,
        request_hash,
        response,
        200,
        _now() + timedelta(hours=IDEMPOTENCY_TTL_HOURS),
    )
    return response
