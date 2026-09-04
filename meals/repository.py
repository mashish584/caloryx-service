"""Food catalog and logged-meal persistence (Prisma).

The only module in this app that touches `get_client()` - services.py owns the
business logic (unit resolution, yield conversion, totals), this owns reads
and writes. Mirrors onboarding/repository.py.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from common.db import get_client

_FOOD_WITH_UNITS = {"servingUnits": True}
_MEAL_WITH_ITEMS = {"items": {"include": {"food": {"include": {"servingUnits": True}}}}}


# -- food catalog ------------------------------------------------------------


def get_food(food_id: str) -> Optional[Any]:
    return get_client().food.find_unique(where={"id": food_id}, include=_FOOD_WITH_UNITS)


def search_foods(query: str, *, limit: int = 20) -> List[Any]:
    where: Dict[str, Any] = {}
    if query:
        where["name"] = {"contains": query, "mode": "insensitive"}
    return get_client().food.find_many(
        where=where, include=_FOOD_WITH_UNITS, order={"name": "asc"}, take=limit
    )


def create_food(data: Dict[str, Any]) -> Any:
    """Used by catalog-seeding tooling (`manage.py seed_foods`), not the
    logging API - there is no endpoint that writes to the catalog."""
    serving_units = data.pop("servingUnits", [])
    food = get_client().food.create(data=data)
    for serving_unit in serving_units:
        get_client().foodservingunit.create(
            data=dict(serving_unit, food={"connect": {"id": food.id}})
        )
    return get_food(food.id)


# -- logged meals -------------------------------------------------------------


def create_logged_meal(
    user_id: str, meal_data: Dict[str, Any], items_data: List[Dict[str, Any]]
) -> Any:
    payload = dict(meal_data, user={"connect": {"id": user_id}})
    payload["items"] = {"create": items_data}
    return get_client().loggedmeal.create(data=payload, include=_MEAL_WITH_ITEMS)


def get_logged_meal(user_id: str, meal_id: str) -> Optional[Any]:
    meal = get_client().loggedmeal.find_unique(
        where={"id": meal_id}, include=_MEAL_WITH_ITEMS
    )
    if meal is None or meal.userId != user_id:
        # Same 404 either way - a meal id that exists but belongs to someone
        # else must not be distinguishable from one that doesn't exist at all.
        return None
    return meal


def list_logged_meals(
    user_id: str,
    *,
    slot: Optional[str] = None,
    logged_after: Optional[datetime] = None,
    limit: int = 50,
) -> List[Any]:
    where: Dict[str, Any] = {"userId": user_id}
    if slot:
        where["slot"] = slot
    if logged_after is not None:
        # Used by assistant.services for "today's totals" on confirm - a plain
        # gte filter rather than a dedicated day-boundary query, since the
        # caller already knows what "today" means (UTC vs. local is its call).
        where["loggedAt"] = {"gte": logged_after}
    return get_client().loggedmeal.find_many(
        where=where, include=_MEAL_WITH_ITEMS, order={"loggedAt": "desc"}, take=limit
    )


def delete_logged_meal(user_id: str, meal_id: str) -> bool:
    if get_logged_meal(user_id, meal_id) is None:
        return False
    get_client().loggedmeal.delete(where={"id": meal_id})
    return True


def get_logged_meal_item(user_id: str, meal_id: str, item_id: str) -> Optional[Any]:
    meal = get_logged_meal(user_id, meal_id)
    if meal is None:
        return None
    return next((item for item in meal.items if item.id == item_id), None)


def update_logged_meal_item(item_id: str, data: Dict[str, Any]) -> Any:
    return get_client().loggedmealitem.update(
        where={"id": item_id},
        data=data,
        include={"food": {"include": {"servingUnits": True}}},
    )


def delete_logged_meal_item(item_id: str) -> None:
    get_client().loggedmealitem.delete(where={"id": item_id})


def update_logged_meal_totals(meal_id: str, totals: Dict[str, Any]) -> Any:
    return get_client().loggedmeal.update(
        where={"id": meal_id}, data=totals, include=_MEAL_WITH_ITEMS
    )
