"""Session endpoints.

PRD §8 lists `/auth/google` and `/auth/apple`; with Clerk on the device those
exchanges happen client-side and this service only verifies the resulting
session JWT, so neither endpoint exists here. `/auth/session` is the equivalent
"who am I, and where should the app resume" call, and `/auth/guest` is unchanged.
"""
from __future__ import annotations

import logging

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from common.exceptions import DomainError
from onboarding import repository as onboarding_repository

from . import repository
from .permissions import IsRegisteredActor
from .serializers import (
    ClaimGuestRequestSerializer,
    serialize_actor,
    serialize_onboarding_state,
    serialize_user,
)
from .tokens import GuestTokenError, issue_guest_token, verify_guest_token

logger = logging.getLogger(__name__)


class GuestSessionView(APIView):
    """POST /api/v1/auth/guest - create an anonymous session (PRD §5.1).

    Guest mode must be fully functional: the user completes onboarding and uses
    the app on this record, and can claim it into a Clerk account later.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_scope_guest_create = True

    def post(self, request):
        user = repository.create_guest_user()
        token = issue_guest_token(user.id)
        logger.info("created guest session for user %s", user.id)
        return Response(
            {
                "token": token["token"],
                "expiresAt": token["expiresAt"],
                "user": serialize_user(user),
                "onboarding": serialize_onboarding_state(None),
            },
            status=status.HTTP_201_CREATED,
        )


class SessionView(APIView):
    """GET/POST /api/v1/auth/session - resolve the current session.

    POST is the sign-in hand-off: the first authenticated call creates or
    refreshes the Clerk-backed user row. GET is the cheap resume check.
    """

    def get(self, request):
        return self._session_response(request)

    def post(self, request):
        return self._session_response(request)

    def _session_response(self, request):
        actor = request.user
        profile = onboarding_repository.get_profile(actor.user_id)
        return Response(
            {
                "user": serialize_actor(actor),
                "onboarding": serialize_onboarding_state(profile),
            }
        )


class ClaimGuestView(APIView):
    """POST /api/v1/auth/claim - move guest onboarding data onto this account.

    The claiming UI is deferred (PRD §12) but the endpoint and data model are in
    place, so a guest who signs in later keeps the plan they already generated.
    """

    permission_classes = [IsRegisteredActor]

    def post(self, request):
        serializer = ClaimGuestRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            guest_user_id = verify_guest_token(serializer.validated_data["guestToken"])
        except GuestTokenError as exc:
            raise DomainError(str(exc), code="invalid_guest_token") from exc

        user = repository.claim_guest(guest_user_id, request.user.user_id)
        return Response(
            {
                "user": serialize_user(user),
                "onboarding": serialize_onboarding_state(getattr(user, "profile", None)),
            }
        )
