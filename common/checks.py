"""Deployment checks that run on `manage.py check` and at startup.

Guest bearer tokens are HS256 signed with `SECRET_KEY` (authx/tokens.py), so a
short or default key is not just a Django convention here - it is what stands
between an attacker and forging a session for any guest user id.
"""
from __future__ import annotations

from django.conf import settings
from django.core.checks import Error, Warning, register

MIN_SECRET_KEY_BYTES = 32
DEV_SECRET_KEY = "dev-only-insecure-key-change-me"


@register()
def check_secret_key(app_configs, **kwargs):
    issues = []
    key = settings.SECRET_KEY or ""

    if key == DEV_SECRET_KEY:
        issues.append(
            (Error if not settings.DEBUG else Warning)(
                "DJANGO_SECRET_KEY is still the built-in development value.",
                hint="Guest session tokens are signed with it. Set a random 64-char value.",
                id="caloryx.E001",
            )
        )
    elif len(key.encode()) < MIN_SECRET_KEY_BYTES:
        issues.append(
            Warning(
                "DJANGO_SECRET_KEY is shorter than the {}-byte minimum for HS256.".format(
                    MIN_SECRET_KEY_BYTES
                ),
                hint="Guest session tokens are signed with it (RFC 7518 §3.2).",
                id="caloryx.W001",
            )
        )
    return issues


@register()
def check_clerk_configured(app_configs, **kwargs):
    if settings.CLERK_JWKS_URL:
        return []
    return [
        (Warning if settings.DEBUG else Error)(
            "Clerk is not configured; signed-in sessions cannot be verified.",
            hint="Set CLERK_ISSUER (or CLERK_JWKS_URL). Guest mode still works without it.",
            id="caloryx.E002",
        )
    ]


@register()
def check_database_url(app_configs, **kwargs):
    if settings.DATABASE_URL:
        return []
    return [
        Error(
            "DATABASE_URL is not set; Prisma cannot connect.",
            hint="See .env.example.",
            id="caloryx.E003",
        )
    ]
