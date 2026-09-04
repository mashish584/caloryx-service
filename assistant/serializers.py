"""Request/response shapes for the Meal Assistant draft API (PRD §9, §10, §5.2.1).

Chunk 2a: structured (non-text) endpoints only - items are added by exact
`foodId`, never by name. Plain `Serializer`s throughout, following
meals/serializers.py's convention of separate request/response shapes plus
free `serialize_x()` helpers for Prisma rows.
"""
from __future__ import annotations

from typing import Any, Dict

from rest_framework import serializers

from chatparser import DraftStatus, ItemResolution, MassSource, MatchBand, ParseTier, QuantitySource
from engine.rounding import round_int
from meals.serializers import MAX_ITEMS_PER_MEAL, LoggedMealSerializer
from meals.services import food_per_100g
from nutrition import FoodState, MealSlot, NutrientVector, item_nutrition

# -- requests -----------------------------------------------------------------


class DraftItemPayloadSerializer(serializers.Serializer):
    """One item, by exact `foodId` - no free-text food matching in Chunk 2a
    (that's Chunk 2b)."""

    foodId = serializers.CharField()
    quantity = serializers.FloatField(min_value=0.01)
    unit = serializers.CharField()
    state = serializers.ChoiceField(choices=[s.value for s in FoodState], required=False)


class DraftCreateSerializer(serializers.Serializer):
    """POST /drafts. `name` is required for now - deterministic template
    naming is Chunk 4's `mealName` work (§5.1.3), not this chunk's. `slot` is
    optional: if omitted, inferred from `localHour` (the client's local hour,
    0-23) using the §5.1.2 default windows, falling back to UTC server time
    if neither is sent - true device-local inference is a client concern this
    just needs *a* signal for."""

    name = serializers.CharField(max_length=80)
    slot = serializers.ChoiceField(choices=[s.value for s in MealSlot], required=False)
    localHour = serializers.IntegerField(min_value=0, max_value=23, required=False)
    items = DraftItemPayloadSerializer(many=True)

    def validate_items(self, items):
        if not items:
            raise serializers.ValidationError("A draft needs at least one item.")
        if len(items) > MAX_ITEMS_PER_MEAL:
            raise serializers.ValidationError(
                "A draft can have at most {} items.".format(MAX_ITEMS_PER_MEAL)
            )
        return items


class DraftUpdateSerializer(serializers.Serializer):
    """PATCH /drafts/{id} - name and/or slot. `version` is the optimistic
    lock (§12.1): required on every mutating call."""

    name = serializers.CharField(max_length=80, required=False)
    slot = serializers.ChoiceField(choices=[s.value for s in MealSlot], required=False)
    version = serializers.IntegerField(min_value=1)

    def validate(self, attrs):
        if "name" not in attrs and "slot" not in attrs:
            raise serializers.ValidationError("Send name and/or slot to update.")
        return attrs


class DraftItemCreateSerializer(DraftItemPayloadSerializer):
    """POST /drafts/{id}/items."""

    version = serializers.IntegerField(min_value=1)


class DraftItemUpdateSerializer(serializers.Serializer):
    """PATCH /drafts/{id}/items/{itemId} - the Adjust Portion persistence
    call (§5.2.1: client does the live math, this endpoint exists to persist
    the final value once, not for interactivity)."""

    quantity = serializers.FloatField(min_value=0.01, required=False)
    unit = serializers.CharField(required=False)
    state = serializers.ChoiceField(choices=[s.value for s in FoodState], required=False)
    version = serializers.IntegerField(min_value=1)

    def validate(self, attrs):
        if not any(k in attrs for k in ("quantity", "unit", "state")):
            raise serializers.ValidationError("Send at least one field to update.")
        return attrs


class VersionSerializer(serializers.Serializer):
    """Body for DELETE endpoints (discard draft, remove item) - just the
    optimistic-lock version being acted against."""

    version = serializers.IntegerField(min_value=1)


class ConfirmSerializer(serializers.Serializer):
    """POST /drafts/{id}/confirm. `idempotencyKey` guards logging - separate
    from any message-level idempotency, which doesn't exist until Chunk 2b
    (§12.1: "messageId guards parsing, the confirm key guards logging")."""

    idempotencyKey = serializers.CharField(max_length=128)
    version = serializers.IntegerField(min_value=1)


# -- responses ------------------------------------------------------------


class PerGramSerializer(serializers.Serializer):
    """Per-gram nutrient rate (§5.2.1, §10.1) - what makes Adjust Portion's
    slider/stepper/chip math purely client-side. Deliberately NOT rounded:
    these are inputs to further client math, not a display value, and
    rounding an intermediate value is exactly what §8 says never to do."""

    kcal = serializers.FloatField()
    proteinG = serializers.FloatField()
    carbsG = serializers.FloatField()
    fatG = serializers.FloatField()


class MealDraftItemSerializer(serializers.Serializer):
    id = serializers.CharField()
    resolution = serializers.ChoiceField(choices=[r.value for r in ItemResolution])
    foodId = serializers.CharField(allow_null=True)
    foodName = serializers.CharField(allow_null=True)
    rawText = serializers.CharField()
    quantity = serializers.FloatField(allow_null=True)
    unit = serializers.CharField(allow_null=True)
    grams = serializers.FloatField(allow_null=True)
    state = serializers.ChoiceField(choices=[s.value for s in FoodState])
    defaultGrams = serializers.FloatField(allow_null=True)
    quantitySource = serializers.ChoiceField(
        choices=[s.value for s in QuantitySource], allow_null=True
    )
    massSource = serializers.ChoiceField(choices=[s.value for s in MassSource], allow_null=True)
    matchBand = serializers.ChoiceField(choices=[b.value for b in MatchBand], allow_null=True)
    caloriesKcal = serializers.IntegerField(allow_null=True)
    proteinG = serializers.IntegerField(allow_null=True)
    carbsG = serializers.IntegerField(allow_null=True)
    fatG = serializers.IntegerField(allow_null=True)
    fiberG = serializers.IntegerField(allow_null=True)
    perGram = PerGramSerializer(allow_null=True)


