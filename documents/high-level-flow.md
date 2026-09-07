# CaloryX Backend — High-Level Flow

A bird's-eye view of `caloryx-service`: who talks to it, what the pieces are,
and the path a user takes through onboarding. For call-by-call detail — token
routing, the engine pipeline, transaction boundaries — see
[`low-level-flow.md`](./low-level-flow.md).

Section references (§) point into *CaloryX — Onboarding Feature PRD v1.1*.

---

## 1. System context

Clerk performs the OAuth exchange on the device, so no provider secret ever
reaches this service — it only verifies the resulting session JWT against
Clerk's JWKS. Guest mode has no Clerk identity, so the service mints its own
HS256 token instead.

```mermaid
flowchart LR
    subgraph Device["Mobile app / Expo client"]
        UI["Onboarding UI<br/>steps 1-5"]
        SDK["Clerk SDK<br/>Google · Apple"]
    end

    subgraph Service["caloryx-service — Django + DRF"]
        API["REST API<br/>/api/v1"]
        ENG["engine/<br/>pure calculation"]
    end

    CLERK["Clerk<br/>hosted identity"]
    DB[("PostgreSQL<br/>via Prisma")]

    UI -->|"Bearer token · JSON"| API
    SDK -->|"OAuth on device"| CLERK
    SDK -.->|"session JWT"| UI
    API -->|"fetch JWKS · cached 600s"| CLERK
    API --> ENG
    API -->|"Prisma sync client"| DB
    API -.->|"OpenAPI 3 schema"| UI
```

**Direction of trust.** Everything inbound carries a bearer token; the service
never calls the device. The only outbound dependency is Clerk's JWKS endpoint,
and a failure there degrades signed-in auth only — guest sessions keep working.

---

## 2. Component map

```mermaid
flowchart TD
    subgraph caloryx["caloryx/ — Django project"]
        SET["settings.py<br/>DATABASES = {} · Prisma owns persistence"]
        URL["urls.py<br/>route table"]
    end

    subgraph common["common/ — cross-cutting"]
        MW["middleware.py<br/>X-Request-Id correlation"]
        EXC["exceptions.py<br/>one error envelope"]
        DB["db.py<br/>Prisma lifecycle · date adaptation"]
        CHK["checks.py<br/>startup guardrails"]
        HLT["views.py<br/>/healthz · /readyz"]
    end

    subgraph authx["authx/ — identity"]
        AUTH["authentication.py<br/>BearerAuthentication"]
        CLK["clerk.py<br/>RS256 · JWKS"]
        TOK["tokens.py<br/>HS256 guest tokens"]
        PERM["permissions.py"]
        AV["views.py<br/>guest · session · claim"]
        AR["repository.py"]
    end

    subgraph onboarding["onboarding/ — the flow"]
        OV["views.py<br/>profile · plan · complete · config"]
        OSER["serializers.py<br/>hard bounds · age gate"]
        OSVC["services.py<br/>use cases"]
        OR["repository.py<br/>+ 60s config cache"]
    end

    subgraph engine["engine/ — no Django · no Prisma · no I/O"]
        CALC["calculator.py<br/>BMR → TDEE → target → macros"]
        CFG["config.py<br/>EngineConfig · goal_for"]
        ADV["advisories.py<br/>non-blocking hints"]
        RND["rounding.py<br/>half-up"]
    end

    PRISMA[("prisma/schema.prisma<br/>User · Profile · Plan · EngineConfig")]

    URL --> AV
    URL --> OV
    URL --> HLT
    SET -.->|"default auth + permission classes"| AUTH
    MW --> URL
    AUTH --> CLK
    AUTH --> TOK
    AUTH --> AR
    AV --> AR
    AV --> PERM
    OV --> OSER
    OV --> OSVC
    OSVC --> OR
    OSVC --> CALC
    OSVC --> ADV
    CALC --> CFG
    CALC --> RND
    OR --> CFG
    AR --> DB
    OR --> DB
    DB --> PRISMA
    EXC -.->|"handles every failure"| OV
    EXC -.-> AV
    CHK -.->|"manage.py check"| SET
```

