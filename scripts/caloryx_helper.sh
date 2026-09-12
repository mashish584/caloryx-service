#!/usr/bin/env bash
# Interactive dev-database helper. Three things a local database needs, all
# in one place:
#
#   1) Reset - `prisma db push --force-reset` (drops and recreates every
#      table), then the two singleton/config row-sets: an active EngineConfig
#      row and the DishCategoryProfile bands. Without the first, plans stop
#      recording the engineConfigId that explains them; without the second, a
#      catalog MISS resolves to nothing instead of a bounded estimate.
#   2) Ingest - load the food catalog (USDA FoodData Central + INDB + Open
#      Food Facts, §8) so chat text resolves to something.
#   3) Drop - remove previously ingested catalog rows for one source (or all
#      of them), for a clean re-import.
#
# Ingest and drop are idempotent / additive-only from the script's own point
# of view: ingest upserts on (source, sourceRef) and skips existing rows;
# drop only ever deletes USDA/INDB/OPEN_FOOD_FACTS rows, never the
# CALORYX_CURATED rows `seed_foods` / `seed_composite_foods` manage.
#
# Bulk ingest datasets are NOT downloaded here - they are large, versioned by
# release date, and two of them need a manual license/export step. Put them
# in $FOOD_DATA_DIR (default ~/Downloads); anything missing is reported and
# skipped rather than failing:
#
#   USDA SR Legacy / FNDDS (Survey) / Foundation, CSV, unzipped or still zipped
#       https://fdc.nal.usda.gov/download-datasets
#   INDB.csv - INDB.xlsx exported to CSV
#       https://www.anuvaad.org.in/indian-nutrient-databank/
#   en.openfoodfacts.org.products.csv.gz
#       https://world.openfoodfacts.org/data
#
# Usage: scripts/caloryx_helper.sh
#        ENGINE_CONFIG_NAME=v2 scripts/caloryx_helper.sh
#        DRY_RUN=1 scripts/caloryx_helper.sh          # ingest: parse+validate, write nothing
#        OFF_MIN_SCANS=5 scripts/caloryx_helper.sh    # ingest: only Open Food Facts products people scan
#        FORCE_INGEST=1 scripts/caloryx_helper.sh     # ingest: re-run the Open Food Facts pass
#        FOOD_DATA_DIR=/data scripts/caloryx_helper.sh
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

NAME="${ENGINE_CONFIG_NAME:-v1}"

# -- food dataset locations ---------------------------------------------------
#
# USDA folder names carry their release date (..._csv_2024-10-31), so they are
# resolved by prefix - newest match wins - rather than pinned here; a new
# release just needs unzipping into $DATA_DIR.
DATA_DIR="${FOOD_DATA_DIR:-$HOME/Downloads}"
USDA_FNDDS_PREFIX="FoodData_Central_survey_food_csv"
USDA_SR_PREFIX="FoodData_Central_sr_legacy_food_csv"
USDA_FOUNDATION_PREFIX="FoodData_Central_foundation_food_csv"
INDB_CSV="${INDB_CSV:-$DATA_DIR/INDB.csv}"
OFF_CSV="${OFF_CSV:-$DATA_DIR/en.openfoodfacts.org.products.csv.gz}"
# 0 imports every Open Food Facts product with usable macros (~2.07M); 5 keeps
# the ~137k that get scanned more than a handful of times. Measured with
# --dry-run against the 2026-09 export.
OFF_MIN_SCANS="${OFF_MIN_SCANS:-0}"
DRY_RUN="${DRY_RUN:-0}"
FORCE_INGEST="${FORCE_INGEST:-0}"

INGEST_ARGS=()
if [ "$DRY_RUN" != "0" ]; then
    INGEST_ARGS+=(--dry-run)
fi
# Bash 3.2 (macOS system bash) treats "${arr[@]}" on an empty array as unbound
# under `set -u`, so every use below goes through the
# ${INGEST_ARGS[@]+"${INGEST_ARGS[@]}"} form - the portable "no extra args".

# Populated by ingest_usda / ingest_indb / ingest_off so action_ingest can
# print a summary of what actually landed vs. what didn't, once at the end
# rather than scattered through the run's output.
INGEST_SUMMARY=()

skip() {
    echo "   skipped: $1 - $2"
    INGEST_SUMMARY+=("skipped   $1 - $2")
}

ingested() {
    local label="$1"
    if [ "$DRY_RUN" != "0" ]; then
        INGEST_SUMMARY+=("dry-run   $label - parsed and validated, nothing written")
    else
        INGEST_SUMMARY+=("ingested  $label")
    fi
}

