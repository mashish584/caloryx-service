"""User persistence (Prisma).

Kept separate from the views so the data access is testable and so a future
move of the calc engine into its own service does not drag identity along.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from common.db import get_client
from common.exceptions import ConflictError, NotFoundError
from engine.enums import AuthProvider

from .clerk import ClerkIdentity

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def get_user(user_id: str, *, include_profile: bool = False) -> Optional[Any]:
    return get_client().user.find_unique(
        where={"id": user_id},
        include={"profile": {"include": {"plan": True}}} if include_profile else None,
    )


def create_guest_user() -> Any:
    """PRD §5.1 - an anonymous server record backing the local guest profile."""
    return get_client().user.create(
        data={
            "isGuest": True,
            "authProvider": AuthProvider.GUEST.value,
        }
    )


def upsert_clerk_user(identity: ClerkIdentity) -> Any:
    """Find-or-create the row behind a Clerk identity.

    Runs on every authenticated request, so it is written as a single upsert:
    the update branch refreshes only what Clerk may have changed.
    """
    update: Dict[str, Any] = {"lastSeenAt": _now(), "isGuest": False}
    if identity.email:
        update["email"] = identity.email
    if identity.provider:
        update["externalProvider"] = identity.provider

    return get_client().user.upsert(
        where={"clerkUserId": identity.clerk_user_id},
        data={
            "create": {
                "clerkUserId": identity.clerk_user_id,
                "authProvider": AuthProvider.CLERK.value,
                "externalProvider": identity.provider,
                "email": identity.email,
                "isGuest": False,
            },
            "update": update,
        },
    )


def claim_guest(guest_user_id: str, target_user_id: str) -> Any:
    """Move a guest's onboarding data onto a Clerk account (PRD §5.1 / §12).

    Refuses rather than merges when the destination already has a profile: two
    real profiles is a product decision, not something to resolve silently.
    """
    client = get_client()

    guest = client.user.find_unique(
        where={"id": guest_user_id}, include={"profile": True}
    )
    if guest is None:
        raise NotFoundError("Guest session not found.", code="guest_not_found")
    if not guest.isGuest:
        raise ConflictError(
            "That session is not a guest session.", code="not_a_guest_session"
        )
    if guest.claimedAt is not None:
        raise ConflictError(
            "That guest session has already been claimed.", code="guest_already_claimed"
        )

    target = client.user.find_unique(
        where={"id": target_user_id}, include={"profile": True}
    )
    if target is None:
        raise NotFoundError("Account not found.", code="user_not_found")

    if target.profile is not None and guest.profile is not None:
        raise ConflictError(
            "This account already has an onboarding profile.",
            code="claim_conflict",
            details={
                "guestProfileId": guest.profile.id,
                "existingProfileId": target.profile.id,
            },
        )

    with client.tx() as tx:
        if guest.profile is not None:
            tx.profile.update(
                where={"userId": guest_user_id},
                data={"user": {"connect": {"id": target_user_id}}},
            )
        tx.user.update(
            where={"id": guest_user_id},
            data={"claimedAt": _now()},
        )
        claimed = tx.user.update(
            where={"id": target_user_id},
            data={"claimedFrom": guest_user_id},
            include={"profile": {"include": {"plan": True}}},
        )

    logger.info("claimed guest %s into user %s", guest_user_id, target_user_id)
    return claimed
