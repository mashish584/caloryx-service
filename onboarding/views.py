from __future__ import annotations

import logging

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from . import repository, services
from .serializers import ProfileUpsertSerializer, serialize_profile

logger = logging.getLogger(__name__)


class ProfileView(APIView):
    """POST /api/v1/onboarding/profile - upsert the collected inputs (steps 1-3)."""

    def post(self, request):
        serializer = ProfileUpsertSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = services.save_profile(request.user.user_id, serializer.validated_data)
        return Response(payload, status=status.HTTP_200_OK)

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


class PlanView(APIView):
    """POST computes and persists the authoritative plan; GET returns the stored
    one for the plan screen and for resuming a half-finished flow (PRD §4)."""

    def post(self, request):
        return Response(services.generate_plan(request.user.user_id))

    def get(self, request):
        plan = services.fetch_plan(request.user.user_id)
        if plan is None:
            return Response(
                {
                    "error": {
                        "code": "plan_not_found",
                        "message": "No plan has been generated yet.",
                    }
                },
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(plan)


class CompleteView(APIView):
    """POST /api/v1/onboarding/complete - stamp `onboardedAt` and finalise."""

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

    def get(self, request):
        return Response(repository.get_active_engine_config().to_public_dict())
