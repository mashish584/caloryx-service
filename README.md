# caloryx-service

Backend for the CaloryX onboarding flow — profile capture, the authoritative
calorie/macro calculation, and plan persistence.

Implements [CaloryX — Onboarding Feature PRD v1.1](../CaloryX-Onboarding-PRD-v1.1.md).
Section references below (§) point into that document.

**Stack:** Django + Django REST Framework · Prisma (prisma-client-py) · PostgreSQL · Clerk

---

## How this differs from the PRD

Two deliberate deviations, both driven by the chosen stack:

**1. Clerk replaces the `/auth/google` and `/auth/apple` endpoints (§8).**
Those were token-exchange endpoints. Clerk performs the OAuth exchange on the
device, so the app sends the resulting Clerk **session JWT** as
`Authorization: Bearer <token>` and this service verifies it against Clerk's
JWKS. No Google or Apple secret ever lives here.

- Apple's private-relay address arrives as an ordinary email claim and is stored
  unchanged. The stable Apple identifier lives inside Clerk and reaches us as
  the Clerk user id, which is the field we key on — so §5.1's requirement is met
  through Clerk rather than directly.
- **Sign in with Apple is still required on iOS** (App Store Guideline 4.8).
  That is a Clerk dashboard + client concern; nothing in this service blocks it,
  but it must not be forgotten at release time.
- Guest mode is not a Clerk concept, so this service mints its own guest tokens.

**2. The calculation engine runs in-process.**
The PRD describes a Node/Prisma API calling a separate Python calc service.
Django *is* Python, so `engine/` is that service, kept behind a hard boundary:
no Django import, no Prisma import, no I/O. It takes a `PlanInput` plus an
`EngineConfig` and returns a `PlanResult`. Extracting it into its own process
later means adding a transport, not rewriting callers.

---

## Layout

```
caloryx/        Django project (settings, urls, wsgi/asgi)
engine/         Calorie & macro engine (§6) — pure, dependency-free, no I/O
  ├ calculator.py   BMR → TDEE → target → macros
  ├ advisories.py   Non-blocking edge-case hints (§9)
  ├ config.py       Server-tunable constants (§10)
  └ rounding.py     Half-up rounding, to match the client preview
authx/          Identity: Clerk verification, guest tokens, claiming
onboarding/     Profile / plan / complete endpoints + persistence
common/         Prisma client lifecycle, error envelope, checks, health probes
prisma/         schema.prisma (§7)
tests/          611 tests, none of which need a database
```

---

## Getting started

Requires **Python 3.11+** and **Node** (the Prisma CLI ships with the Python
package but shells out to Node-based engines).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # then fill in DJANGO_SECRET_KEY and CLERK_ISSUER
```

Generate a secret key:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Start Postgres:

```bash
docker compose up -d postgres
```

Push the schema and generate the Prisma client:

```bash
prisma db push --schema prisma/schema.prisma
```

`db push` is fine for early development. Once the schema stabilises, switch to
migrations so changes are reviewable and replayable:

```bash
prisma migrate dev --name init --schema prisma/schema.prisma
```

If the database already held plans before `Plan.safetyFloorKcal` /
`Plan.requestedAdjustmentKcal` were added, backfill them once. Those rows cannot
explain a §6.2 clamp until you do:

```bash
python manage.py backfill_plan_rationale --dry-run
python manage.py backfill_plan_rationale
```

Likewise, `Profile.preferredUnits` was replaced by `weightUnit` + `heightUnit`
(a single METRIC/IMPERIAL flag cannot express kg + ft/in). The new columns carry
defaults, so a push silently lands existing IMPERIAL users on KG/CM. Backfill
before dropping the old column:

```bash
python manage.py backfill_unit_preferences --dry-run
python manage.py backfill_unit_preferences
```

`Profile.age` was likewise replaced by `dateOfBirth`, from which age is derived
on every read (§9). Old rows only ever stored the integer, so their date has to
be reconstructed — approximately — before `age` can be dropped:

```bash
python manage.py backfill_date_of_birth --dry-run
python manage.py backfill_date_of_birth
```

Both `age` and `preferredUnits` are then droppable in a single contract-phase push.

Verify configuration, then run:

```bash
python manage.py check
```

```bash
python manage.py runserver 0.0.0.0:8000
```

### Tests

```bash
pytest
```

The suite needs no database and no Prisma client — the engine is pure and the
persistence layers are exercised through their repository seams.

---

## API

Base path `/api/v1`. Every endpoint accepts **guest and Clerk sessions alike**
(§8), except `/auth/claim`, which by definition needs a signed-in account.

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/auth/guest` | Create an anonymous session; returns a guest bearer token |
| `GET`/`POST` | `/auth/session` | Resolve the current session + where to resume |
| `POST` | `/auth/claim` | Move a guest's onboarding data onto the signed-in account |
| `GET`/`POST` | `/onboarding/profile` | Upsert / read the collected inputs |
| `POST` | `/onboarding/plan` | Compute + persist the plan |
| `GET` | `/onboarding/plan` | Fetch the stored plan (plan screen, resume) |
| `POST` | `/onboarding/complete` | Stamp `onboardedAt`, finalise |
| `GET` | `/onboarding/config` | Engine constants, so the client preview matches |
| `GET` | `/healthz` · `/readyz` | Liveness / readiness probes |

