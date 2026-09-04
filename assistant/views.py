"""Meal Assistant draft endpoints (PRD §9, §10, §5.2.1) - structured (non-text)
mutation API, Chunk 2a. `POST /messages` and everything text-driven is Chunk 2b.
"""
from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from common.schema import SERVER_ERROR, UNAUTHORIZED, VALIDATION_ERROR, error_response

from . import services
from .serializers import (
    ConfirmResponseSerializer,
    ConfirmSerializer,
    DraftCreateSerializer,
    DraftItemCreateSerializer,
    DraftItemUpdateSerializer,
    DraftUpdateSerializer,
    MealDraftSerializer,
    VersionSerializer,
)

_DRAFT_NOT_FOUND = error_response(
    "No draft with this id for this user.", "draft_not_found"
)
_FOOD_NOT_FOUND = error_response("A referenced food does not exist.", "food_not_found")
_INVALID_QUANTITY = error_response(
    "The quantity could not be resolved to a mass - an unrecognised unit, or "
    "a raw/cooked conversion with no yield factor on the food.",
    "invalid_quantity",
)
_MUTATION_ERRORS = {
    404: _DRAFT_NOT_FOUND,
    409: error_response(
        "Either the draft is no longer OPEN, or its version is stale - see "
        "`error.code` (`draft_not_open` | `draft_version_conflict`).",
        "draft_not_open",
        "draft_version_conflict",
    ),
}


class DraftCreateView(APIView):
    """POST creates a draft from structured items (§4's "fully quantified"
    use case, chat-shaped) - one open draft per user (§9, §12.2). No `GET`
    (list) here: a user has at most one open draft, and past drafts aren't
    listed in Chunk 2a."""

    @extend_schema(
        operation_id="assistant_drafts_create",
        request=DraftCreateSerializer,
        responses={
            201: MealDraftSerializer,
            400: VALIDATION_ERROR,
            401: UNAUTHORIZED,
            404: _FOOD_NOT_FOUND,
            409: error_response(
                "The user already has an open draft. It's attached under "
                "`error.details.draft` so the client can offer to resume or "
                "discard it.",
                "open_draft_exists",
            ),
            422: _INVALID_QUANTITY,
            500: SERVER_ERROR,
        },
    )
    def post(self, request):
        serializer = DraftCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = services.create_draft(request.user.user_id, serializer.validated_data)
        return Response(payload, status=status.HTTP_201_CREATED)


class DraftDetailView(APIView):
    @extend_schema(
        operation_id="assistant_drafts_retrieve",
        responses={200: MealDraftSerializer, 401: UNAUTHORIZED, 404: _DRAFT_NOT_FOUND, 500: SERVER_ERROR},
    )
    def get(self, request, draft_id):
        return Response(services.fetch_draft(request.user.user_id, draft_id))

    @extend_schema(
        operation_id="assistant_drafts_update",
        request=DraftUpdateSerializer,
        responses={
            200: MealDraftSerializer,
            400: VALIDATION_ERROR,
            401: UNAUTHORIZED,
            **_MUTATION_ERRORS,
            500: SERVER_ERROR,
        },
    )
    def patch(self, request, draft_id):
        serializer = DraftUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = services.update_draft(request.user.user_id, draft_id, serializer.validated_data)
        return Response(payload)

    @extend_schema(
        operation_id="assistant_drafts_discard",
        request=VersionSerializer,
        responses={
            200: MealDraftSerializer,
            400: VALIDATION_ERROR,
            401: UNAUTHORIZED,
            **_MUTATION_ERRORS,
            500: SERVER_ERROR,
        },
    )
    def delete(self, request, draft_id):
        serializer = VersionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = services.discard_draft(
            request.user.user_id, draft_id, serializer.validated_data["version"]
        )
        return Response(payload)


class DraftItemCreateView(APIView):
    """POST /drafts/{id}/items - add an item, by exact `foodId`."""

    @extend_schema(
        operation_id="assistant_draft_items_create",
        request=DraftItemCreateSerializer,
        responses={
            201: MealDraftSerializer,
            400: VALIDATION_ERROR,
            401: UNAUTHORIZED,
            404: error_response(
                "No such draft for this user, or a referenced food does not exist.",
                "draft_not_found",
                "food_not_found",
            ),
            409: _MUTATION_ERRORS[409],
            422: _INVALID_QUANTITY,
            500: SERVER_ERROR,
        },
    )
    def post(self, request, draft_id):
        serializer = DraftItemCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = services.add_draft_item(
            request.user.user_id, draft_id, serializer.validated_data
        )
        return Response(payload, status=status.HTTP_201_CREATED)


class DraftItemDetailView(APIView):
    """PATCH is the Adjust Portion persistence call (§5.2.1) - the client
    does the live math and PATCHes once on "Update Portion", not per slider
    frame."""

    @extend_schema(
        operation_id="assistant_draft_items_update",
        request=DraftItemUpdateSerializer,
        responses={
            200: MealDraftSerializer,
            400: VALIDATION_ERROR,
            401: UNAUTHORIZED,
            404: error_response(
                "No such draft or item for this user.", "draft_not_found", "draft_item_not_found"
            ),
            409: _MUTATION_ERRORS[409],
            422: _INVALID_QUANTITY,
            500: SERVER_ERROR,
        },
    )
    def patch(self, request, draft_id, item_id):
        serializer = DraftItemUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = services.update_draft_item(
            request.user.user_id, draft_id, item_id, serializer.validated_data
        )
        return Response(payload)

    @extend_schema(
        operation_id="assistant_draft_items_destroy",
        request=VersionSerializer,
        responses={
            200: MealDraftSerializer,
            400: VALIDATION_ERROR,
            401: UNAUTHORIZED,
            404: error_response(
                "No such draft or item for this user.", "draft_not_found", "draft_item_not_found"
            ),
            409: _MUTATION_ERRORS[409],
            500: SERVER_ERROR,
        },
    )
    def delete(self, request, draft_id, item_id):
        serializer = VersionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = services.delete_draft_item(
            request.user.user_id, draft_id, item_id, serializer.validated_data["version"]
        )
        return Response(payload)


class DraftConfirmView(APIView):
    """POST /drafts/{id}/confirm -> creates a `LoggedMeal` (§9, §12.1, §12.5).
    `idempotencyKey` guards logging; double-taps replay the original response."""

    @extend_schema(
        operation_id="assistant_drafts_confirm",
        request=ConfirmSerializer,
        responses={
            201: ConfirmResponseSerializer,
            400: VALIDATION_ERROR,
            401: UNAUTHORIZED,
            **_MUTATION_ERRORS,
            500: SERVER_ERROR,
        },
    )
    def post(self, request, draft_id):
        serializer = ConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = services.confirm_draft(
            request.user.user_id,
            draft_id,
            serializer.validated_data["idempotencyKey"],
            serializer.validated_data["version"],
        )
        return Response(payload, status=status.HTTP_201_CREATED)
