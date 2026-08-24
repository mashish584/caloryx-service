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
    "common",
    "authx",
    "onboarding",
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