`POST /onboarding/plan` returns the §8 shape, plus the fields the plan screen
needs to render its rationale honestly:

```json
{
  "calories": 1860,
  "macros": { "proteinG": 162, "carbsG": 186, "fatG": 52, "fiberG": 28 },
  "macroEnergyKcal": { "protein": 648, "carbs": 744, "fat": 468, "total": 1860 },
  "rationale": {
    "adjustmentKcal": -400,
    "weeklyChangeKg": -0.4,
    "clamped": false,
    "safetyFloorKcal": 1500,
    "requestedAdjustmentKcal": -400
  },
  "isEstimate": false,
  "bmr": 1880.0,
  "tdee": 2256.0,
  "advisories": []
}
```

`macroEnergyKcal` has no `fiber` key, deliberately: fiber is a subset of
carbohydrate grams and must never reach an energy ring (§5.5).

### Errors

One envelope for everything, so the client has a single shape to branch on:

```json
{ "error": { "code": "age_below_minimum", "message": "…",
             "details": {}, "requestId": "…" } }
```

Notable codes: `age_below_minimum` (422), `profile_required` (409),
`plan_required` (409), `claim_conflict` (409), `validation_error` (400).

### Advisories

Onboarding speed and completion are core metrics, so §9's edge cases come back
as **non-blocking** advisories rather than errors. Each carries a `code`, a
`message`, a `severity`, and — where the PRD asks for a one-tap correction — the
`options` themselves, so the user chooses and the server never silently
auto-corrects:

| Code | Trigger |
|---|---|
| `goal_target_weight_conflict` | "Lose" with a target ≥ current (or the reverse) — carries Keep / Switch options |
| `target_weight_below_healthy_bmi` | Wellbeing safeguard: target implies BMI < 18.5 |
| `calories_clamped_to_floor` | Target was clamped up to the safety floor |
| `weight_out_of_typical_range` · `height_out_of_typical_range` | Plausible but unusual input |

### API schema & frontend types