class MealDraftTotalsSerializer(serializers.Serializer):
    caloriesKcal = serializers.IntegerField()
    proteinG = serializers.IntegerField()
    carbsG = serializers.IntegerField()
    fatG = serializers.IntegerField()
    fiberG = serializers.IntegerField(allow_null=True)


class MealDraftSerializer(serializers.Serializer):
    id = serializers.CharField()
    version = serializers.IntegerField()
    status = serializers.ChoiceField(choices=[s.value for s in DraftStatus])
    name = serializers.CharField()
    slot = serializers.ChoiceField(choices=[s.value for s in MealSlot])
    parseTier = serializers.ChoiceField(choices=[t.value for t in ParseTier])
    confidence = serializers.FloatField()
    totals = MealDraftTotalsSerializer()
    items = MealDraftItemSerializer(many=True)


class DailyTotalsSerializer(serializers.Serializer):
    """Sum of today's `LoggedMeal`s, returned with confirm so Home-shaped
    totals update without a refetch (§10.1). No `remainingKcal` here - that
    needs the user's plan/goal (onboarding's domain), which this endpoint
    deliberately doesn't reach into yet."""

    caloriesKcal = serializers.IntegerField()
    proteinG = serializers.IntegerField()
    carbsG = serializers.IntegerField()
    fatG = serializers.IntegerField()
    fiberG = serializers.IntegerField(allow_null=True)


class ConfirmResponseSerializer(serializers.Serializer):
    loggedMeal = LoggedMealSerializer()
    dailyTotals = DailyTotalsSerializer()


# -- serialize_x() helpers ---------------------------------------------------


def _per_gram(food: Any) -> Dict[str, float]:
    return {
        "kcal": food.caloriesKcalPer100g / 100.0,
        "proteinG": food.proteinGPer100g / 100.0,
        "carbsG": food.carbsGPer100g / 100.0,
        "fatG": food.fatGPer100g / 100.0,
    }


def item_nutrient_vector(item: Any) -> NutrientVector:
    """A draft item's nutrition is computed live from the catalog, not stored
    (see the MealDraftItem schema comment) - it's mutable and unconfirmed, so
    freezing it would just be a staleness bug waiting to happen. Public:
    `assistant.services` reuses this for totals recomputation, so there's one
    place that turns a draft item row into a nutrient vector."""
    return item_nutrition(food_per_100g(item.food), item.grams)


def serialize_draft_item(item: Any) -> Dict[str, Any]:
    if item.resolution != ItemResolution.RESOLVED.value or item.food is None:
        # Unreachable in Chunk 2a (every item is RESOLVED with a food), kept
        # so this helper doesn't need rewriting when Chunk 2b/5 land.
        return {
            "id": item.id,
            "resolution": item.resolution,
            "foodId": None,
            "foodName": None,
            "rawText": item.rawText,
            "quantity": item.quantity,
            "unit": item.unit,
            "grams": item.grams,
            "state": item.state,
            "defaultGrams": item.defaultGrams,
            "quantitySource": item.quantitySource,
            "massSource": item.massSource,
            "matchBand": item.matchBand,
            "caloriesKcal": None,
            "proteinG": None,
            "carbsG": None,
            "fatG": None,
            "fiberG": None,
            "perGram": None,
        }

    nutrition = item_nutrient_vector(item)
    return {
        "id": item.id,
        "resolution": item.resolution,
        "foodId": item.foodId,
        "foodName": item.food.name,
        "rawText": item.rawText,
        "quantity": item.quantity,
        "unit": item.unit,
        "grams": item.grams,
        "state": item.state,
        "defaultGrams": item.defaultGrams,
        "quantitySource": item.quantitySource,
        "massSource": item.massSource,
        "matchBand": item.matchBand,
        "caloriesKcal": round_int(nutrition.calories_kcal),
        "proteinG": round_int(nutrition.protein_g),
        "carbsG": round_int(nutrition.carbs_g),
        "fatG": round_int(nutrition.fat_g),
        "fiberG": round_int(nutrition.fiber_g) if nutrition.fiber_g is not None else None,
        "perGram": _per_gram(item.food),
    }


def serialize_draft(draft: Any) -> Dict[str, Any]:
    return {
        "id": draft.id,
        "version": draft.version,
        "status": draft.status,
        "name": draft.name,
        "slot": draft.slot,
        "parseTier": draft.parseTier,
        "confidence": draft.confidence,
        "totals": {
            "caloriesKcal": round_int(draft.caloriesKcal),
            "proteinG": round_int(draft.proteinG),
            "carbsG": round_int(draft.carbsG),
            "fatG": round_int(draft.fatG),
            "fiberG": round_int(draft.fiberG) if draft.fiberG is not None else None,
        },
        "items": [serialize_draft_item(item) for item in draft.items],
    }


def serialize_daily_totals(totals: NutrientVector) -> Dict[str, Any]:
    return {
        "caloriesKcal": round_int(totals.calories_kcal),
        "proteinG": round_int(totals.protein_g),
        "carbsG": round_int(totals.carbs_g),
        "fatG": round_int(totals.fat_g),
        "fiberG": round_int(totals.fiber_g) if totals.fiber_g is not None else None,
    }
