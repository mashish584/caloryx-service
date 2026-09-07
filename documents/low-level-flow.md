# CaloryX Backend — Low-Level Flow

Call-by-call detail: how a token is routed, what each endpoint does in order,
how the engine turns a profile into a plan, and where every failure lands. The
architectural overview lives in [`high-level-flow.md`](./high-level-flow.md).

Section references (§) point into *CaloryX — Onboarding Feature PRD v1.1*.

---

## 1. Middleware and DRF pipeline

Every `/api/v1` request passes through the same chain before a view body runs.

```mermaid
flowchart TD
    IN(["HTTP request"]) --> SEC["SecurityMiddleware"]
    SEC --> CORS["CorsMiddleware<br/>CORS_ALLOWED_ORIGINS"]
    CORS --> RID["RequestIdMiddleware<br/>reuse X-Request-Id or mint a uuid4<br/>bind to a contextvar"]
    RID --> COMMON["CommonMiddleware"]
    COMMON --> RESOLVE["URL resolution — caloryx/urls.py"]

    RESOLVE --> AUTHN["BearerAuthentication.authenticate"]
    AUTHN --> PERMS["Permission classes"]
    PERMS --> THROTTLE["GuestCreationThrottle<br/>only when throttle_scope_guest_create"]
    THROTTLE --> PARSE["JSONParser → serializer.is_valid"]
    PARSE --> BODY["View method"]
    BODY --> RENDER["JSONRenderer"]
    RENDER --> OUT(["Response + X-Request-Id"])

    AUTHN -.->|"AuthenticationFailed"| HANDLER
    PERMS -.->|"PermissionDenied"| HANDLER
    THROTTLE -.->|"Throttled"| HANDLER
    PARSE -.->|"ValidationError"| HANDLER
    BODY -.->|"DomainError / unhandled"| HANDLER

    HANDLER["common.exceptions.api_exception_handler"] --> ENVELOPE["error envelope + requestId"]
    ENVELOPE --> OUT
```

The request id is bound to a `contextvars.ContextVar`, so `RequestIdLogFilter`
stamps it onto every log line and `api_exception_handler` stamps it onto every
error body. One mobile trace is followable end to end.

**Route-level auth overrides.** The defaults are `BearerAuthentication` +
`IsAuthenticatedActor`. Three routes opt out — `POST /auth/guest` and
`GET /onboarding/config` set `authentication_classes = []` with `AllowAny`, and
the schema/docs views do the same so CI can fetch the OpenAPI document without a
token. `POST /auth/claim` tightens instead, to `IsRegisteredActor`.

---

## 2. Bearer token routing

One header, two token families. The signing algorithm routes it, so the client
never declares which kind it holds (§8).

```mermaid
flowchart TD
    H["Authorization header"] --> EMPTY{"present?"}
    EMPTY -->|no| ANON["return None — anonymous"]
    ANON --> PERM{"permission class"}
    PERM -->|"IsAuthenticatedActor"| E401(["401 · Bearer realm=caloryx"])

    EMPTY -->|yes| SHAPE{"exactly<br/>'Bearer &lt;token&gt;'?"}
    SHAPE -->|no| E401
    SHAPE -->|yes| ALG["token_algorithm — read the unverified alg header"]

    ALG -->|HS256| G1["verify_guest_token<br/>signature · exp · iss · typ=guest"]
    ALG -->|"RS256 or unreadable"| C1["clerk.verify_token"]

    G1 -->|invalid| E401
    G1 -->|ok| G2["repository.get_user by sub"]
    G2 -->|missing| E401
    G2 --> G3{"claimedAt set?"}
    G3 -->|yes| E401C(["401 — sign in to continue<br/>the data now lives on an account"])
    G3 -->|no| GACT["Actor · provider=GUEST · is_guest=True"]

    C1 --> C2["PyJWKClient.get_signing_key_from_jwt<br/>JWKS cached CLERK_JWKS_CACHE_SECONDS"]
    C2 -->|"JWKS unreachable"| E401
    C2 --> C3["jwt.decode — RS256 · iss · optional aud ·<br/>leeway CLERK_LEEWAY_SECONDS · require exp, sub"]
    C3 -->|"PyJWTError"| E401
    C3 -->|"CLERK_JWKS_URL unset"| E401M(["401 — temporarily unavailable<br/>logged as a deployment error"])
    C3 --> C4["upsert_clerk_user — keyed on clerkUserId<br/>refresh lastSeenAt, email, externalProvider"]
    C4 --> CACT["Actor · provider=CLERK · clerk_user_id · email"]
```

