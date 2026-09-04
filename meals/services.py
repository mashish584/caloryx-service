"""Meal-logging use cases (PRD §6, §8) - Chunk 1 of the AI Meal Assistant
roadmap: no chat, no drafts, no AI. Views stay thin: they validate, call one
of these, and render - see onboarding/services.py for the pattern.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from common.exceptions import NotFoundError, UnresolvableQuantityError
from nutrition import (
    FoodState,
    NutrientVector,
    NutritionError,
    ServingUnit,
    UnknownServingUnitError,
    apply_yield,
    item_nutrition,
    resolve_grams,
    sum_nutrition,
)

from . import repository
from .serializers import serialize_food, serialize_logged_meal

logger = logging.getLogger(__name__)


def _serving_units(food: Any) -> List[ServingUnit]:
    return [
        ServingUnit(unit=su.unit, grams=su.grams, type=su.type) for su in food.servingUnits
    ]


def food_per_100g(food: Any) -> NutrientVector:
    """Public: `assistant.serializers`/`assistant.services` reuse this to
    compute a draft item's nutrition live (drafts store no nutrition columns
    of their own - see the MealDraftItem schema comment)."""
    return NutrientVector(
        calories_kcal=food.caloriesKcalPer100g,
        protein_g=food.proteinGPer100g,
        carbs_g=food.carbsGPer100g,
        fat_g=food.fatGPer100g,
        fiber_g=food.fiberGPer100g,
    )


def resolve_item(
    food: Any, quantity: float, unit: str, state: Optional[str]
) -> Tuple[Dict[str, Any], NutrientVector]:
    """One input item -> `{grams, state, nutrition}` against `food`'s catalog
    data. `state` defaults to the food's own default state when the caller
    didn't send one - there is nothing to convert (§8).

    Public (not `_`-prefixed): `assistant.services` reuses this directly for
    draft items and the confirm->LoggedMeal handoff, rather than duplicating
    the quantity/unit/yield-resolution logic Chunk 1 already tested.
    """
    item_state = FoodState(state) if state else FoodState(food.defaultState)
    default_state = FoodState(food.defaultState)

    try:
        stated_grams = resolve_grams(quantity, unit, _serving_units(food))
    except UnknownServingUnitError as exc:
        raise UnresolvableQuantityError(str(exc), details={"unit": unit}) from exc

    try:
        basis_grams = apply_yield(
            stated_grams,
            from_state=item_state,
            to_state=default_state,
            raw_to_cooked_yield=food.rawToCookedYield,
        )
    except NutritionError as exc:
        raise UnresolvableQuantityError(str(exc), details={"state": item_state.value}) from exc

    nutrition = item_nutrition(food_per_100g(food), basis_grams)
    item = {
        "foodId": food.id,
        "quantity": quantity,
        "unit": unit,
        "grams": stated_grams,
        "state": item_state.value,
        "caloriesKcal": nutrition.calories_kcal,
        "proteinG": nutrition.protein_g,
        "carbsG": nutrition.carbs_g,
        "fatG": nutrition.fat_g,
        "fiberG": nutrition.fiber_g,
    }
    return item, nutrition


def _totals_payload(totals: NutrientVector) -> Dict[str, Any]:
    return {
        "caloriesKcal": totals.calories_kcal,
        "proteinG": totals.protein_g,
        "carbsG": totals.carbs_g,
        "fatG": totals.fat_g,
        "fiberG": totals.fiber_g,
    }


def search_foods(query: str) -> Dict[str, Any]:
    foods = repository.search_foods(query)
    return {"foods": [serialize_food(f) for f in foods]}


def log_meal(user_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """Create a `LoggedMeal` from explicit items (§4's "fully quantified" use
    case). Ingredients are the source of truth (§6): totals are always the sum
    of item nutrition at log time, never an independently authored number."""
    items_input = data["items"]
    resolved_items: List[Dict[str, Any]] = []
    vectors: List[NutrientVector] = []
    for raw in items_input:
        food = repository.get_food(raw["foodId"])
        if food is None:
            raise NotFoundError(
                "Food not found.", code="food_not_found", details={"foodId": raw["foodId"]}
            )
        item, vector = resolve_item(food, raw["quantity"], raw["unit"], raw.get("state"))
        resolved_items.append(item)
        vectors.append(vector)

    meal_data = dict(name=data["name"], slot=data["slot"], **_totals_payload(sum_nutrition(vectors)))
    meal = repository.create_logged_meal(user_id, meal_data, resolved_items)
    logger.info("meal logged user=%s meal=%s items=%s", user_id, meal.id, len(resolved_items))
    return serialize_logged_meal(meal)


def list_logged_meals(user_id: str, *, slot: Optional[str] = None) -> Dict[str, Any]:
    meals = repository.list_logged_meals(user_id, slot=slot)
    return {"meals": [serialize_logged_meal(m) for m in meals]}


def fetch_logged_meal(user_id: str, meal_id: str) -> Dict[str, Any]:
    meal = repository.get_logged_meal(user_id, meal_id)
    if meal is None:
        raise NotFoundError("Meal not found.", code="meal_not_found")
    return serialize_logged_meal(meal)


def delete_logged_meal(user_id: str, meal_id: str) -> None:
    if not repository.delete_logged_meal(user_id, meal_id):
        raise NotFoundError("Meal not found.", code="meal_not_found")


def _recompute_meal_totals(user_id: str, meal_id: str) -> Any:
    meal = repository.get_logged_meal(user_id, meal_id)
    vectors = [
        NutrientVector(item.caloriesKcal, item.proteinG, item.carbsG, item.fatG, item.fiberG)
        for item in meal.items
    ]
    return repository.update_logged_meal_totals(meal_id, _totals_payload(sum_nutrition(vectors)))


def update_logged_meal_item(
    user_id: str, meal_id: str, item_id: str, data: Dict[str, Any]
) -> Dict[str, Any]:
    item = repository.get_logged_meal_item(user_id, meal_id, item_id)
    if item is None:
        raise NotFoundError("Item not found.", code="meal_item_not_found")

    food = repository.get_food(item.foodId)
    quantity = data.get("quantity", item.quantity)
    unit = data.get("unit", item.unit)
    state = data.get("state", item.state)
    resolved, _ = resolve_item(food, quantity, unit, state)
    repository.update_logged_meal_item(item_id, resolved)

    meal = _recompute_meal_totals(user_id, meal_id)
    logger.info("meal item updated user=%s meal=%s item=%s", user_id, meal_id, item_id)
    return serialize_logged_meal(meal)


def delete_logged_meal_item(user_id: str, meal_id: str, item_id: str) -> Dict[str, Any]:
    item = repository.get_logged_meal_item(user_id, meal_id, item_id)
    if item is None:
        raise NotFoundError("Item not found.", code="meal_item_not_found")

    repository.delete_logged_meal_item(item_id)
    meal = _recompute_meal_totals(user_id, meal_id)
    logger.info("meal item removed user=%s meal=%s item=%s", user_id, meal_id, item_id)
    return serialize_logged_meal(meal)
