"""Food catalog and logged-meal persistence (Prisma).

The only module in this app that touches `get_client()` - services.py owns the
business logic (unit resolution, yield conversion, totals), this owns reads
and writes. Mirrors onboarding/repository.py.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

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


# -- embeddings (`manage.py backfill_food_embeddings`, §7.2/§7.6, Chunk 9a) ---
#
# `Food.embedding` / `CompositeFood.embedding` are `Unsupported("vector(N)")`
# in the schema, so the generated Prisma client cannot see them at all - no
# `find_many` returns them and no `update` writes them. Everything below is
# therefore raw SQL, the same way `search_foods` reaches pg_trgm's operators.
# A vector crosses this boundary as pgvector's text form ('[0.1,0.2,...]')
# and is cast with `::vector`, which is the documented input format.

# Selects rows that have no vector, or whose vector came from a different
# model than the one being backfilled with. `IS DISTINCT FROM` (not `<>`)
# matters: a NULL embeddingModel must count as stale, and `<>` yields NULL
# there, which WHERE reads as false.
_NEEDS_EMBEDDING_PREDICATE = '(embedding IS NULL OR "embeddingModel" IS DISTINCT FROM $1)'

# An empty $2 means "every source". Otherwise a comma-joined whitelist, split
# server-side - the command validates each value against the FoodSource enum
# before it gets here, and this stays parameterized either way.
_SOURCE_FILTER = "($2 = '' OR source::text = ANY(string_to_array($2, ',')))"

_COUNT_FOODS_SQL = 'SELECT COUNT(*)::int AS n FROM "Food" WHERE {} AND {}'.format(
    _NEEDS_EMBEDDING_PREDICATE, _SOURCE_FILTER
)

# ORDER BY id makes this a stable cursor: rows written by the previous batch
# drop out of the predicate, so re-running with the same LIMIT walks forward
# rather than re-reading. That is the whole resumability mechanism - there is
# no offset to lose track of if the command dies mid-run.
_FETCH_FOODS_SQL = 'SELECT id, name, brand FROM "Food" WHERE {} AND {} ORDER BY id LIMIT $3'.format(
    _NEEDS_EMBEDDING_PREDICATE, _SOURCE_FILTER
)

_COUNT_COMPOSITES_SQL = 'SELECT COUNT(*)::int AS n FROM "CompositeFood" WHERE {}'.format(
    _NEEDS_EMBEDDING_PREDICATE
)
_FETCH_COMPOSITES_SQL = (
    'SELECT id, name, aliases FROM "CompositeFood" WHERE {} ORDER BY id LIMIT $2'.format(
        _NEEDS_EMBEDDING_PREDICATE
    )
)

_SET_EMBEDDING_SQL = (
    'UPDATE "{}" SET embedding = $1::vector, "embeddingModel" = $2, "embeddedAt" = NOW() WHERE id = $3'
)


# ANN retrieval, the semantic half of §7.2's match score. `<=>` is pgvector's
# cosine *distance*, so similarity is `1 - distance` and the ORDER BY is
# ascending - the operator is what the HNSW index (vector_cosine_ops) serves,
# so the expression must stay in this exact form or the index goes unused and
# this silently becomes a sequential scan of the catalog.
#
# `embedding IS NOT NULL` excludes rows the backfill hasn't reached (or that
# were deliberately skipped, e.g. Open Food Facts). Those stay reachable
# through `search_foods`'s trigram window exactly as before.
_SEMANTIC_SEARCH_SQL = """
SELECT id, 1 - (embedding <=> $1::vector) AS similarity
FROM "{}"
WHERE embedding IS NOT NULL
ORDER BY embedding <=> $1::vector
LIMIT $2
"""


def _semantic_search(table: str, vector: List[float], limit: int) -> List[Dict[str, Any]]:
    return get_client().query_raw(
        _SEMANTIC_SEARCH_SQL.format(table), _to_vector_literal(vector), limit
    )


def search_foods_by_embedding(
    vector: List[float], *, limit: int = 25
) -> List[Tuple[Any, float]]:
    """Nearest catalog foods to a query vector, as `(food, similarity)` pairs
    in descending similarity. Similarity is cosine, in [-1, 1] - callers
    compare it against `settings.SEMANTIC_MATCH_FLOOR`, never against the
    lexical thresholds in `chatparser.confidence`, which are a different
    scale entirely."""
    rows = _semantic_search("Food", vector, limit)
    if not rows:
        return []
    similarity = {row["id"]: row["similarity"] for row in rows}
    foods = get_client().food.find_many(
        where={"id": {"in": list(similarity)}}, include=_FOOD_WITH_UNITS
    )
    return sorted(
        ((food, similarity[food.id]) for food in foods), key=lambda pair: pair[1], reverse=True
    )


def search_composites_by_embedding(
    vector: List[float], *, limit: int = 10
) -> List[Tuple[Any, float]]:
    """Same for composite dishes (§7.6). Components are included, because
    every caller expands the dish immediately after matching it."""
    rows = _semantic_search("CompositeFood", vector, limit)
    if not rows:
        return []
    similarity = {row["id"]: row["similarity"] for row in rows}
    composites = get_client().compositefood.find_many(
        where={"id": {"in": list(similarity)}}, include=_COMPOSITE_WITH_COMPONENTS
    )
    return sorted(
        ((c, similarity[c.id]) for c in composites), key=lambda pair: pair[1], reverse=True
    )


def vector_extension_installed() -> bool:
    """Whether `CREATE EXTENSION vector` has actually run on this database.
    `prisma db push` does it via the datasource's `extensions` list, but a
    database restored from elsewhere (or pushed before Chunk 9a) may not have
    it - and every query below fails opaquely without it."""
    rows = get_client().query_raw("SELECT 1 AS ok FROM pg_extension WHERE extname = 'vector'")
    return bool(rows)


def embedding_column_width(table: str) -> Optional[int]:
    """The `vector(N)` width Postgres actually has for `table.embedding`, or
    None if the column doesn't exist. pgvector stores the declared dimension
    in `atttypmod` directly. The schema hardcodes N and `settings
    .EMBEDDING_DIMENSIONS` is set independently, so the backfill compares the
    two here rather than discovering the mismatch as an insert error a few
    thousand paid embeddings later."""
    rows = get_client().query_raw(
        """
        SELECT a.atttypmod AS width
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        WHERE c.relname = $1 AND a.attname = 'embedding' AND NOT a.attisdropped
        """,
        table,
    )
    if not rows:
        return None
    width = rows[0]["width"]
    return width if width and width > 0 else None


def count_foods_needing_embedding(model: str, sources: Optional[List[str]] = None) -> int:
    rows = get_client().query_raw(_COUNT_FOODS_SQL, model, ",".join(sources or []))
    return rows[0]["n"] if rows else 0


def fetch_foods_needing_embedding(
    model: str, sources: Optional[List[str]] = None, *, limit: int = 256
) -> List[Dict[str, Any]]:
    return get_client().query_raw(_FETCH_FOODS_SQL, model, ",".join(sources or []), limit)


def count_composites_needing_embedding(model: str) -> int:
    rows = get_client().query_raw(_COUNT_COMPOSITES_SQL, model)
    return rows[0]["n"] if rows else 0


def fetch_composites_needing_embedding(model: str, *, limit: int = 256) -> List[Dict[str, Any]]:
    return get_client().query_raw(_FETCH_COMPOSITES_SQL, model, limit)


def _to_vector_literal(vector: List[float]) -> str:
    """pgvector's text input form. `repr`-free formatting keeps this stable
    across Python versions and avoids scientific notation, which the parser
    accepts but which makes a stored vector needlessly hard to eyeball."""
    return "[{}]".format(",".join("{:.8f}".format(value) for value in vector))


def _set_embeddings(table: str, updates: List[Tuple[str, List[float]]], model: str) -> int:
    """One transaction per call, not one round trip per row: a backfill batch
    is hundreds of updates and a pooled remote connection makes the round
    trip, not the update, the cost. Returns the number of rows written."""
    if not updates:
        return 0
    sql = _SET_EMBEDDING_SQL.format(table)
    batcher = get_client().batch_()
    for row_id, vector in updates:
        batcher.execute_raw(sql, _to_vector_literal(vector), model, row_id)
    batcher.commit()
    return len(updates)


def set_food_embeddings(updates: List[Tuple[str, List[float]]], model: str) -> int:
    return _set_embeddings("Food", updates, model)


def set_composite_embeddings(updates: List[Tuple[str, List[float]]], model: str) -> int:
    return _set_embeddings("CompositeFood", updates, model)


# HNSW rather than IVFFlat: it needs no training pass over existing data, so
# it stays correct on an empty or half-backfilled column - which is exactly
# the state Chunk 9a leaves the catalog in. Cosine ops to match the
# similarity Chunk 9b scores with; a different operator class here would make
# the index silently unusable for that query.
_VECTOR_INDEXES = {
    "Food_embedding_hnsw_idx": '"Food"',
    "CompositeFood_embedding_hnsw_idx": '"CompositeFood"',
}


def ensure_vector_indexes() -> List[str]:
    """Create the ANN indexes if absent (idempotent). Separate from `db push`
    because Prisma has no Hnsw index type to declare - see the note in
    schema.prisma - which also means a later `db push` can drop these as
    drift. Re-running this is the fix. Returns the index names that exist
    afterwards."""
    client = get_client()
    for index_name, table in _VECTOR_INDEXES.items():
        client.execute_raw(
            'CREATE INDEX IF NOT EXISTS "{}" ON {} USING hnsw (embedding vector_cosine_ops)'.format(
                index_name, table
            )
        )
    rows = client.query_raw(
        "SELECT indexname FROM pg_indexes WHERE indexname = ANY(string_to_array($1, ','))",
        ",".join(_VECTOR_INDEXES),
    )
    return [row["indexname"] for row in rows]


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