**Why `Actor` and not Django's `User`.** Prisma owns persistence and `DATABASES`
is empty, so `request.user` is a frozen `Actor` dataclass carrying everything a
view needs to authorise without a second round trip. `UNAUTHENTICATED_USER` is
set to `None` so Django's `AnonymousUser` never drags in the auth app's tables.

**A claimed guest token returns 401 by design.** That is the client's signal to
fall back to the Clerk session, not an error to retry.

---

## 3. `POST /auth/guest`

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant T as GuestCreationThrottle
    participant V as GuestSessionView
    participant R as authx.repository
    participant D as PostgreSQL

    C->>T: POST /api/v1/auth/guest — no token
    alt over GUEST_CREATE_RATE
        T-->>C: 429 envelope
    else within budget
        T->>V: allow
        V->>R: create_guest_user
        R->>D: user.create — isGuest=true, authProvider=GUEST
        D-->>R: User row
        R-->>V: user
        V->>V: issue_guest_token — HS256 over SECRET_KEY<br/>sub, typ=guest, iss, iat, exp = now + TTL days
        V-->>C: 201 — token, expiresAt, user,<br/>onboarding {false, false, null}
    end
```

The guest row holds no personal data until the user claims it. The token asserts
nothing beyond "this device owns anonymous user X" — which is exactly why
`common/checks.py` fails `manage.py check` on the built-in development
`SECRET_KEY`: it is what stands between an attacker and a forged guest session.

Guest creation is the one unauthenticated write in the service, so it is the one
endpoint throttled out of the box. `GuestCreationThrottle` applies per view via
the `throttle_scope_guest_create` flag, not globally.

---

## 4. `GET`/`POST /auth/session`

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant A as BearerAuthentication
    participant V as SessionView
    participant OR as onboarding.repository
    participant D as PostgreSQL

    C->>A: GET or POST /api/v1/auth/session
    A->>D: guest lookup, or Clerk upsert
    A-->>V: Actor
    V->>OR: get_profile — include plan
    OR->>D: profile.find_unique where userId
    D-->>OR: Profile + Plan, or null
    OR-->>V: profile
    V-->>C: 200 — user {id, isGuest, authProvider, email},<br/>onboarding {hasProfile, hasPlan, onboardedAt}
```

`GET` and `POST` share one body. The distinction is intent, not behaviour: `POST`
is the sign-in hand-off — the first authenticated call is what creates or
refreshes the Clerk-backed row inside `authenticate` — while `GET` is the cheap
resume check on launch.

The three `onboarding` booleans are the whole of the resume contract (§4):

| `hasProfile` | `hasPlan` | `onboardedAt` | Client resumes at |
|---|---|---|---|
| `false` | `false` | `null` | Step 1 — collect inputs |
| `true` | `false` | `null` | Plan generation |
| `true` | `true` | `null` | Plan screen |
| `true` | `true` | set | App home |

---

## 5. `POST /onboarding/profile`

```mermaid
flowchart TD
    REQ["POST body — sexAtBirth, dateOfBirth,<br/>weightKg, heightCm, targetWeightKg,<br/>activityLevel, preferredUnits"]
    REQ --> FIELD["ProfileUpsertSerializer field validation"]

    FIELD --> B1{"sexAtBirth in<br/>MALE, FEMALE?"}
    B1 -->|no| V400(["400 validation_error<br/>UNSPECIFIED is never accepted"])
    B1 --> B2{"weightKg 20-500 ·<br/>heightCm 50-272 ·<br/>targetWeightKg 20-500?"}
    B2 -->|no| V400
    B2 --> B3{"dateOfBirth<br/>in the future?"}
    B3 -->|yes| V400
    B3 --> AGE["age_from_dob — whole years,<br/>month/day tuple comparison"]

    AGE --> B4{"age &lt; MINIMUM_AGE_YEARS?"}
    B4 -->|yes| E422(["422 age_below_minimum<br/>details: minimumAge, age"])
    B4 --> B5{"age &gt; MAXIMUM_AGE_YEARS?"}
    B5 -->|yes| V400
    B5 --> OK["validated_data<br/>+ transitional age column"]

    OK --> SVC["services.save_profile"]
    SVC --> CFG["repository.get_active_engine_config"]
    CFG --> GOAL["config.goal_for weightKg, targetWeightKg"]
    GOAL --> FLAT["flatten preferredUnits → weightUnit + heightUnit"]
    FLAT --> DATE["to_prisma_date — date → UTC-midnight datetime"]
    DATE --> UP["repository.upsert_profile<br/>where userId · create or update"]
    UP --> ADVS["engine.evaluate_profile"]
    ADVS --> RESP(["200 — profile + advisories"])
```

