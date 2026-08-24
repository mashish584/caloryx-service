"""Guest session tokens - PRD §5.1."""
from __future__ import annotations

import time

import jwt
import pytest
from django.conf import settings

from authx.tokens import (
    GUEST_ALGORITHM,
    GuestTokenError,
    issue_guest_token,
    token_algorithm,
    verify_guest_token,
)


def test_guest_token_round_trip():
    issued = issue_guest_token("user_abc")
    assert verify_guest_token(issued["token"]) == "user_abc"
    assert issued["expiresAt"]


def test_guest_tokens_are_identifiable_by_algorithm():
    """Algorithm routes the token to the right verifier: guest tokens are HS256,
    Clerk session tokens are RS256."""
    issued = issue_guest_token("user_abc")
    assert token_algorithm(issued["token"]) == GUEST_ALGORITHM


def test_token_algorithm_is_none_for_garbage():
    assert token_algorithm("not-a-jwt") is None


def test_a_tampered_token_is_rejected():
    token = issue_guest_token("user_abc")["token"]
    forged = jwt.encode(
        {"sub": "user_someone_else", "typ": "guest", "iss": settings.GUEST_TOKEN_ISSUER,
         "exp": int(time.time()) + 600},
        "x" * 64,  # a valid-length key that simply is not ours
        algorithm=GUEST_ALGORITHM,
    )
    assert forged != token
    with pytest.raises(GuestTokenError):
        verify_guest_token(forged)


def test_an_expired_token_is_rejected():
    expired = jwt.encode(
        {"sub": "user_abc", "typ": "guest", "iss": settings.GUEST_TOKEN_ISSUER,
         "exp": int(time.time()) - 60},
        settings.SECRET_KEY,
        algorithm=GUEST_ALGORITHM,
    )
    with pytest.raises(GuestTokenError):
        verify_guest_token(expired)


def test_a_non_guest_token_type_is_rejected():
    other = jwt.encode(
        {"sub": "user_abc", "typ": "admin", "iss": settings.GUEST_TOKEN_ISSUER,
         "exp": int(time.time()) + 600},
        settings.SECRET_KEY,
        algorithm=GUEST_ALGORITHM,
    )
    with pytest.raises(GuestTokenError):
        verify_guest_token(other)


def test_a_token_from_another_issuer_is_rejected():
    foreign = jwt.encode(
        {"sub": "user_abc", "typ": "guest", "iss": "somebody-else",
         "exp": int(time.time()) + 600},
        settings.SECRET_KEY,
        algorithm=GUEST_ALGORITHM,
    )
    with pytest.raises(GuestTokenError):
        verify_guest_token(foreign)