The API is documented with [drf-spectacular](https://drf-spectacular.readthedocs.io/),
generated from the DRF serializers rather than hand-maintained, so it can't
drift from what the endpoints actually return.

```
GET /api/schema        raw OpenAPI 3 document (no auth required)
GET /api/schema/docs   Swagger UI
GET /api/schema/redoc  ReDoc
```

To export the schema to a file (used to generate frontend types):

```bash
scripts/export_openapi_schema.sh schema.yaml
```

Then generate TypeScript types on the frontend with
[openapi-typescript](https://openapi-ts.dev/):

```bash
npx openapi-typescript schema.yaml -o src/api/schema.d.ts
```

Re-run both steps whenever a serializer or view response shape changes — the
schema is not committed (`schema.yaml` is gitignored) since it's a build
artifact, not a source file.

---

## The engine (§6)

Mifflin-St Jeor → TDEE → goal adjustment → safety-floor clamp → macros derived
from the target so they always reconcile. Verified against the PRD's worked
example (§6.5) in `tests/test_engine.py`:

> Male · 30 yrs · 180 cm · 90 kg · Sedentary · Lose
> → BMR 1,880 · TDEE 2,256 · **1,860 kcal** · 162 P / 186 C / 52 F / 28 fiber
> → 648 + 744 + 468 = 1,860 ✓

Three details worth knowing:

**Rounding.** The target rounds to the nearest 10 kcal (configurable), which is
what turns 2,256 − 400 = 1,856 into the PRD's 1,860. Rounding is half-**up**
throughout (`engine/rounding.py`), not Python's default half-to-even, so the
client's optimistic preview and the server agree on boundary cases.

**The rationale is derived, never hardcoded.** Normally `adjustmentKcal` is the
configured goal adjustment — the single source of truth per §5.3, so rounding
never makes the screen read "−396 kcal". When the safety floor clamps the
target, it is recomputed from the clamped number instead, so a clamped plan can
never advertise a larger deficit than it actually delivers (§6.2).

**The `UNSPECIFIED` path still exists but is unreachable from onboarding.**
v7 makes the body-basis field a required Male/Female choice, and the API
*rejects* `UNSPECIFIED`. The −78 constant, the 1,500 floor, and `isEstimate` are
retained purely so the engine never throws on imported or malformed records
(§6.4) — and so a decline affordance can be reinstated without engine work,
which §11 leaves open.

### Tuning without an app release (§10)

Multipliers, adjustments, macro ratios, floors and rounding live in the
`EngineConfig` table; compiled defaults apply when no row is active. Reads are
cached for 60s, and a config lookup failure falls back to the defaults rather
than costing a user their plan.

```bash
python manage.py engine_config --show
python manage.py engine_config --seed --name v1 --activate
python manage.py engine_config --name v1 --set loseAdjustmentKcal=-350 --activate
```

Every plan row records the `engineConfigId` that produced it, so a stored plan
stays explainable after a retune.

---

## Notes for the client

- **Send the Clerk session JWT or the guest token** as `Authorization: Bearer …`.
  The signing algorithm routes it (Clerk RS256 / guest HS256); the client never
  declares which kind it holds.
- **A claimed guest token stops working** and returns 401. That is the signal to
  fall back to the Clerk session — not an error to retry.
- **Resume state** comes from `/auth/session`: `hasProfile`, `hasPlan`,
  `onboardedAt` map onto the last incomplete step (§4).
- **Age**: send `dateOfBirth` and let the server derive the age (§9 asks for a
  real date entry). `age` is still accepted for offline-computed submissions;
  when both arrive, the date wins.
- **Fetch `/onboarding/config`** and drive the client preview from it, rather
  than duplicating the constants in the app (§10).

---

## Known gaps

Deliberately not built, and why:

- **Analytics events (§10).** The listed events are client-side and belong in
  the app's analytics SDK. If they should be server-collected instead, that is a
  new endpoint and a decision to make explicitly.
- **Rate limiting** covers only guest-session creation, the one unauthenticated
  write. Everything else sits behind a verified token; add throttles at the edge
  or per-endpoint as traffic warrants.
- **`prisma db push` over migrations.** Fine now, wrong for a second
  environment — switch before anyone else runs this.
- **Unresolved in the PRD itself (§11):** the body-basis decline decision, legal
  sign-off on 18+, and the fiber rule (15 vs 14 g/1,000 kcal). The engine
  implements 15 g and puts all three behind config or a defensive path, so none
  of them requires a rewrite once decided.
