"""Test settings.

pytest-django configures Django before conftest.py is imported, so environment
overrides have to arrive as a settings module rather than as env vars.

Nothing here reaches Postgres: the engine is pure, and the layers that do talk
to Prisma are exercised through their repository seams.
"""
from __future__ import annotations

import os

# caloryx.settings calls load_dotenv(), which never overrides a variable
# already present in the environment. Set this first so the real DATABASE_URL
# in .env can't leak into the Prisma client the readiness probe connects with.
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")

from caloryx.settings import *  # noqa: F401,F403

# 64 chars: guest tokens are HS256-signed with this, and PyJWT warns below 32 bytes.
SECRET_KEY = "t" * 64

CLERK_ISSUER = "https://example.clerk.accounts.dev"
CLERK_JWKS_URL = "{}/.well-known/jwks.json".format(CLERK_ISSUER)
CLERK_AUDIENCE = None

DATABASE_URL = os.environ["DATABASE_URL"]

MINIMUM_AGE_YEARS = 18
MAXIMUM_AGE_YEARS = 100
