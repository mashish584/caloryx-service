# CaloryX Backend — Documentation

Flow diagrams for `caloryx-service`, the backend behind the CaloryX onboarding
flow. Diagrams are Mermaid, which GitHub renders inline; nothing here needs a
build step or an external tool.

| Document | What it covers |
|---|---|
| [`high-level-flow.md`](./high-level-flow.md) | System context · component map · the onboarding journey · request lifecycle · data model · deployment · endpoint summary |
| [`low-level-flow.md`](./low-level-flow.md) | Middleware and DRF pipeline · bearer token routing · every endpoint call-by-call · the engine pipeline · advisories · error handling · Prisma lifecycle · startup checks · testing seams |

Each document is also available as a print-ready PDF —
[`high-level-flow.pdf`](./high-level-flow.pdf) and
[`low-level-flow.pdf`](./low-level-flow.pdf) — with every diagram rendered at
full size; oversized ones sit on their own correctly proportioned sheet rather
than being shrunk to fit. Regenerate them whenever the Markdown changes.

Start with the high-level document for orientation, then drop into the low-level
one for the endpoint or subsystem you're working on. Section references (§)
point into *CaloryX — Onboarding Feature PRD v1.1*.

## Keeping these current

The diagrams describe behaviour that lives in code, so they go stale the same
way comments do. Refresh them when any of the following change:

- **A route is added or removed** — `caloryx/urls.py`, `authx/urls.py`,
  `onboarding/urls.py` (high-level §7, low-level §3–§10).
- **The engine pipeline changes** — `engine/calculator.py` or the fields on
  `EngineConfig` (low-level §6).
- **Auth routing changes** — `authx/authentication.py`, `authx/clerk.py`,
  `authx/tokens.py` (low-level §2).
- **An error code or status is added** — `common/exceptions.py` and the view
  `@extend_schema` blocks (low-level §12).
- **The Prisma schema changes** — `prisma/schema.prisma` (high-level §5).

The generated OpenAPI document remains the authoritative API contract; these
diagrams explain the *why* and the ordering that a schema cannot:

```bash
scripts/export_openapi_schema.sh schema.yaml
```