**Four details worth carrying in your head:**

- **`goal` is not a request field.** It is derived in the service layer, not the
  serializer, because the `MAINTAIN` band is server-tunable config and the
  serializers stay clear of the repository. `get_active_engine_config` is cached
  and falls back to compiled defaults, so this cannot cost a profile save.
- **`preferredUnits` is nested on the wire and two columns in the database.**
  Flattening happens in the service so `upsert_profile` stays a blind
  pass-through into Prisma.
- **`to_prisma_date` is load-bearing.** Prisma's argument serializer knows
  `datetime` but not `date`; a bare `date` used to crash the endpoint with a
  500. Both legs are pinned to UTC — a birth date is a calendar fact, and
  converting through a local zone shifts the day (and, on a birthday, the derived
  age) for anyone west of UTC.
- **The upsert is idempotent.** A user stepping back and forth through the flow
  (§4) never creates a second profile.

### Goal derivation (§5.3)

```mermaid
flowchart LR
    D["delta = targetWeightKg - weightKg"] --> Q{"abs delta &lt;=<br/>maintainBandKg?"}
    Q -->|yes| M["MAINTAIN — adjustment 0"]
    Q -->|no| S{"delta &lt; 0?"}
    S -->|yes| L["LOSE — loseAdjustmentKcal"]
    S -->|no| G["GAIN — gainAdjustmentKcal"]
```

The band matters in both directions: someone holding steady should not have to
type their weight to the decimal, and a difference the size of a rounding error
must not silently buy a 400 kcal deficit.

---

## 6. `POST /onboarding/plan` — the engine pipeline

```mermaid
sequenceDiagram
    autonumber
    participant V as PlanView
    participant S as services.generate_plan
    participant R as onboarding.repository
    participant E as engine.calculate_plan
    participant D as PostgreSQL

    V->>S: user_id
    S->>R: get_profile — include plan
    R->>D: profile.find_unique
    alt no profile
        S-->>V: ProfileRequiredError → 409 profile_required
    else
        S->>R: get_active_engine_config
        R-->>S: EngineConfig — cached or defaults
        S->>S: _to_plan_input — age derived from dateOfBirth
        S->>E: calculate_plan input, config
        E-->>S: PlanResult — pure, no I/O
        S->>R: upsert_plan profile.id, result
        R->>D: plan.upsert where profileId<br/>+ connect engineConfigId
        D-->>R: Plan row
        S->>S: plan_advisories — profile hints + clamp note
        S-->>V: to_response + computedAt from the row + advisories
    end
```

`computedAt` is read back from the persisted row rather than a fresh `now()`, so
a client caching the POST payload ages it against the same instant the database
holds — and the POST and GET shapes stay identical field for field.

### Inside `calculate_plan` (§6.1–§6.3)

