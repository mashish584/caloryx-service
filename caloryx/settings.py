"""Django settings for the CaloryX onboarding service.

Persistence is Prisma-only (see common/db.py), so `DATABASES` is intentionally
empty: any accidental use of the Django ORM fails loudly instead of quietly
opening a second, unmanaged connection pool.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_list(key: str, default: str = "") -> list:
    return [item.strip() for item in env(key, default).split(",") if item.strip()]


SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me")
DEBUG = env_bool("DJANGO_DEBUG", False)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")

INSTALLED_APPS = [
    # contenttypes/auth carry model definitions that DRF imports at module load.
    # They are never queried - see the empty DATABASES below.
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "corsheaders",
    "rest_framework",
    "drf_spectacular",
    "common",
    "authx",
    "onboarding",
    "meals",
    "assistant",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "common.middleware.RequestIdMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "caloryx.urls"
WSGI_APPLICATION = "caloryx.wsgi.application"
ASGI_APPLICATION = "caloryx.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    }
]

# Prisma owns the database. Nothing should reach the Django ORM.
DATABASES = {}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- REST framework -------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "authx.authentication.BearerAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "authx.permissions.IsAuthenticatedActor",
    ],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "common.exceptions.api_exception_handler",
    # Django's AnonymousUser would drag in the auth app's DB tables; we never
    # need it because permissions run off our own actor object.
    "UNAUTHENTICATED_USER": None,
    "DEFAULT_THROTTLE_CLASSES": [
        "authx.throttling.GuestCreationThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        # Guest sessions are unauthenticated and create rows, so they are the one
        # endpoint worth rate-limiting out of the box.
        "guest_create": env("GUEST_CREATE_RATE", "20/hour"),
    },
}

# --- API schema (drf-spectacular) ------------------------------------------
# Source of truth for FE type generation: `manage.py spectacular --file schema.yaml`
# (see scripts/export_openapi_schema.sh).
SPECTACULAR_SETTINGS = {
    "TITLE": "CaloryX API",
    "DESCRIPTION": "Onboarding, auth, and engine-config endpoints for the CaloryX client.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    # Per-operation security is derived from each view's authentication_classes
    # via authx.schema.BearerAuthenticationScheme.
    "SCHEMA_PATH_PREFIX": "/api/v1",
    # `PreferredUnits.weight` / `.height` are *units*, not measurements. Without
    # this the generated client would carry a `WeightEnum` sitting next to
    # `weightKg`, which reads as the weight itself.
    "ENUM_NAME_OVERRIDES": {
        "WeightUnitEnum": "engine.enums.WeightUnit",
        "HeightUnitEnum": "engine.enums.HeightUnit",
        # Advisory fields are named `code` / `field` / `severity`, so the derived
        # names would be `CodeEnum` and `FieldEnum` - meaningless beside the
        # error codes in a generated client.
        "AdvisoryCodeEnum": "engine.enums.ADVISORY_CODE_CHOICES",
        "AdvisoryFieldEnum": "engine.enums.ADVISORY_FIELD_CHOICES",
        "AdvisorySeverityEnum": "engine.enums.ADVISORY_SEVERITY_CHOICES",
        # `state` is named identically (and shares its choice set) across
        # FoodSerializer/LoggedMealItemInputSerializer/etc; without this,
        # spectacular can't tell those apart from an unrelated `StateEnum`.
        "StateEnum": "nutrition.enums.FoodState",
        "SourceEnum": "nutrition.enums.FoodSource",
        "SlotEnum": "nutrition.enums.MealSlot",
        "TypeEnum": "nutrition.enums.ServingUnitType",
        # `MealDraftSerializer.parseTier` and `MessageResponseSerializer.tier`
        # share the same ParseTier choice set but auto-derive different
        # component names ("ParseTierEnum" vs "TierEnum") from their field
        # names - same issue as StateEnum above.
        "TierEnum": "chatparser.enums.ParseTier",
    },
}

# --- CORS -----------------------------------------------------------------
# Expo dev clients send no Origin for native requests; web builds do.
CORS_ALLOWED_ORIGINS = env_list("CORS_ALLOWED_ORIGINS", "http://localhost:8081")
CORS_ALLOW_CREDENTIALS = False

# --- Prisma ---------------------------------------------------------------
DATABASE_URL = env("DATABASE_URL")
PRISMA_CONNECT_TIMEOUT_SECONDS = int(env("PRISMA_CONNECT_TIMEOUT_SECONDS", "10"))

# --- Clerk ----------------------------------------------------------------
# The mobile app authenticates with Clerk (Google / Apple / any future provider)
# and sends the resulting session JWT as `Authorization: Bearer <token>`. We
# verify it against Clerk's JWKS; the service never holds a provider secret.
CLERK_ISSUER = env("CLERK_ISSUER").rstrip("/")
CLERK_JWKS_URL = env("CLERK_JWKS_URL") or (
    "{}/.well-known/jwks.json".format(CLERK_ISSUER) if CLERK_ISSUER else ""
)
# Optional. Set only if the Clerk JWT template sets an `aud` claim.
CLERK_AUDIENCE = env("CLERK_AUDIENCE") or None
# Tolerance for clock skew between the device and this server, in seconds.
CLERK_LEEWAY_SECONDS = int(env("CLERK_LEEWAY_SECONDS", "30"))
CLERK_JWKS_CACHE_SECONDS = int(env("CLERK_JWKS_CACHE_SECONDS", "600"))

# --- OpenAI (T2/T3, AI Meal Assistant PRD §7.3) ---------------------------
# The small model handles the overwhelming majority of escalations (§7.1);
# T3 (a larger model for low-confidence T2 results) is Chunk 4c.
OPENAI_API_KEY = env("OPENAI_API_KEY")
OPENAI_SMALL_MODEL = env("OPENAI_SMALL_MODEL", "gpt-4o-mini")
OPENAI_LARGE_MODEL = env("OPENAI_LARGE_MODEL", "gpt-4o")
# ~300 per §7.3 - the envelope is a small structured object, not prose.
OPENAI_MAX_TOKENS = int(env("OPENAI_MAX_TOKENS", "300"))
OPENAI_TIMEOUT_SECONDS = float(env("OPENAI_TIMEOUT_SECONDS", "10"))
# Approximate list pricing in micros of USD per 1M tokens - verify against
# OpenAI's current pricing page before relying on this for real
# billing/alerting; it only feeds ParseEvent.costMicros for now, not a charge.
OPENAI_SMALL_MODEL_INPUT_COST_PER_1M_MICROS = int(
    env("OPENAI_SMALL_MODEL_INPUT_COST_PER_1M_MICROS", "150000")
)
OPENAI_SMALL_MODEL_OUTPUT_COST_PER_1M_MICROS = int(
    env("OPENAI_SMALL_MODEL_OUTPUT_COST_PER_1M_MICROS", "600000")
)
OPENAI_LARGE_MODEL_INPUT_COST_PER_1M_MICROS = int(
    env("OPENAI_LARGE_MODEL_INPUT_COST_PER_1M_MICROS", "2500000")
)
OPENAI_LARGE_MODEL_OUTPUT_COST_PER_1M_MICROS = int(
    env("OPENAI_LARGE_MODEL_OUTPUT_COST_PER_1M_MICROS", "10000000")
)

# --- AI quota & T3 escalation (§5.1.4, §7.1, §12.8, Chunk 4c) --------------
# 20 LOG_NEW AI parses per rolling 24h for free tier (§5.1.4) - a semi-rolling
# window (see assistant.repository.try_consume_quota), not a true sliding
# log. No premium/subscription concept exists in this codebase yet, so this
# single limit applies to every user until one does.
AI_QUOTA_LIMIT = int(env("AI_QUOTA_LIMIT", "20"))
AI_QUOTA_WINDOW_HOURS = int(env("AI_QUOTA_WINDOW_HOURS", "24"))
# One simple, honestly-scoped threshold (no shadow mode, no corrections data
# to calibrate against yet - same reasoning as the T1->T2 router in Chunk 4a).
T3_ESCALATION_CONFIDENCE_THRESHOLD = float(env("T3_ESCALATION_CONFIDENCE_THRESHOLD", "0.5"))

# --- Wellbeing safeguards (§5.6, Chunk 6b) ---------------------------------
# PLACEHOLDER SCAFFOLDING pending clinical/trust-and-safety review (§5.6's own
# framing: "written here as a requirement, not a finished policy") - do not
# treat WELLBEING_RESOURCES or the reply text in assistant/services.py as
# reviewed copy.
#
# Off by default: a T1-successful message (real, parseable food) never calls
# a model today, and this flag is what would change that for every message,
# not just the ones that already reach a model for other reasons. Flip to
# true only once the broader keyword net (chatparser.has_wellbeing_signal)
# and the response copy below have had real review.
WELLBEING_CHECK_ALL_MESSAGES = env_bool("WELLBEING_CHECK_ALL_MESSAGES", False)
# This *is* the "remote config" resource list §5.6 asks for, in placeholder
# form - no real remote-config mechanism exists in this codebase. NEDA's own
# helpline is permanently disconnected (§5.6's explicit correction) - the
# National Alliance for Eating Disorders replaces it here. US-only, which the
# PRD itself says isn't acceptable given India is a primary market - locale
# expansion is required before this ships for real, not before this chunk
# compiles.
WELLBEING_RESOURCES = [
    {
        "name": "National Alliance for Eating Disorders Helpline",
        "region": "US",
        "phone": "1-866-662-1235",
    },
]

# --- Offline queue & sync (§12.12, Chunk 7) --------------------------------
# Hard expiry - a queued op older than this is refused and surfaced for
# review (§12.12), never silently processed.
MAX_QUEUE_AGE_DAYS = int(env("MAX_QUEUE_AGE_DAYS", "30"))
# Soft staleness gate - older than this needs an explicit confirmation flag.
STALE_QUEUE_AGE_DAYS = int(env("STALE_QUEUE_AGE_DAYS", "7"))
# REPLAY_WINDOW = MAX_QUEUE_AGE + a 1-day retry tail (§9) - deliberately
# *derived*, not independently configurable: a shorter idempotency TTL than
# the queue's own hard-expiry window is exactly the bug that motivated this
# (a queued op replayed near the 30-day limit finding no idempotency record
# and creating a duplicate meal). Used by every replay-relevant idempotency
# record (the structured mutations + confirm) - NOT `POST /messages`, which
# keeps its own 24h window since descriptive/AI input isn't a queueable
# `opType` at all (§12.12).
REPLAY_WINDOW_HOURS = (MAX_QUEUE_AGE_DAYS + 1) * 24
# §12.5's "beyond epsilon" threshold for the catalog-drift note on confirm.
NUTRITION_DRIFT_EPSILON_KCAL = float(env("NUTRITION_DRIFT_EPSILON_KCAL", "5"))

# --- Reproducibility & cache-key versioning (§12.3, §12.7, Chunk 8a) -------
# These three only ever change via a code deploy (editing chatparser's
# normalization/grammar, or nutrition/calculator.py) - a code-level constant
# is the honest representation of "changes only when this code changes".
# The fourth piece of §12.7's cache-key formula, the food catalog's own
# version, is *not* here - Food/CompositeFood/DishCategoryProfile are edited
# by hand with no code deploy involved, so it lives in the DB-backed
# `CatalogVersion` singleton instead (see meals.repository.get_catalog_version
# and `manage.py bump_catalog_version`).
NORMALIZATION_VERSION = int(env("NORMALIZATION_VERSION", "1"))
PARSER_VERSION = int(env("PARSER_VERSION", "1"))
NUTRITION_ENGINE_VERSION = int(env("NUTRITION_ENGINE_VERSION", "1"))

# --- Privacy & retention (§12.14, Chunk 8c) --------------------------------
# `ChatMessage` TTL - raw phrasing has little value once parsed, so it's kept
# for a much shorter window than `LoggedMeal` (§12.14). No scheduler exists in
# this repo (same as every seed_* command) - `manage.py purge_chat_messages`
# is run by hand or from an external cron, not wired up here.
CHAT_MESSAGE_RETENTION_DAYS = int(env("CHAT_MESSAGE_RETENTION_DAYS", "30"))

# --- Guest sessions -------------------------------------------------------
# Guest mode is not a Clerk concept, so we mint our own short-lived tokens.
GUEST_TOKEN_TTL_DAYS = int(env("GUEST_TOKEN_TTL_DAYS", "180"))
GUEST_TOKEN_ISSUER = env("GUEST_TOKEN_ISSUER", "caloryx-service")

# --- Domain rules ---------------------------------------------------------
# PRD §9: 18 for v1, pending legal sign-off per launch market. Configurable so
# the threshold can move without a code change.
MINIMUM_AGE_YEARS = int(env("MINIMUM_AGE_YEARS", "18"))
MAXIMUM_AGE_YEARS = int(env("MAXIMUM_AGE_YEARS", "100"))

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"
        }
    },
    "filters": {"request_id": {"()": "common.middleware.RequestIdLogFilter"}},
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
            "filters": ["request_id"],
        }
    },
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False}
    },
}