**The one hard boundary.** `engine/` imports nothing from Django, Prisma, or the
rest of the service. It receives a `PlanInput` plus an `EngineConfig` and returns
a `PlanResult`. Lifting it into a standalone process later means adding a
transport, not rewriting callers.

---

## 3. The onboarding journey

Every endpoint accepts guest and Clerk sessions alike (§8), so the flow below is
identical whether the user signed in first or started anonymously.

```mermaid
flowchart TD
    START(["App launch"]) --> HAS{"Token stored<br/>on device?"}

    HAS -->|no| CHOICE{"Sign in<br/>or continue<br/>as guest?"}
    HAS -->|yes| SESSION["GET /auth/session<br/>resolve resume point"]

    CHOICE -->|guest| GUEST["POST /auth/guest<br/>mint HS256 token"]
    CHOICE -->|"Google / Apple"| CLERKIN["Clerk sign-in on device"]
    CLERKIN --> POSTSESS["POST /auth/session<br/>upsert Clerk user row"]

    GUEST --> COLLECT
    POSTSESS --> COLLECT
    SESSION --> RESUME{"hasProfile ·<br/>hasPlan ·<br/>onboardedAt"}

    RESUME -->|"no profile"| COLLECT
    RESUME -->|"profile, no plan"| PLANSTEP
    RESUME -->|"plan, not complete"| PLANSCREEN
    RESUME -->|"onboardedAt set"| HOME

    COLLECT["Steps 1-3 — collect inputs<br/>sex · date of birth · weight ·<br/>height · target weight · activity"]
    COLLECT --> PROFILE["POST /onboarding/profile"]
    PROFILE --> GATE{"Age >= minimum?"}
    GATE -->|no| BLOCKED(["422 age_below_minimum<br/>dedicated screen"])
    GATE -->|yes| SAVED["Profile stored ·<br/>goal derived from target weight ·<br/>advisories returned"]

    SAVED --> PLANSTEP["POST /onboarding/plan"]
    PLANSTEP --> PLANSCREEN["Plan screen — calories, macros,<br/>rationale, advisories"]
    PLANSCREEN --> DONE["POST /onboarding/complete<br/>stamp onboardedAt"]
    DONE --> HOME(["App home"])

    PLANSCREEN -.->|"guest signs in later"| CLAIM["POST /auth/claim<br/>move data onto the account"]
    CLAIM --> PLANSCREEN

    CONFIG["GET /onboarding/config"] -.->|"drives the optimistic preview"| COLLECT
```

**Three things this diagram encodes that are easy to get wrong:**

- **The goal is never asked for.** It is derived server-side from
  `targetWeightKg` against `weightKg` (§5.3), which is why `targetWeightKg` is
  required rather than optional.
- **Advisories do not block.** Out-of-range-but-plausible input, an underweight
  target, a clamped calorie floor — all come back alongside a `200`, because
  onboarding speed and completion are the core metrics (§9). The one exception
  is the age gate, which blocks with `422`.
- **Resume is server-driven.** `hasProfile` / `hasPlan` / `onboardedAt` from
  `/auth/session` map onto the last incomplete step (§4); the client stores no
  progress of its own.

---

## 4. Request lifecycle, coarse

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant M as RequestIdMiddleware
    participant A as BearerAuthentication
    participant V as View
    participant S as Service layer
    participant E as engine/
    participant D as PostgreSQL

    C->>M: HTTP request + Bearer token
    M->>M: adopt or mint X-Request-Id
    M->>A: dispatch
    A->>A: route on JWT alg — HS256 guest / RS256 Clerk
    A->>D: resolve or upsert the user row
    A-->>V: Actor
    V->>V: serializer validation — hard bounds, age gate
    V->>S: use case
    S->>D: read profile + active EngineConfig
    S->>E: calculate_plan
    E-->>S: PlanResult — pure, no I/O
    S->>D: persist Plan
    S-->>V: response payload + advisories
    V-->>M: 2xx JSON
    M-->>C: response with X-Request-Id
