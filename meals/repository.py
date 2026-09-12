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


# Trigram candidate retrieval over the whole catalog, served by
# `Food_name_trgm_idx` (GIN, gin_trgm_ops): a substring hit or a word-level
# trigram match (`<%`, pg_trgm.word_similarity_threshold, default 0.6) is a
# candidate. Catalog names lead with the food itself ("Milk, whole, ..."), so
# names whose comma-delimited head *is* the query come first - generic
# sources ahead of branded within that, or a global Open Food Facts import's
# hundreds of products literally named "Hummus" would fill the window before
# USDA's "Hummus, plain" - then names that start with it, then closest
# word-similarity, then shorter names. Callers re-rank in Python - this only
# has to get the right food into the window, not pick it.
_SEARCH_SQL = """
SELECT id FROM "Food"
WHERE name ILIKE $2 ESCAPE '\\' OR $1 <% name
ORDER BY (lower(name) = lower($1) OR name ILIKE $3 ESCAPE '\\') DESC,
         (source = 'OPEN_FOOD_FACTS') ASC,
         (name ILIKE $4 ESCAPE '\\') DESC,
         word_similarity($1, name) DESC, length(name) ASC, id ASC
LIMIT $5
"""


def search_foods(query: str, *, limit: int = 20) -> List[Any]:
    query = query.strip()
    if not query:
        return get_client().food.find_many(
            include=_FOOD_WITH_UNITS, order={"name": "asc"}, take=limit
        )
    escaped = _escape_like(query)
    rows = get_client().query_raw(
        _SEARCH_SQL,
        query,
        "%{}%".format(escaped),  # contains
        "{},%".format(escaped),  # head is the query: "chicken,%"
        "{}%".format(escaped),  # starts with
        limit,
    )
    ids = [row["id"] for row in rows]
    if not ids:
        return []
    foods = get_client().food.find_many(where={"id": {"in": ids}}, include=_FOOD_WITH_UNITS)
    rank = {food_id: i for i, food_id in enumerate(ids)}
    return sorted(foods, key=lambda food: rank[food.id])


def _escape_like(text: str) -> str:
    """User text is a literal inside the ILIKE pattern, not a wildcard."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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


# -- bulk ingestion (`manage.py ingest_foods`, PRD §8 "Data sources") ---------


def bulk_insert_foods(records: List[Any]) -> int:
    """Insert a batch of `meals.ingest.FoodRecord`s with their serving units.
    Rows already present by `(source, sourceRef)` are skipped, not updated -
    re-running an import is safe. Returns the number of new foods."""
    if not records:
        return 0
    client = get_client()
    inserted = client.food.create_many(
        data=[record.to_food_data() for record in records], skip_duplicates=True
    )

    refs_by_source: Dict[str, List[str]] = {}
    for record in records:
        if record.serving_units:
            refs_by_source.setdefault(record.source, []).append(record.source_ref)
    units_by_key = {(r.source, r.source_ref): r.serving_units for r in records}
    unit_rows: List[Dict[str, Any]] = []
    for source, refs in refs_by_source.items():
        for food in client.food.find_many(where={"source": source, "sourceRef": {"in": refs}}):
            for unit in units_by_key[(source, food.sourceRef)]:
                unit_rows.append(
                    {"foodId": food.id, "unit": unit.unit, "grams": unit.grams, "type": unit.type}
                )
    if unit_rows:
        client.foodservingunit.create_many(data=unit_rows, skip_duplicates=True)
    return inserted


def upsert_food(record: Any) -> Any:
    """`ingest_foods --update-existing`: refresh one food's nutrients (e.g. a
    new USDA release) in place. Serving units are upserted per unit, never
    deleted, so a unit a curator added by hand survives a re-import."""
    client = get_client()
    data = record.to_food_data()
    food = client.food.upsert(
        where={"source_sourceRef": {"source": record.source, "sourceRef": record.source_ref}},
        data={"create": data, "update": data},
    )
    for unit in record.serving_units:
        client.foodservingunit.upsert(
            where={"foodId_unit": {"foodId": food.id, "unit": unit.unit}},
            data={
                "create": {"foodId": food.id, "unit": unit.unit, "grams": unit.grams, "type": unit.type},
                "update": {"grams": unit.grams, "type": unit.type},
            },
        )
    return food


# -- composite foods (Chunk 3, §7.6) -----------------------------------------

_COMPOSITE_WITH_COMPONENTS = {"components": {"include": {"food": {"include": {"servingUnits": True}}}}}


def get_composite_foods() -> List[Any]:
    return get_client().compositefood.find_many(include=_COMPOSITE_WITH_COMPONENTS)


def create_composite_food(data: Dict[str, Any]) -> Any:
    """Used by `manage.py seed_composite_foods`, not any API endpoint - same
    seeding-only status as `create_food`."""
    components = data.pop("components", [])
    composite = get_client().compositefood.create(data=data)
    for component in components:
        get_client().compositefoodcomponent.create(
            data=dict(component, composite={"connect": {"id": composite.id}})
        )
    return get_client().compositefood.find_unique(
        where={"id": composite.id}, include=_COMPOSITE_WITH_COMPONENTS
    )


# -- miss queue (Chunk 3, §9, I8) --------------------------------------------


def get_dish_category_profile(category: str) -> Optional[Any]:
    """Curated per-100g calorie band for an estimated dish (§7.6.1, Chunk
    5b). `None` means no confident category has a profile yet - the caller
    treats that as "no confident category -> no number," not a crash."""
    return get_client().dishcategoryprofile.find_unique(where={"category": category})


def get_catalog_version() -> int:
    """Global, curator-bumped catalog version (§12.3, §12.7, Chunk 8a). No row
    yet means "at its default" - same posture as
    `DishCategoryProfile.catalogVersion`'s own hardcoded default of 1."""
    row = get_client().catalogversion.find_unique(where={"id": "global"})
    return row.version if row is not None else 1


def bump_catalog_version() -> int:
    """Increment the global catalog version after any catalog edit (seed,
    ingest, hand curation) so the T0/L2 parse caches (§12.7) treat the
    catalog as changed. Returns the new version."""
    client = get_client()
    row = client.catalogversion.find_unique(where={"id": "global"})
    if row is None:
        row = client.catalogversion.create(data={"id": "global", "version": 1})
    else:
        row = client.catalogversion.update(where={"id": "global"}, data={"version": {"increment": 1}})
    return row.version


def file_food_miss(raw_text: str, locale: str = "") -> Any:
    """Upsert on `(rawText, locale)` - a repeat miss increments `occurrences`
    rather than creating a duplicate row. `locale` defaults to `""`, never
    `None`: Postgres unique indexes never consider two NULLs equal, which
    would silently break this dedupe (see the schema comment on
    `FoodMissQueue.locale`)."""
    return get_client().foodmissqueue.upsert(
        where={"rawText_locale": {"rawText": raw_text, "locale": locale}},
        data={
            "create": {"rawText": raw_text, "locale": locale},
            "update": {"occurrences": {"increment": 1}},
        },
    )


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