confirm() {
    local reply
    read -rp "$1 [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]]
}

# Newest unzipped dataset directory for a prefix, unzipping the archive first
# if only that is present. Messages go to stderr so they don't end up in the
# captured path.
usda_dir() {
    local prefix="$1" dir zip
    dir=$(ls -d "$DATA_DIR/$prefix"_*/ 2>/dev/null | sort | tail -1 || true)
    if [ -z "$dir" ]; then
        zip=$(ls "$DATA_DIR/$prefix"_*.zip 2>/dev/null | sort | tail -1 || true)
        if [ -n "$zip" ]; then
            echo "   unzipping $(basename "$zip")" >&2
            unzip -n -q "$zip" -d "$DATA_DIR"
            dir=$(ls -d "$DATA_DIR/$prefix"_*/ 2>/dev/null | sort | tail -1 || true)
        fi
    fi
    printf '%s' "${dir%/}"
}

ingest_usda() {
    local label="$1" prefix="$2" dir
    dir=$(usda_dir "$prefix")
    if [ -z "$dir" ] || [ ! -f "$dir/food.csv" ]; then
        skip "$label" "no ${prefix}_* in $DATA_DIR"
        return 0
    fi
    echo "   $label: $(basename "$dir")"
    python manage.py ingest_foods --source usda --path "$dir" \
        ${INGEST_ARGS[@]+"${INGEST_ARGS[@]}"}
    ingested "$label"
}

ingest_indb() {
    if [ -f "$INDB_CSV" ]; then
        echo "   INDB: $(basename "$INDB_CSV")"
        python manage.py ingest_foods --source indb --path "$INDB_CSV" \
            ${INGEST_ARGS[@]+"${INGEST_ARGS[@]}"}
        ingested "INDB"
    else
        skip "INDB" "$INDB_CSV not found (export INDB.xlsx to CSV)"
    fi
}

ingest_off() {
    if [ ! -f "$OFF_CSV" ]; then
        skip "Open Food Facts" "$OFF_CSV not found"
    elif [ "$FORCE_INGEST" = "0" ] && [ "$DRY_RUN" = "0" ] && [ "$(food_count OPEN_FOOD_FACTS)" != "0" ]; then
        # Every row would be a duplicate, but re-reading ~10GB to discover that
        # takes tens of minutes. FORCE_INGEST=1 re-runs it (e.g. after a new export).
        skip "Open Food Facts" "already imported - FORCE_INGEST=1 to re-run"
    else
        echo "   Open Food Facts: $(basename "$OFF_CSV") (min-scans $OFF_MIN_SCANS)"
        if [ "$OFF_MIN_SCANS" = "0" ]; then
            echo "   ~2.07M products, tens of minutes to hours, and a lot of storage."
            echo "   OFF_MIN_SCANS=5 imports the ~137k that people actually scan."
        fi
        python manage.py ingest_foods --source off --path "$OFF_CSV" \
            --min-scans "$OFF_MIN_SCANS" ${INGEST_ARGS[@]+"${INGEST_ARGS[@]}"}
        ingested "Open Food Facts"
    fi
}

# Rows already in the catalog for one source - used to decide whether the Open
# Food Facts pass can be skipped, and to report what a drop actually removed.
food_count() {
    python manage.py shell -c "
from common.db import get_client
print(get_client().food.count(where={'source': '$1'}))
" 2>/dev/null | tail -1
}

drop_source() {
    local source="$1"
    python manage.py shell -c "
from common.db import get_client
c = get_client()
print('   {}: {} rows removed'.format('$source', c.food.delete_many(where={'source': '$source'})))
"
}

verify_catalog() {
    echo
    echo "== verify"
    # search_foods() runs the raw trigram SQL, so this also proves pg_trgm and
    # Food_name_trgm_idx survived a reset - an exception here means `db push`
    # did not install the extension, and that must be fixed before any ingest.
    python manage.py shell -c "
from common.db import get_client
from meals import repository
c = get_client()
by_source = {s: c.food.count(where={'source': s})
             for s in ('USDA', 'INDB', 'OPEN_FOOD_FACTS', 'CALORYX_CURATED')}
print('dish profiles  ', c.dishcategoryprofile.count())
print('engine configs ', c.engineconfig.count(where={'isActive': True}))
print('foods          ', c.food.count(),
      '(' + ' | '.join('{} {}'.format(k.lower(), v) for k, v in by_source.items()) + ')')
for query in ('rice', 'hummus'):
    names = [f.name for f in repository.search_foods(query, limit=3)]
    print('search({:7})'.format(query), ' | '.join(names) or '(nothing)')
"
}

