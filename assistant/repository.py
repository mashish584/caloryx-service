"""Chat session, draft, and idempotency persistence (Prisma).

The only module in this app that touches `get_client()` - services.py owns
the business logic (state-machine rules, optimistic-lock checks, idempotency
comparison), this owns reads and writes. Mirrors meals/repository.py.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

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