```

Any exception anywhere in that chain lands in `common.exceptions.api_exception_handler`
and comes back as one envelope, so the client has a single shape to branch on:

```json
{ "error": { "code": "profile_required", "message": "…", "details": {}, "requestId": "…" } }
```

---

## 5. Data model

```mermaid
erDiagram
    User ||--o| Profile : "has at most one"
    Profile ||--o| Plan : "has at most one"
    EngineConfig ||--o{ Plan : "produced"

    User {
        string id PK
        string clerkUserId UK "null for guests"
        enum authProvider "GUEST | CLERK"
        string externalProvider "oauth_google, oauth_apple"
        string email
        bool isGuest
        datetime claimedAt "set when merged into an account"
        string claimedFrom UK
    }

    Profile {
        string id PK
        string userId UK
        enum sexAtBirth "MALE | FEMALE | UNSPECIFIED"
        date dateOfBirth "age derived on every read"
        float weightKg "always metric"
        float heightCm "always metric"
        float targetWeightKg
        enum goal "derived, never submitted"
        enum activityLevel
        enum weightUnit "display only"
        enum heightUnit "display only"
        datetime onboardedAt
    }

    Plan {
        string id PK
        string profileId UK
        int caloriesKcal
        int proteinG
        int carbsG
        int fatG
        int fiberG "never counted as energy"
        float bmr
        float tdee
        bool clamped
        bool isEstimate
        int adjustmentKcal "effective"
        int requestedAdjustmentKcal "configured"
        int safetyFloorKcal
        string engineConfigId FK
        datetime computedAt
    }

    EngineConfig {
        string id PK
        string name UK
        bool isActive "exactly one row"
        int loseAdjustmentKcal
        int gainAdjustmentKcal
        float maintainBandKg
        float sedentaryMultiplier
        float proteinPerKg
        float fatPct
        int floorMaleKcal
        int floorFemaleKcal
        int targetRoundingKcal
    }
```

Every `Plan` records the `engineConfigId` that produced it, so a stored plan
stays explainable after the constants are retuned (§10).

---

## 6. Configuration and deployment

```mermaid
flowchart LR
    subgraph Runtime["Process"]
        DJ["Django WSGI/ASGI"]
        CACHE["EngineConfig cache<br/>60s TTL"]
        POOL["Prisma sync client<br/>one per process"]
    end

    ENV[".env<br/>DJANGO_SECRET_KEY · DATABASE_URL ·<br/>CLERK_ISSUER · MINIMUM_AGE_YEARS"]
    CHECKS["manage.py check<br/>secret key · Clerk · DATABASE_URL"]
    PG[("PostgreSQL")]
    OPS["Orchestrator probes"]

    ENV --> DJ
    ENV --> CHECKS
    CHECKS -.->|"fails the boot on a bad key"| DJ
    DJ --> CACHE
    CACHE -->|"miss or expiry"| POOL
    POOL --> PG
    OPS -->|"GET /healthz — process only"| DJ
    OPS -->|"GET /readyz — SELECT 1"| POOL
```

**Tuning without an app release.** Multipliers, goal adjustments, macro ratios,
safety floors and rounding live in the `EngineConfig` table. A retune goes live
within one cache TTL, with no deploy:

```bash
python manage.py engine_config --show
python manage.py engine_config --name v1 --set loseAdjustmentKcal=-350 --activate
```

A config lookup that fails falls back to the compiled defaults rather than
costing a user their plan.

---

## 7. Endpoint summary

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/api/v1/auth/guest` | none, throttled | Mint an anonymous session |
| `GET`/`POST` | `/api/v1/auth/session` | any session | Identity + resume point |
| `POST` | `/api/v1/auth/claim` | Clerk only | Move guest data onto the account |
| `GET`/`POST` | `/api/v1/onboarding/profile` | any session | Read / upsert the inputs |
| `POST` | `/api/v1/onboarding/plan` | any session | Compute and persist the plan |
| `GET` | `/api/v1/onboarding/plan` | any session | Fetch the stored plan |
| `POST` | `/api/v1/onboarding/complete` | any session | Stamp `onboardedAt` |
| `GET` | `/api/v1/onboarding/config` | none | Engine constants + validation bounds |
| `GET` | `/healthz` · `/readyz` | none | Liveness / readiness |
| `GET` | `/api/schema` · `/docs` · `/redoc` | none | OpenAPI 3 + docs UIs |
