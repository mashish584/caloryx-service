from __future__ import annotations

import logging

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from common.exceptions import NotFoundError
from common.schema import (
    SERVER_ERROR,
    UNAUTHORIZED,
    VALIDATION_ERROR,
    error_response,
)

from . import repository, services
from .serializers import (
    AdvisoryCheckResponseSerializer,
    AdvisoryCheckSerializer,
    CompleteResponseSerializer,
    EngineConfigResponseSerializer,
    PlanResponseSerializer,
    ProfileStateResponseSerializer,
    ProfileUpsertResponseSerializer,
    ProfileUpsertSerializer,
    serialize_profile,
    validation_bounds,
)

logger = logging.getLogger(__name__)


class ProfileView(APIView):
    """POST /api/v1/onboarding/profile - upsert the collected inputs (steps 1-3)."""

    @extend_schema(
        request=ProfileUpsertSerializer,
        responses={
            200: ProfileUpsertResponseSerializer,
            400: VALIDATION_ERROR,
            401: UNAUTHORIZED,
            422: error_response(
                "Under the minimum age (§9). Render the dedicated screen for this "
                "one, not a field error - it blocks account creation.",
                "age_below_minimum",
            ),
            500: SERVER_ERROR,
        },
    )
    def post(self, request):
        serializer = ProfileUpsertSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = services.save_profile(request.user.user_id, serializer.validated_data)
        return Response(payload, status=status.HTTP_200_OK)

    @extend_schema(
        responses={
            200: ProfileStateResponseSerializer,
            401: UNAUTHORIZED,
            500: SERVER_ERROR,
        }
    )
    def get(self, request):
        profile = repository.get_profile(request.user.user_id)
        if profile is None:
            return Response({"profile": None, "advisories": []})

        return Response(
            {
                "profile": serialize_profile(profile),
                "advisories": [a.to_dict() for a in services.profile_advisories(profile)],
            }
        )


class AdvisoryCheckView(APIView):
    """POST /api/v1/onboarding/advisories - hints for values, without storing them.

    A read-only sibling of the profile upsert. The client sends the measurements
    it has collected so far, gets the §9 advisories back, and nothing is
    persisted: there is no `profile` in the response because there is no row.
    Before this existed, the only way to see an advisory was to write one.

    Authenticated like the rest of the flow. It touches no user data, but an
    unauthenticated compute endpoint is a throttling problem for no gain - the
    client already holds a guest token by this point in onboarding (§5.1).
    """

    @extend_schema(
        request=AdvisoryCheckSerializer,
        responses={
            200: AdvisoryCheckResponseSerializer,
            400: VALIDATION_ERROR,
            401: UNAUTHORIZED,
            500: SERVER_ERROR,
        },
    )
    def post(self, request):
        serializer = AdvisoryCheckSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(services.check_advisories(serializer.validated_data))


class PlanView(APIView):
    """POST computes and persists the authoritative plan; GET returns the stored
    one for the plan screen and for resuming a half-finished flow (PRD §4)."""

    @extend_schema(
        request=None,
        responses={
            200: PlanResponseSerializer,
            401: UNAUTHORIZED,
            409: error_response(
                "No profile yet - send the user back to the profile step.",
                "profile_required",
            ),
            500: SERVER_ERROR,
        },
    )
    def post(self, request):
        return Response(services.generate_plan(request.user.user_id))

    @extend_schema(
        responses={
            200: PlanResponseSerializer,
            401: UNAUTHORIZED,
            404: error_response(
                "No plan has been generated yet.", "plan_not_found"
            ),
            409: error_response(
                "No profile yet - send the user back to the profile step.",
                "profile_required",
            ),
            500: SERVER_ERROR,
        }
    )
    def get(self, request):
        plan = services.fetch_plan(request.user.user_id)
        if plan is None:
            # Raised rather than assembled inline: `api_exception_handler` is
            # what attaches `requestId`, and this used to be the one error in
            # the service without one.
            raise NotFoundError(
                "No plan has been generated yet.", code="plan_not_found"
            )
        return Response(plan)


class CompleteView(APIView):
    """POST /api/v1/onboarding/complete - stamp `onboardedAt` and finalise."""

    @extend_schema(
        request=None,
        responses={
            200: CompleteResponseSerializer,
            401: UNAUTHORIZED,
            409: error_response(
                "A prior step is missing. Branch on `code`, not the status: both "
                "outcomes are 409 and they route to different screens.",
                "profile_required",
                "plan_required",
            ),
            500: SERVER_ERROR,
        },
    )
    def post(self, request):
        return Response(services.complete_onboarding(request.user.user_id))


class EngineConfigView(APIView):
    """GET /api/v1/onboarding/config - the constants behind the client preview.

    PRD §10 requires multipliers, adjustments, macro ratios and floors to live in
    server config rather than client constants. Serving them here lets the
    optimistic preview (§5.5) match the authoritative result instead of drifting
    from it after a retune.
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(
        responses={200: EngineConfigResponseSerializer, 500: SERVER_ERROR}
    )
    def get(self, request):
        config = repository.get_active_engine_config()
        return Response(config.to_public_dict(validation_bounds()))