```mermaid
flowchart TD
    IN["PlanInput — sex, age, weightKg,<br/>heightCm, goal, activityLevel"] --> EST{"sex ==<br/>UNSPECIFIED?"}
    EST -->|yes| MARK["isEstimate = true<br/>note: unspecified_body_basis_fallback"]
    EST -->|no| BMR
    MARK --> BMR

    BMR["BMR — Mifflin-St Jeor<br/>10·kg + 6.25·cm - 5·age + K<br/>K: MALE +5 · FEMALE -161 · UNSPECIFIED -78"]
    BMR --> TDEE["TDEE = BMR × multiplier_for activityLevel<br/>1.2 · 1.375 · 1.55 · 1.725 · 1.9"]
    TDEE --> ADJ["requested = adjustment_for goal<br/>LOSE -400 · MAINTAIN 0 · GAIN +400"]
    ADJ --> RAW["raw_target = TDEE + requested"]

    RAW --> FLOORQ{"raw_target &lt;<br/>floor_for sex?"}
    FLOORQ -->|"yes — clamped"| CL["target = floor<br/>note: clamped_to_safety_floor"]
    FLOORQ -->|no| NC["target = raw_target"]

    CL --> ROUND
    NC --> ROUND
    ROUND["round_to_nearest target,<br/>targetRoundingKcal — half away from zero"]

    ROUND --> RAT{"clamped?"}
    RAT -->|yes| R1["adjustmentKcal = round target - TDEE<br/>never advertise a deficit<br/>larger than delivered"]
    RAT -->|no| R2["adjustmentKcal = requested<br/>rounding must not read '-396 kcal'"]

    R1 --> WEEK
    R2 --> WEEK
    WEEK["weeklyChangeKg = round_half_up<br/>adjustmentKcal × 7 / kcalPerKgBodyMass, 1"]
    WEEK --> MACRO["_split_macros"]
    MACRO --> REC{"abs macro energy - target<br/>&lt;= 4 kcal?"}
    REC -->|no| ERR(["PlanReconciliationError — a code defect,<br/>surfaces as 500"])
    REC -->|yes| OUT(["PlanResult"])
```

### Macro split (§6.3)

Macros are derived *from* the calorie target, which is what makes them
self-reconcile.

```mermaid
flowchart TD
    T["target_kcal, weight_kg, config"] --> P["proteinG = round proteinPerKg × weightKg"]
    P --> F["fatG = round fatPct × target / 9"]
    F --> REM["remainder = target - proteinG×4 - fatG×9"]
    REM --> C["carbsG = round remainder / 4"]
    C --> NEG{"carbsG &lt; 0?"}
    NEG -->|"yes — heavy user on a clamped target"| RB["carbsG = 0 · hold fat at its share ·<br/>protein takes the rest"]
    NEG -->|no| FIB
    RB --> FIB
    FIB["fiberG = round fiberGPer1000Kcal × target / 1000"]
    FIB --> DONE["Macros — fiber informational only,<br/>never in the energy total"]
```

Fiber is deliberately absent from `macroEnergyKcal`: it is a subset of
carbohydrate grams and must never reach an energy ring (§5.5).

### Worked example (§6.5)

> Male · 30 yrs · 180 cm · 90 kg · Sedentary · Lose
>
> BMR `10(90) + 6.25(180) − 5(30) + 5` = **1,880**
> TDEE `1,880 × 1.2` = **2,256**
> Raw target `2,256 − 400` = 1,856 → rounded to 10 → **1,860 kcal**
> Protein `1.8 × 90` = 162 g · Fat `0.25 × 1860 / 9` = 52 g ·
> Carbs `(1860 − 648 − 468) / 4` = 186 g · Fiber `15 × 1.86` = 28 g
> Reconcile `648 + 744 + 468` = **1,860 ✓**

### Rounding

`engine/rounding.py` is half-**up**, not Python's default half-to-even, because
the client renders an optimistic preview that must reconcile with the server
value (§5.5) and JavaScript's `Math.round` is half-up. `round(2.5)` is `2` in
Python and `3` in JS; standardising on half-away-from-zero removes the
disagreement on boundary cases.

---

## 7. `GET /onboarding/plan` — resume, with self-healing

```mermaid
flowchart TD
    G["GET /onboarding/plan"] --> P["repository.get_profile"]
    P -->|null| C409(["409 profile_required"])
    P --> PL{"profile.plan?"}
    PL -->|null| N404(["404 plan_not_found<br/>raised, not assembled inline,<br/>so it carries a requestId"])
    PL --> SER["serialize_stored_plan — same shape as POST"]

    SER --> HEAL{"safetyFloorKcal or<br/>requestedAdjustmentKcal null?"}
    HEAL -->|"yes — row predates the columns"| DERIVE["derive_stored_rationale<br/>from today's active config"]
    HEAL -->|no| ADV
    DERIVE --> ADV["plan_advisories — profile hints,<br/>plus the clamp note if clamped"]
    ADV --> R200(["200 — identical shape to POST"])
```

