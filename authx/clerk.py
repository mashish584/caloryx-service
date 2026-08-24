"""Clerk session-token verification.

The app signs in through Clerk on the device (Google, Apple, and anything Clerk
adds later) and sends the resulting session JWT as a bearer token. This service
verifies it against Clerk's published JWKS, so no provider secret ever lives
here - which is why the PRD's `/auth/google` and `/auth/apple` token-exchange
endpoints (§8) are not implemented: Clerk performs that exchange on the client.

Apple's private-relay address arrives as an ordinary email claim and is stored
unchanged; the stable Apple identifier lives inside Clerk and reaches us as the
Clerk user id, which is what we key on.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional

import jwt
from django.conf import settings
from jwt import PyJWKClient

logger = logging.getLogger(__name__)

_jwks_client: Optional[PyJWKClient] = None
_jwks_lock = threading.Lock()


class ClerkConfigurationError(RuntimeError):
    """Clerk settings are missing - a deployment problem, not a caller problem."""


class ClerkTokenError(Exception):
    """The presented token is missing, malformed, expired, or not ours."""


@dataclass(frozen=True)
class ClerkIdentity:
    clerk_user_id: str
    email: Optional[str] = None
    provider: Optional[str] = None  # "oauth_google", "oauth_apple", ...
    session_id: Optional[str] = None
    raw_claims: Optional[Dict[str, Any]] = None


def _client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is not None:
        return _jwks_client
    with _jwks_lock:
        if _jwks_client is None:
            if not settings.CLERK_JWKS_URL:
                raise ClerkConfigurationError(
                    "CLERK_JWKS_URL (or CLERK_ISSUER) must be set to verify Clerk tokens."
                )
            _jwks_client = PyJWKClient(
                settings.CLERK_JWKS_URL,
                cache_keys=True,
                lifespan=settings.CLERK_JWKS_CACHE_SECONDS,
            )
    return _jwks_client


def _extract_email(claims: Dict[str, Any]) -> Optional[str]:
    """Clerk's default session token is lean; email only appears when the JWT
    template adds it. Accept the common spellings and shrug if absent."""
    for key in ("email", "email_address", "primary_email_address"):
        value = claims.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _extract_provider(claims: Dict[str, Any]) -> Optional[str]:
    for key in ("provider", "oauth_provider", "external_provider"):
        value = claims.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def verify_token(token: str) -> ClerkIdentity:
    """Verify signature, expiry, and issuer. Raises ClerkTokenError on any doubt."""
    try:
        signing_key = _client().get_signing_key_from_jwt(token)
    except ClerkConfigurationError:
        raise
    except Exception as exc:  # noqa: BLE001 - JWKS fetch/parse failures
        logger.warning("clerk jwks lookup failed: %s", exc)
        raise ClerkTokenError("Could not verify the session token.") from exc

    options = {"require": ["exp", "sub"], "verify_aud": bool(settings.CLERK_AUDIENCE)}
    try:
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=settings.CLERK_ISSUER or None,
            audience=settings.CLERK_AUDIENCE,
            leeway=settings.CLERK_LEEWAY_SECONDS,
            options=options,
        )
    except jwt.PyJWTError as exc:
        raise ClerkTokenError("Session token is invalid or expired.") from exc

    subject = claims.get("sub")
    if not subject:
        raise ClerkTokenError("Session token is missing a subject.")

    return ClerkIdentity(
        clerk_user_id=subject,
        email=_extract_email(claims),
        provider=_extract_provider(claims),
        session_id=claims.get("sid"),
        raw_claims=claims,
    )


def reset_cache() -> None:
    """Test hook - drops the memoised JWKS client."""
    global _jwks_client
    with _jwks_lock:
        _jwks_client = None
