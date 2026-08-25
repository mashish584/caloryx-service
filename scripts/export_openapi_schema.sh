#!/usr/bin/env bash
# Exports the OpenAPI schema that the frontend generates TypeScript types
# from. Uses tests.settings so this needs no real DATABASE_URL/Clerk config -
# schema generation never touches the database.
#
# Usage: scripts/export_openapi_schema.sh [output-path]
#
# Frontend consumption (openapi-typescript):
#   npx openapi-typescript schema.yaml -o src/api/schema.d.ts
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

OUT="${1:-schema.yaml}"

DJANGO_SETTINGS_MODULE=tests.settings python manage.py spectacular \
    --file "$OUT" \
    --validate \
    --fail-on-warn

echo "Wrote $OUT"