Two deliberate choices here:

- **One serializer for POST and GET.** They were separate once and drifted
  twice — the stored form first lost the §6.2 clamp fields, costing a resumed
  plan its explanation, then lagged behind on `computedAt`.
- **Healing is a stopgap, not the fix.** `manage.py backfill_plan_rationale` is
  what actually clears those nulls; healing here keeps the response well-formed
  in the meantime, at the cost of using today's config rather than the one that
  produced the plan.

Similarly, `plan_advisories` is shared between the compute and resume paths.
They used to assemble the list separately, and the resume path forgot the clamp
advisory — so a user held at the floor saw the explanation once and never again.

---

## 8. `POST /onboarding/complete`

```mermaid
flowchart LR
    REQ["POST /onboarding/complete"] --> P{"profile?"}
    P -->|no| E1(["409 profile_required"])
    P -->|yes| PL{"plan?"}
    PL -->|no| E2(["409 plan_required"])
    PL -->|yes| M["repository.mark_onboarded<br/>set onboardedAt = now"]
    M --> OK(["200 — profile"])
```

Both failures are `409`; they differ only by `code`, and they route to different
screens. Branch on `code`, never on the status. "You're all set" should never be
reachable without a plan — a missing one here means the client skipped a step.

---

## 9. `POST /auth/claim`

```mermaid
sequenceDiagram
    autonumber
    participant C as Client — signed in
    participant V as ClaimGuestView
    participant R as authx.repository
    participant D as PostgreSQL

    C->>V: POST /auth/claim {guestToken} + Clerk Bearer
    Note over V: IsRegisteredActor — a guest cannot claim
    V->>V: verify_guest_token
    alt token does not verify
        V-->>C: 400 invalid_guest_token
    end
    V->>R: claim_guest guest_user_id, actor.user_id
    R->>D: find guest + profile
    alt guest missing
        R-->>C: 404 guest_not_found
    else not a guest row
        R-->>C: 409 not_a_guest_session
    else claimedAt already set
        R-->>C: 409 guest_already_claimed
    end
    R->>D: find target + profile
    alt target missing
        R-->>C: 404 user_not_found
    else both sides have a profile
        R-->>C: 409 claim_conflict<br/>details: guestProfileId, existingProfileId
    end
    R->>D: BEGIN
    R->>D: profile.update — reconnect to the target user
    R->>D: user.update guest — claimedAt = now
    R->>D: user.update target — claimedFrom = guest id
    R->>D: COMMIT
    D-->>R: claimed user + profile + plan
    R-->>C: 200 — user + onboarding state
```

All three writes are one transaction: a half-applied claim would leave a profile
pointing at an account whose `claimedFrom` was never set. The conflict case
**refuses rather than merges** — two real profiles is a product decision, not
something to resolve silently.

From the next request onward the guest token returns `401`, which is the
client's cue to use the Clerk session.

---

## 10. `GET /onboarding/config` and cache behaviour

```mermaid
flowchart TD
    REQ["GET /onboarding/config — no auth"] --> GET["get_active_engine_config"]

    GET --> HIT{"cached and<br/>now &lt; expires_at?"}
    HIT -->|yes| USE["cached EngineConfig"]
    HIT -->|no| LOCK["acquire _config_lock"]
    LOCK --> DBL{"another thread<br/>just filled it?"}
    DBL -->|yes| USE
    DBL -->|no| Q["engineconfig.find_first where isActive"]

    Q -->|"row found"| FROM["config_from_row —<br/>null columns fall back per field"]
    Q -->|"no active row"| DEF["DEFAULT_CONFIG"]
    Q -->|"any exception"| WARN["log a warning → DEFAULT_CONFIG<br/>never block a plan on config I/O"]

    FROM --> STORE["cache for 60s"]
    DEF --> STORE
    WARN --> STORE
    STORE --> USE

    USE --> PUB["to_public_dict validation_bounds"]
    PUB --> RESP(["200 — adjustments, maintainBandKg,<br/>multipliers, macros, floors, rounding,<br/>validation bounds"])
```

