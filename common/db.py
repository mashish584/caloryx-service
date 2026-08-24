"""Prisma client lifecycle.

The generated client is a *sync* client (see prisma/schema.prisma), which matches
Django's WSGI request model. One process-wide client is shared across threads;
the underlying query engine handles its own connection pool, so creating more
than one would just multiply pools.
"""
from __future__ import annotations

import atexit
import logging
import threading
from datetime import timedelta
from typing import TYPE_CHECKING, Optional

from django.conf import settings

if TYPE_CHECKING:  # pragma: no cover
    from prisma import Prisma

logger = logging.getLogger(__name__)

_client: Optional["Prisma"] = None
_lock = threading.Lock()


class PrismaClientUnavailable(RuntimeError):
    pass


def _import_prisma():
    try:
        from prisma import Prisma  # noqa: WPS433 - deliberately deferred
    except (ImportError, RuntimeError) as exc:
        raise PrismaClientUnavailable(
            "The Prisma client has not been generated. Run `prisma generate` "
            "(see README) before starting the service."
        ) from exc
    return Prisma


def get_client() -> "Prisma":
    """Return the shared, connected client, connecting on first use."""
    global _client

    client = _client
    if client is not None and client.is_connected():
        return client

    with _lock:
        if _client is None:
            prisma_cls = _import_prisma()
            _client = prisma_cls(auto_register=True)
        if not _client.is_connected():
            timeout = timedelta(seconds=settings.PRISMA_CONNECT_TIMEOUT_SECONDS)
            _client.connect(timeout=timeout)
            logger.info("prisma client connected")
    return _client


def disconnect() -> None:
    global _client
    with _lock:
        if _client is not None and _client.is_connected():
            _client.disconnect()
            logger.info("prisma client disconnected")
        _client = None


def ping() -> bool:
    """Cheap liveness probe for the readiness endpoint."""
    get_client().query_raw("SELECT 1")
    return True


atexit.register(disconnect)
