"""Meal-logging endpoints (PRD §6, §8, §10-shaped) - explicit item entry, no
chat/AI (Chunk 1 of the AI Meal Assistant roadmap)."""
from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from common.schema import SERVER_ERROR, UNAUTHORIZED, VALIDATION_ERROR, error_response

from . import services
from .serializers import (
    FoodSearchResponseSerializer,
    LoggedMealCreateSerializer,
    LoggedMealItemUpdateSerializer,
    LoggedMealListResponseSerializer,
    LoggedMealSerializer,
)

_INVALID_QUANTITY = error_response(
    "The quantity could not be resolved to a mass - an unrecognised unit, or "
    "a raw/cooked conversion with no yield factor on the food.",
    "invalid_quantity",
)


class FoodSearchView(APIView):
    """GET /api/v1/meals/foods?q=... - catalog lookup for composing a manual log."""

    @extend_schema(
        parameters=[
            OpenApiParameter("q", str, required=False, description="Substring match on food name.")
        ],
        responses={200: FoodSearchResponseSerializer, 401: UNAUTHORIZED, 500: SERVER_ERROR},
    )
    def get(self, request):
        query = request.query_params.get("q", "")
        return Response(services.search_foods(query))


class LoggedMealListCreateView(APIView):
    """POST creates a fully-quantified meal (§4); GET lists this user's meals."""

    @extend_schema(
        operation_id="meals_create",
        request=LoggedMealCreateSerializer,
        responses={
            201: LoggedMealSerializer,
            400: VALIDATION_ERROR,
            401: UNAUTHORIZED,
            404: error_response("A referenced food does not exist.", "food_not_found"),
            422: _INVALID_QUANTITY,
            500: SERVER_ERROR,
        },
    )
    def post(self, request):
        serializer = LoggedMealCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = services.log_meal(request.user.user_id, serializer.validated_data)
        return Response(payload, status=status.HTTP_201_CREATED)

    @extend_schema(
        operation_id="meals_list",
        parameters=[OpenApiParameter("slot", str, required=False)],
        responses={200: LoggedMealListResponseSerializer, 401: UNAUTHORIZED, 500: SERVER_ERROR},
    )
    def get(self, request):
        slot = request.query_params.get("slot")
        return Response(services.list_logged_meals(request.user.user_id, slot=slot))


class LoggedMealDetailView(APIView):
    @extend_schema(
        operation_id="meals_retrieve",
        responses={
            200: LoggedMealSerializer,
            401: UNAUTHORIZED,
            404: error_response("No meal with this id for this user.", "meal_not_found"),
            500: SERVER_ERROR,
        }
    )
    def get(self, request, meal_id):
        return Response(services.fetch_logged_meal(request.user.user_id, meal_id))

    @extend_schema(
        operation_id="meals_destroy",
        responses={
            204: None,
            401: UNAUTHORIZED,
            404: error_response("No meal with this id for this user.", "meal_not_found"),
            500: SERVER_ERROR,
        }
    )
    def delete(self, request, meal_id):
        services.delete_logged_meal(request.user.user_id, meal_id)
        return Response(status=status.HTTP_204_NO_CONTENT)


class LoggedMealItemView(APIView):
    """PATCH/DELETE one item - the manual-entry equivalent of the PRD's
    per-item endpoints (§10), recomputing the meal's totals on any change."""

    @extend_schema(
        operation_id="meals_items_update",
        request=LoggedMealItemUpdateSerializer,
        responses={
            200: LoggedMealSerializer,
            400: VALIDATION_ERROR,
            401: UNAUTHORIZED,
            404: error_response("No such meal or item for this user.", "meal_item_not_found"),
            422: _INVALID_QUANTITY,
            500: SERVER_ERROR,
        },
    )
    def patch(self, request, meal_id, item_id):
        serializer = LoggedMealItemUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = services.update_logged_meal_item(
            request.user.user_id, meal_id, item_id, serializer.validated_data
        )
        return Response(payload)

    @extend_schema(
        operation_id="meals_items_destroy",
        responses={
            200: LoggedMealSerializer,
            401: UNAUTHORIZED,
            404: error_response("No such meal or item for this user.", "meal_item_not_found"),
            500: SERVER_ERROR,
        }
    )
    def delete(self, request, meal_id, item_id):
        payload = services.delete_logged_meal_item(request.user.user_id, meal_id, item_id)
        return Response(payload)
