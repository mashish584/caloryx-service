"""Chat session, draft, and idempotency persistence (Prisma).

The only module in this app that touches `get_client()` - services.py owns
the business logic (state-machine rules, optimistic-lock checks, idempotency
comparison), this owns reads and writes. Mirrors meals/repository.py.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from prisma import Json

from common.db import get_client

_ITEM_WITH_FOOD = {"food": {"include": {"servingUnits": True}}}
_DRAFT_WITH_ITEMS = {"items": {"include": _ITEM_WITH_FOOD}}


def _now() -> datetime:
    return datetime.now(timezone.utc)


# -- sessions -----------------------------------------------------------------


def get_or_create_today_session(user_id: str) -> Any:
    """One `ChatSession` per UTC calendar day per user (§18 open question -
    session semantics aren't settled product policy; this is the simplest
    resume-shaped default). Not client-visible in Chunk 2a - purely the FK
    a draft belongs to."""
    today_start = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    session = get_client().chatsession.find_first(
        where={"userId": user_id, "startedAt": {"gte": today_start}},
        order={"startedAt": "desc"},
    )
    if session is not None:
        return session
    return get_client().chatsession.create(data={"user": {"connect": {"id": user_id}}})


# -- drafts ---------------------------------------------------------------


def get_open_draft(user_id: str) -> Optional[Any]:
    return get_client().mealdraft.find_first(
        where={"userId": user_id, "status": "OPEN"}, include=_DRAFT_WITH_ITEMS
    )


def create_draft_with_expiry_check(
    user_id: str, session_id: str, draft_data: Dict[str, Any], items_data: List[Dict[str, Any]]
) -> Optional[Any]:
    """Transactional lazy expiry + check-then-insert (§12.2), applied without
    the DB-level partial unique index (see the schema comment on
    `MealDraft.expiresAt` for why). Returns the created draft, or `None` if
    the user already has a genuinely open draft - the caller re-reads it via
    `get_open_draft` for the error payload, since that read doesn't need to
    be part of this transaction.

    Both branches commit normally (no exception): even the "found existing"
    branch is safe to commit, because the expiry step only ever touches
    drafts that were actually stale.
    """
    now = _now()
    with get_client().tx() as tx:
        tx.mealdraft.update_many(
            where={"userId": user_id, "status": "OPEN", "expiresAt": {"lt": now}},
            data={"status": "EXPIRED"},
        )
        if tx.mealdraft.find_first(where={"userId": user_id, "status": "OPEN"}) is not None:
            return None

        payload = dict(
            draft_data,
            user={"connect": {"id": user_id}},
            session={"connect": {"id": session_id}},
        )
        payload["items"] = {"create": items_data}
        return tx.mealdraft.create(data=payload, include=_DRAFT_WITH_ITEMS)


def get_draft(user_id: str, draft_id: str) -> Optional[Any]:
    draft = get_client().mealdraft.find_unique(
        where={"id": draft_id}, include=_DRAFT_WITH_ITEMS
    )
    if draft is None or draft.userId != user_id:
        # Same 404 either way - see meals/repository.py's get_logged_meal.
        return None
    return draft


def expire_draft_if_stale(draft: Any) -> Any:
    """Every read path treats an OPEN draft past `expiresAt` as expired
    (§12.2) - flips it in place so a client never sees a stale draft as
    resumable, and so the fix is durable rather than re-derived on every call."""
    if draft.status == "OPEN" and draft.expiresAt < _now():
        return get_client().mealdraft.update(
            where={"id": draft.id}, data={"status": "EXPIRED"}, include=_DRAFT_WITH_ITEMS
        )
    return draft


def update_draft(draft_id: str, data: Dict[str, Any]) -> Any:
    """Generic draft update - name/slot changes, totals recomputation, and
    status transitions (CONFIRMED/DISCARDED) all go through this. Callers pass
    `{"version": {"increment": 1}}` for the optimistic-lock bump so it's
    atomic with whatever else the write is doing."""
    return get_client().mealdraft.update(
        where={"id": draft_id}, data=data, include=_DRAFT_WITH_ITEMS
    )


# -- draft items ------------------------------------------------------------


def create_draft_item(draft_id: str, item_data: Dict[str, Any]) -> Any:
    payload = dict(item_data, draft={"connect": {"id": draft_id}})
    return get_client().mealdraftitem.create(data=payload, include=_ITEM_WITH_FOOD)


def get_draft_item(user_id: str, draft_id: str, item_id: str) -> Optional[Any]:
    draft = get_draft(user_id, draft_id)
    if draft is None:
        return None
    return next((item for item in draft.items if item.id == item_id), None)


def update_draft_item(item_id: str, data: Dict[str, Any]) -> Any:
    return get_client().mealdraftitem.update(
        where={"id": item_id}, data=data, include=_ITEM_WITH_FOOD
    )


def delete_draft_item(item_id: str) -> None:
    get_client().mealdraftitem.delete(where={"id": item_id})


# -- idempotency (§9, §12.1) -------------------------------------------------


def get_idempotency_record(key: str) -> Optional[Any]:
    return get_client().idempotencyrecord.find_unique(where={"key": key})


def save_idempotency_record(
    key: str,
    user_id: str,
    request_hash: str,
    response_body: Dict[str, Any],
    status_code: int,
    expires_at: datetime,
) -> Any:
    return get_client().idempotencyrecord.create(
        data={
            "key": key,
            "userId": user_id,
            "requestHash": request_hash,
            "responseBody": Json(response_body),
            "statusCode": status_code,
            "expiresAt": expires_at,
        }
    )


# -- chat messages (Chunk 2b, §9) --------------------------------------------


def create_chat_message(session_id: str, user_id: str, data: Dict[str, Any]) -> Any:
    payload = dict(
        data,
        session={"connect": {"id": session_id}},
        user={"connect": {"id": user_id}},
    )
    if "parseSnapshot" in payload and payload["parseSnapshot"] is not None:
        payload["parseSnapshot"] = Json(payload["parseSnapshot"])
    return get_client().chatmessage.create(data=payload)


def find_cached_message(user_id: str, normalized_hash: str) -> Optional[Any]:
    """T0 (§7.4): the most recent successful `LOG_NEW` parse by this user for
    this exact normalized text, if any. `parseSnapshot` is checked in Python
    rather than in the query - simpler than a JSON-null filter, and this
    table is small enough per-user that it doesn't matter."""
    message = get_client().chatmessage.find_first(
        where={"userId": user_id, "normalizedHash": normalized_hash, "role": "USER"},
        order={"createdAt": "desc"},
    )
    if message is not None and message.parseSnapshot is not None:
        return message
    return None


