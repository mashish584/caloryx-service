"""Request/response shapes for meal logging (PRD §6, §8).

Chunk 1 of the AI Meal Assistant roadmap: explicit item entry only - no chat,
no drafts, no AI. Plain `Serializer`s throughout (there is no Django model),
following onboarding/serializers.py's convention of separate request/response
shapes plus free `serialize_x()` helpers for Prisma rows.
"""
from __future__ import annotations

from typing import Any, Dict

from rest_framework import serializers

from engine.rounding import round_int
from nutrition import FoodSource, FoodState, LoggedMealSource, MealSlot, ServingUnitType

# §12.4 of the AI PRD bounds a draft at 25 items; nothing about that cap is
# chat-specific, so the same ceiling applies to a manually-entered meal.
MAX_ITEMS_PER_MEAL = 25


class FoodServingUnitSerializer(serializers.Serializer):
    unit = serializers.CharField()
    grams = serializers.FloatField()
    type = serializers.ChoiceField(choices=[t.value for t in ServingUnitType])


class FoodSerializer(serializers.Serializer):
    """A catalog entry (§8). Nutrients are per 100g, on `defaultState` basis."""

    id = serializers.CharField()
    name = serializers.CharField()
    source = serializers.ChoiceField(choices=[s.value for s in FoodSource])
    defaultState = serializers.ChoiceField(choices=[s.value for s in FoodState])
    caloriesKcalPer100g = serializers.FloatField()
    proteinGPer100g = serializers.FloatField()
    carbsGPer100g = serializers.FloatField()
    fatGPer100g = serializers.FloatField()
    fiberGPer100g = serializers.FloatField(allow_null=True)
    servingUnits = FoodServingUnitSerializer(many=True)


class FoodSearchResponseSerializer(serializers.Serializer):
    foods = FoodSerializer(many=True)


class LoggedMealItemInputSerializer(serializers.Serializer):
    foodId = serializers.CharField()
    quantity = serializers.FloatField(min_value=0.01)
    unit = serializers.CharField()
    # Defaults to the food's own `defaultState` in the service layer when
    # omitted - there is nothing to convert if the caller stated nothing.
    state = serializers.ChoiceField(choices=[s.value for s in FoodState], required=False)


class LoggedMealCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=80)
    slot = serializers.ChoiceField(choices=[s.value for s in MealSlot])
    items = LoggedMealItemInputSerializer(many=True)

    def validate_items(self, items):
        if not items:
            raise serializers.ValidationError("A meal needs at least one item.")
        if len(items) > MAX_ITEMS_PER_MEAL:
            raise serializers.ValidationError(
                "A meal can have at most {} items.".format(MAX_ITEMS_PER_MEAL)
            )
        return items


class LoggedMealItemUpdateSerializer(serializers.Serializer):
    quantity = serializers.FloatField(min_value=0.01, required=False)
    unit = serializers.CharField(required=False)
    state = serializers.ChoiceField(choices=[s.value for s in FoodState], required=False)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("Send at least one field to update.")
        return attrs


class LoggedMealItemSerializer(serializers.Serializer):
    id = serializers.CharField()
    foodId = serializers.CharField()
    foodName = serializers.CharField()
    quantity = serializers.FloatField()
    unit = serializers.CharField()
    grams = serializers.FloatField()
    state = serializers.ChoiceField(choices=[s.value for s in FoodState])
    caloriesKcal = serializers.IntegerField()
    proteinG = serializers.IntegerField()
    carbsG = serializers.IntegerField()
    fatG = serializers.IntegerField()
    fiberG = serializers.IntegerField(allow_null=True)


class LoggedMealTotalsSerializer(serializers.Serializer):
    caloriesKcal = serializers.IntegerField()
    proteinG = serializers.IntegerField()
    carbsG = serializers.IntegerField()
    fatG = serializers.IntegerField()
    fiberG = serializers.IntegerField(allow_null=True)


class LoggedMealSerializer(serializers.Serializer):
    id = serializers.CharField()
    name = serializers.CharField()
    slot = serializers.ChoiceField(choices=[s.value for s in MealSlot])
    source = serializers.ChoiceField(choices=[s.value for s in LoggedMealSource])
    loggedAt = serializers.DateTimeField()
    totals = LoggedMealTotalsSerializer()
    items = LoggedMealItemSerializer(many=True)


class LoggedMealListResponseSerializer(serializers.Serializer):
    meals = LoggedMealSerializer(many=True)


def serialize_food(food: Any) -> Dict[str, Any]:
    return {
        "id": food.id,
        "name": food.name,
        "source": food.source,
        "defaultState": food.defaultState,
        "caloriesKcalPer100g": food.caloriesKcalPer100g,
        "proteinGPer100g": food.proteinGPer100g,
        "carbsGPer100g": food.carbsGPer100g,
        "fatGPer100g": food.fatGPer100g,
        "fiberGPer100g": food.fiberGPer100g,
        "servingUnits": [
            {"unit": su.unit, "grams": su.grams, "type": su.type}
            for su in food.servingUnits
        ],
    }


def _serialize_item(item: Any) -> Dict[str, Any]:
    return {
        "id": item.id,
        "foodId": item.foodId,
        "foodName": item.food.name,
        "quantity": item.quantity,
        "unit": item.unit,
        "grams": item.grams,
        "state": item.state,
        "caloriesKcal": round_int(item.caloriesKcal),
        "proteinG": round_int(item.proteinG),
        "carbsG": round_int(item.carbsG),
        "fatG": round_int(item.fatG),
        "fiberG": round_int(item.fiberG) if item.fiberG is not None else None,
    }


def serialize_logged_meal(meal: Any) -> Dict[str, Any]:
    # Rounding happens exactly once, here, at the response boundary (§8) - the
    # stored totals and every stored item stay full-precision so this is the
    # only place kcal/macros are ever rounded.
    return {
        "id": meal.id,
        "name": meal.name,
        "slot": meal.slot,
        "source": meal.source,
        "loggedAt": meal.loggedAt.isoformat(),
        "totals": {
            "caloriesKcal": round_int(meal.caloriesKcal),
            "proteinG": round_int(meal.proteinG),
            "carbsG": round_int(meal.carbsG),
            "fatG": round_int(meal.fatG),
            "fiberG": round_int(meal.fiberG) if meal.fiberG is not None else None,
        },
        "items": [_serialize_item(item) for item in meal.items],
    }