**Why the bounds travel with the config.** `validation_bounds()` reads the same
constants the serializers enforce and the age limits straight from settings, so
the client can stop a bad value at the field instead of discovering it as a 400
four screens later. They are injected into `to_public_dict` rather than read
inside it, because `engine/` imports neither Django nor the serializers.
`tests/test_config_bounds.py` fails if what is advertised stops matching what is
enforced.

**Why `deepcopy`.** The bounds are nested one level and the payload may well be
cached; a shallow copy would leave the caller holding a handle into it.

`invalidate_engine_config_cache()` is called by `manage.py engine_config` so a
retune in the same process is visible immediately rather than up to a TTL later.

---

## 11. Advisories (§9)

Non-blocking by construction: every one of these rides alongside a `200`.

```mermaid
flowchart TD
    EV["evaluate_profile weightKg, heightCm, targetWeightKg"] --> W{"35 &lt;= weightKg &lt;= 250?"}
    W -->|no| A1["weight_out_of_typical_range · warning · weightKg"]
    W --> H{"130 &lt;= heightCm &lt;= 220?"}
    A1 --> H
    H -->|no| A2["height_out_of_typical_range · warning · heightCm"]
    H --> B{"BMI of target &lt; 18.5?"}
    A2 --> B
    B -->|yes| A3["target_weight_below_healthy_bmi · warning · targetWeightKg"]
    B --> LIST["advisory list"]
    A3 --> LIST

    LIST --> PLANQ{"plan path and clamped?"}
    PLANQ -->|yes| A4["calories_clamped_to_floor · info<br/>inserted first"]
    PLANQ -->|no| OUT
    A4 --> OUT(["returned with the 200"])
```

**Soft versus hard bounds.** The soft ranges above only warn; the hard
physiological caps in `onboarding/serializers.py` (`weightKg` 20–500,
`heightCm` 50–272) reject with a `400`. The safety floor protects the daily
number; the BMI safeguard protects the goal itself.

**The `options` field.** An advisory may carry one-tap `{id, label, patch}`
options so the server never silently auto-corrects. None does today —
`goal_target_weight_conflict` was the only one that ever did, and deriving the
goal from the target weight made it impossible. The shape stays in the contract
so the next advisory that needs it is not a breaking type change for generated
clients, and `tests/test_advisories.py` enforces that every `patch` key is a real
`ProfileUpsertSerializer` field.

---

## 12. Error handling

```mermaid
flowchart TD
    EXC["exception raised"] --> H["api_exception_handler"]
    H --> D{"DomainError subclass?"}
    D -->|yes| DE["error_body code, message, details, requestId<br/>at exc.status_code"]
    D -->|no| DRF["rest_framework exception_handler"]

    DRF -->|"returns None"| UN["log exception →<br/>500 internal_error"]
    DRF --> SHAPE{"data == {detail: ...}<br/>single key?"}
    SHAPE -->|yes| SD["code from detail.code, or 'error'"]
    SHAPE -->|no| FD["validation_error ·<br/>per-field errors kept under details"]

    DE --> OUT(["{ error: { code, message, details, requestId } }"])
    UN --> OUT
    SD --> OUT
    FD --> OUT
```

| Exception | Code | Status |
|---|---|---|
| `AgeBelowMinimumError` | `age_below_minimum` | 422 |
| `ProfileRequiredError` | `profile_required` / `plan_required` | 409 |
| `ConflictError` | `claim_conflict`, `not_a_guest_session`, `guest_already_claimed` | 409 |
| `NotFoundError` | `plan_not_found`, `guest_not_found`, `user_not_found` | 404 |
| `DomainError` | `invalid_guest_token` | 400 |
| DRF `ValidationError` | `validation_error` | 400 |
| DRF auth failure | derived from `detail.code` | 401 / 403 |
| DRF `Throttled` | derived from `detail.code` | 429 |
| `UpstreamUnavailableError` | `upstream_unavailable` | 503 |
| anything unhandled | `internal_error` | 500 |

`api_exception_handler` imports DRF's handler inside the function body, not at
module scope: `rest_framework.views` resolves `DEFAULT_AUTHENTICATION_CLASSES` on
import, which imports `authx`, which imports this module.

---

## 13. Prisma client lifecycle