# -- parse telemetry (Chunk 4a, §9, I9) --------------------------------------


def create_parse_event(user_id: str, data: Dict[str, Any]) -> Any:
    return get_client().parseevent.create(data=dict(data, userId=user_id))


# -- quantity-resolution ladder (Chunk 4b, §5.1.1a) --------------------------

_RECENT_OBSERVATIONS_CAP = 10


def get_serving_preference(user_id: str, food_id: str, state: str) -> Optional[Any]:
    return get_client().userservingpreference.find_unique(
        where={"userId_foodId_state": {"userId": user_id, "foodId": food_id, "state": state}}
    )


def record_serving_observation(user_id: str, food_id: str, state: str, grams: float) -> Any:
    """Every EXPLICIT resolved item's write-back into the user's serving
    history (§5.1.1a: "every portion edit writes to a per-user serving
    profile"). `recentGrams` is capped at the last 10 observations so
    `medianGrams` is a literal median-of-recent-logs, computed here rather
    than via a raw SQL aggregate - Postgres has no portable median, and the
    array is small enough that Python is simpler."""
    existing = get_serving_preference(user_id, food_id, state)
    if existing is None:
        return get_client().userservingpreference.create(
            data={
                "userId": user_id,
                "foodId": food_id,
                "state": state,
                "recentGrams": [grams],
                "medianGrams": grams,
                "observations": 1,
            }
        )

    recent = (list(existing.recentGrams) + [grams])[-_RECENT_OBSERVATIONS_CAP:]
    return get_client().userservingpreference.update(
        where={"id": existing.id},
        data={
            "recentGrams": recent,
            "medianGrams": statistics.median(recent),
            "observations": len(recent),
        },
    )


# -- AI quota (Chunk 4c, §5.1.4, §12.8) --------------------------------------


def get_quota_counter(user_id: str) -> Optional[Any]:
    return get_client().aiquotacounter.find_unique(where={"userId": user_id})


def try_consume_quota(user_id: str, limit: int, window: timedelta) -> Tuple[Any, bool]:
    """Atomic check-and-consume (§12.8) with no Redis and no raw SQL: a
    Postgres `UPDATE ... WHERE` locks the row it's about to touch even under
    READ COMMITTED, so two concurrent conditional `update_many` calls
    serialize on the same row rather than both reading a stale count. The
    two conditions below are mutually exclusive and jointly exhaustive for a
    given row (its `windowStart` is either still within `window` or not), so
    at most one of them can ever match - `create` only fires for a user's
    very first call ever, when no row exists for either to match at all.

    Returns `(counter, consumed)` - `consumed` is whether *this* call's
    write happened. `False` means an active window already at `limit` -
    genuinely exhausted, nothing left to write."""
    client = get_client()
    now = _now()
    floor = now - window

    bumped = client.aiquotacounter.update_many(
        where={"userId": user_id, "windowStart": {"gt": floor}, "count": {"lt": limit}},
        data={"count": {"increment": 1}},
    )
    consumed = bumped.count == 1

    if not consumed:
        reset = client.aiquotacounter.update_many(
            where={"userId": user_id, "windowStart": {"lte": floor}},
            data={"windowStart": now, "count": 1},
        )
        consumed = reset.count == 1

    counter = client.aiquotacounter.find_unique(where={"userId": user_id})
    if counter is None:
        counter = client.aiquotacounter.create(
            data={"userId": user_id, "windowStart": now, "count": 1}
        )
        consumed = True

    return counter, consumed


# -- L2 global parse cache (Chunk 4c, §7.4) ----------------------------------


def get_global_cache(normalized_hash: str) -> Optional[Any]:
    return get_client().globalparsecache.find_unique(where={"normalizedHash": normalized_hash})


def save_global_cache(normalized_hash: str, snapshot: Dict[str, Any]) -> Any:
    """Upsert - a repeat write (a different user's identical phrasing
    resolving slightly differently, e.g. after a catalog edit) just refreshes
    the snapshot rather than erroring on the unique constraint."""
    return get_client().globalparsecache.upsert(
        where={"normalizedHash": normalized_hash},
        data={
            "create": {"normalizedHash": normalized_hash, "snapshot": Json(snapshot)},
            "update": {"snapshot": Json(snapshot)},
        },
    )


def bump_global_cache_hit(normalized_hash: str) -> None:
    get_client().globalparsecache.update(
        where={"normalizedHash": normalized_hash}, data={"hitCount": {"increment": 1}}
    )
