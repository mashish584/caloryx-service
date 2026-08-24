"""Guest session tokens.

Guest mode (PRD §5.1) has no Clerk identity, so this service mints its own
bearer token for anonymous users. Symmetric HS256 signed with the Django secret:
the token asserts nothing beyond "this device owns anonymous user X", and the
row it points at holds no personal data until the user claims it.

The algorithm also disambiguates the two token families at the door - Clerk
session tokens are RS256, guest tokens are HS256.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from django.conf import settings

GUEST_TOKEN_TYPE = "guest"
GUEST_ALGORITHM = "HS256"


class GuestTokenError(Exception):
    pass


def issue_guest_token(user_id: str, ttl_days: Optional[int] = None) -> dict:
    now = datetime.now(timezone.utc)
    ttl = timedelta(days=ttl_days or settings.GUEST_TOKEN_TTL_DAYS)
    expires_at = now + ttl
    payload = {
        "sub": user_id,
        "typ": GUEST_TOKEN_TYPE,
        "iss": settings.GUEST_TOKEN_ISSUER,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(payload, settings.SECRET_KEY, algorithm=GUEST_ALGORITHM)
    return {"token": token, "expiresAt": expires_at.isoformat()}


def verify_guest_token(token: str) -> str:
    """Return the guest user id, or raise GuestTokenError."""
    try:
        claims = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[GUEST_ALGORITHM],
            issuer=settings.GUEST_TOKEN_ISSUER,
            options={"require": ["exp", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise GuestTokenError("Guest session is invalid or expired.") from exc

    if claims.get("typ") != GUEST_TOKEN_TYPE:
        raise GuestTokenError("Not a guest session token.")
    subject = claims.get("sub")
    if not subject:
        raise GuestTokenError("Guest session token is missing a subject.")
    return subject


def token_algorithm(token: str) -> Optional[str]:
    """Read the `alg` header without verifying anything, to route the token to
    the right verifier."""
    try:
        return jwt.get_unverified_header(token).get("alg")
    except jwt.PyJWTError:
        return None
