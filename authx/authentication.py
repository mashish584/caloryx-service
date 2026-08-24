"""Bearer authentication for both session families.

`Authorization: Bearer <token>` carries either a Clerk session JWT (RS256) or a
guest token this service minted (HS256). The signing algorithm routes it, so the
client never has to say which kind it holds and every endpoint transparently
accepts guest, Google, and Apple sessions (PRD §8).
"""
from __future__ import annotations

import logging

from rest_framework import authentication, exceptions

from engine.enums import AuthProvider

from . import repository
from .actor import Actor
from .clerk import ClerkConfigurationError, ClerkTokenError, verify_token
from .tokens import GUEST_ALGORITHM, GuestTokenError, token_algorithm, verify_guest_token

logger = logging.getLogger(__name__)

AUTH_SCHEME = "bearer"


class BearerAuthentication(authentication.BaseAuthentication):
    def authenticate(self, request):
        header = authentication.get_authorization_header(request).decode("latin-1")
        if not header:
            return None  # anonymous; permission classes decide what that means

        parts = header.split()
        if len(parts) != 2 or parts[0].lower() != AUTH_SCHEME:
            raise exceptions.AuthenticationFailed(
                "Authorization header must be 'Bearer <token>'."
            )

        token = parts[1]
        if token_algorithm(token) == GUEST_ALGORITHM:
            return self._authenticate_guest(token), token
        return self._authenticate_clerk(token), token

    def authenticate_header(self, request):
        # Makes DRF answer 401 rather than 403 for missing credentials.
        return 'Bearer realm="caloryx"'

    # -- internals ---------------------------------------------------------

    def _authenticate_guest(self, token: str) -> Actor:
        try:
            user_id = verify_guest_token(token)
        except GuestTokenError as exc:
            raise exceptions.AuthenticationFailed(str(exc)) from exc

        user = repository.get_user(user_id)
        if user is None:
            raise exceptions.AuthenticationFailed("Guest session no longer exists.")
        if user.claimedAt is not None:
            # The guest data now lives on a Clerk account; the app must sign in.
            raise exceptions.AuthenticationFailed(
                "This guest session has been claimed by an account. Sign in to continue."
            )

        return Actor(
            user_id=user.id,
            provider=AuthProvider.GUEST,
            is_guest=True,
        )

    def _authenticate_clerk(self, token: str) -> Actor:
        try:
            identity = verify_token(token)
        except ClerkConfigurationError as exc:
            logger.error("clerk misconfigured: %s", exc)
            raise exceptions.AuthenticationFailed(
                "Authentication is temporarily unavailable."
            ) from exc
        except ClerkTokenError as exc:
            raise exceptions.AuthenticationFailed(str(exc)) from exc

        user = repository.upsert_clerk_user(identity)
        return Actor(
            user_id=user.id,
            provider=AuthProvider.CLERK,
            is_guest=False,
            clerk_user_id=user.clerkUserId,
            email=user.email,
        )