# -- option 1: reset & seed config/profiles ----------------------------------

action_reset() {
    echo
    echo "This drops and recreates every table (prisma db push --force-reset),"
    echo "then seeds an active EngineConfig row and the DishCategoryProfile bands."
    echo "The food catalog is untouched - use option 2 afterwards to (re)load it."
    if ! confirm "Continue?"; then
        echo "Aborted."
        return
    fi

    prisma db push --force-reset --schema prisma/schema.prisma

    echo
    echo "== config and fallback rows"
    # Engine constants (§10). Exactly one row may be active; --activate
    # deactivates the rest. Without an active row Plan.engineConfigId stays
    # null and a stored plan can no longer be explained after a retune.
    python manage.py engine_config --seed --name "$NAME" --activate
    # Dish category bands (§7.6.1) - the catalog-MISS fallback, not catalog
    # data. An estimated dish has no food relation at all, only a curated
    # profile behind its number; with no rows the item is dropped as unconsumed.
    python manage.py seed_dish_category_profiles

    verify_catalog
}

# -- option 2: ingest food catalog data ---------------------------------------

action_ingest() {
    INGEST_SUMMARY=()
    echo
    echo "Available food data:"
    echo "  All"
    echo "  USDA FNDDS"
    echo "  USDA SR Legacy"
    echo "  USDA Foundation"
    echo "  INDB"
    echo "  Open Food Facts"
    read -rp "Which food data do you want to ingest? " choice

    echo
    echo "== food catalog"
    if [ "$DRY_RUN" != "0" ]; then
        echo "   DRY_RUN - parsing and validating only, nothing is written"
    fi

    shopt -s nocasematch
    case "$choice" in
        All)
            # Generic sources first. They are small (~13k USDA, ~2k INDB) and
            # they are what plain chat text ("rice", "2 eggs") resolves to;
            # Open Food Facts is branded data and goes last because it is by
            # far the longest pass.
            ingest_usda "USDA FNDDS (Survey)" "$USDA_FNDDS_PREFIX"
            ingest_usda "USDA SR Legacy" "$USDA_SR_PREFIX"
            ingest_usda "USDA Foundation" "$USDA_FOUNDATION_PREFIX"
            ingest_indb
            ingest_off
            ;;
        "USDA FNDDS") ingest_usda "USDA FNDDS (Survey)" "$USDA_FNDDS_PREFIX" ;;
        "USDA SR Legacy") ingest_usda "USDA SR Legacy" "$USDA_SR_PREFIX" ;;
        "USDA Foundation") ingest_usda "USDA Foundation" "$USDA_FOUNDATION_PREFIX" ;;
        INDB) ingest_indb ;;
        "Open Food Facts") ingest_off ;;
        *)
            shopt -u nocasematch
            echo "Unrecognized choice: $choice" >&2
            return 1
            ;;
    esac
    shopt -u nocasematch

    echo
    echo "== ingest summary"
    for line in "${INGEST_SUMMARY[@]}"; do
        echo "   $line"
    done

    verify_catalog
}

# -- option 3: drop food catalog data -----------------------------------------

action_drop() {
    echo
    echo "Available food categories:"
    echo "  All"
    echo "  USDA"
    echo "  INDB"
    echo "  Open Food Facts"
    read -rp "Which food category data do you want to drop? " choice

    local sources=()
    shopt -s nocasematch
    case "$choice" in
        All) sources=(USDA INDB OPEN_FOOD_FACTS) ;;
        USDA) sources=(USDA) ;;
        INDB) sources=(INDB) ;;
        "Open Food Facts") sources=(OPEN_FOOD_FACTS) ;;
        *)
            shopt -u nocasematch
            echo "Unrecognized choice: $choice" >&2
            return 1
            ;;
    esac
    shopt -u nocasematch

    echo
    echo "This permanently deletes Food rows for: ${sources[*]}"
    echo "CALORYX_CURATED rows (seed_foods / seed_composite_foods) are untouched."
    if ! confirm "Continue?"; then
        echo "Aborted."
        return
    fi

    echo
    echo "== dropping food catalog data"
    for source in "${sources[@]}"; do
        drop_source "$source"
    done

    verify_catalog
}

# -- menu ----------------------------------------------------------------------

echo "== CaloryX dev helper =="
echo "1) Reset DB & seed initial engine config + food profiles"
echo "2) Ingest food catalog data"
echo "3) Drop food catalog data"
read -rp "Select an option [1-3]: " option

case "$option" in
    1) action_reset ;;
    2) action_ingest ;;
    3) action_drop ;;
    *)
        echo "Unrecognized option: $option" >&2
        exit 1
        ;;
esac