```mermaid
flowchart TD
    CALL["get_client()"] --> LIVE{"_client set and<br/>is_connected?"}
    LIVE -->|yes| RET["return it"]
    LIVE -->|no| L["acquire _lock"]
    L --> IMP{"_client is None?"}
    IMP -->|yes| GEN["import prisma"]
    GEN -->|ImportError / RuntimeError| ERR(["PrismaClientUnavailable —<br/>run `prisma generate`"])
    GEN --> NEW["Prisma auto_register=True"]
    IMP -->|no| CONN
    NEW --> CONN{"connected?"}
    CONN -->|no| DO["connect timeout=<br/>PRISMA_CONNECT_TIMEOUT_SECONDS"]
    DO --> RET
    CONN -->|yes| RET

    EXIT["process exit"] --> AT["atexit → disconnect"]
    PROBE["GET /readyz"] --> PING["ping → query_raw SELECT 1"]
    PING -->|ok| OK200(["200 status ok, database ok"])
    PING -->|raises| D503(["503 status degraded — the probe<br/>reports, it never raises"])
```

One process-wide **sync** client, matching Django's WSGI request model. The
underlying query engine handles its own connection pool, so a second client
would just multiply pools. `/healthz` deliberately does not touch the database —
liveness is "the process is up", readiness is "it can serve traffic".

### Date adaptation across the Prisma boundary

```mermaid
flowchart LR
    DRF["DRF DateField → datetime.date"] -->|to_prisma_date| PDT["datetime at UTC midnight"]
    PDT --> COL[("Profile.dateOfBirth @db.Date")]
    COL -->|"read back as datetime"| BACK["from_prisma_date → datetime.date"]
    BACK --> AGE["age_from_dob"]
    BACK --> SER["serialize_profile → YYYY-MM-DD"]
```

The generated client types even a `@db.Date` column as `Optional[datetime]` on
both input and output, so dates cross this boundary as datetimes in both
directions. Both legs are pinned to UTC and neither ever touches local time.

---

## 14. Startup checks and migrations

```mermaid
flowchart TD
    BOOT["manage.py check / runserver"] --> C1{"SECRET_KEY is the<br/>dev default?"}
    C1 -->|yes| E1["caloryx.E001 — Error<br/>Warning only when DEBUG"]
    C1 --> C2{"len(SECRET_KEY) &lt; 32 bytes?"}
    C2 -->|yes| W1["caloryx.W001 — Warning<br/>RFC 7518 §3.2 minimum for HS256"]
    C2 --> C3{"CLERK_JWKS_URL set?"}
    C3 -->|no| E2["caloryx.E002 — Error<br/>Warning when DEBUG · guest mode still works"]
    C3 --> C4{"DATABASE_URL set?"}
    C4 -->|no| E3["caloryx.E003 — Error"]
    C4 --> RUN(["serve"])
```

### Transitional columns

Three columns are mid-migration. Each has an expand-migrate-contract backfill
that must run before the old column is dropped.

```mermaid
flowchart LR
    A["Profile.preferredUnits<br/>METRIC | IMPERIAL"] -->|backfill_unit_preferences| A2["weightUnit + heightUnit<br/>kg + ft/in is now expressible"]
    B["Profile.age — Int"] -->|backfill_date_of_birth| B2["dateOfBirth<br/>age derived on every read"]
    C["Plan — null rationale columns"] -->|backfill_plan_rationale| C2["safetyFloorKcal +<br/>requestedAdjustmentKcal"]
```

Run each with `--dry-run` first. `age` and `preferredUnits` are then droppable in
a single contract-phase push.

---

## 15. Testing seams

The suite needs no database and no generated Prisma client. Three seams make
that work:

```mermaid
flowchart TD
    E["engine/ — pure functions"] -->|"direct calls, no fixtures"| T1["test_engine · test_rounding ·<br/>test_advisories · test_goal_derivation"]
    R["repository modules — the only Prisma callers"] -->|"patched at the module boundary"| T2["test_services · test_serializers ·<br/>test_prisma_dates"]
    S["serializers + drf-spectacular schema"] -->|"shape assertions"| T3["test_api_contract ·<br/>test_error_contract · test_config_bounds"]
```

Because every Prisma call funnels through a repository module, patching that
module is enough to exercise the full service and view layers. `test_prisma_dates`
exists specifically because the read side once drifted on the coincidence that
`datetime` subclasses `date`.
