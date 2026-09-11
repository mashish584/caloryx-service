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
from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, Optional

from django.conf import settings

if TYPE_CHECKING:  # pragma: no cover
    from prisma import Prisma

logger = logging.getLogger(__name__)

_client: Optional["Prisma"] = None
_lock = threading.Lock()


class PrismaClientUnavailable(RuntimeError):
    pass


# -- date adaptation -------------------------------------------------------
#
# Prisma renders query arguments through a `singledispatch` serializer that
# knows `datetime.datetime` but not `datetime.date` (prisma._builder), so a bare
# date raises `TypeError: Type <class 'datetime.date'> not serializable` and the
# write comes back as a 500. The generated client types even a `@db.Date` column
# as `Optional[datetime]` on both input and output, so dates cross this boundary
# as datetimes in both directions and are converted here.
#
# Both legs are pinned to UTC and neither ever touches local time. A birth date
# is a calendar fact, not an instant: convert it through a local zone and
# midnight lands on the previous day for anyone west of UTC, silently shifting
# the date by one and, on a birthday, the derived age with it.


def to_prisma_date(value: Optional[date]) -> Optional[datetime]:
    """A plain `date` as the UTC-midnight `datetime` Prisma requires.

    Matches the "naive means UTC" assumption in `serialize_datetime`, so the
    calendar day submitted is the calendar day Postgres stores. A `datetime`
    passes through untouched - a caller that already holds one is not an error.
    """
    if value is None or isinstance(value, datetime):
        return value
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


def from_prisma_date(value: Optional[date]) -> Optional[date]:
    """The inverse: back to a plain `date` for serialization and arithmetic.

    Takes `.date()` directly rather than converting zones first, for the reason
    above. A `date` passes through unchanged, which is also what the repository
    seams in the test suite hand back.
    """
    if isinstance(value, datetime):
        return value.date()
    return value


def _import_prisma():
    try:
        from prisma import Prisma  # noqa: WPS433 - deliberately deferred
    except (ImportError, RuntimeError) as exc:
        raise PrismaClientUnavailable(
            "The Prisma client has not been generated. Run `prisma generate` "
            "(see README) before starting the service."
        ) from exc
    return Prisma


def _is_usable(client: "Prisma") -> bool:
    """True only if the client has an engine that can still serve queries.

    `Prisma.is_connected()` just checks that an engine object is attached, and
    prisma leaves a dead one attached in two cases: a failed `connect()` (the
    engine kills its process and closes its HTTP session, then re-raises) and
    its own `atexit` stop. Trusting it turns the shared client into a zombie
    that fails every query with `HTTPClientClosedError` until restart. Prisma
    exposes no public liveness check, hence the private attributes.
    """
    if not client.is_connected():
        return False
    engine = getattr(client, "_internal_engine", None)
    process = getattr(engine, "process", None)
    if process is None or process.poll() is not None:
        return False
    session = getattr(engine, "session", None)
    return not getattr(session, "closed", False)


def _discard_engine() -> None:
    """Detach and stop whatever engine the client holds so `connect()` starts clean."""
    try:
        _client.disconnect()
    except Exception:  # noqa: BLE001 - the engine is already dead; nothing to salvage
        logger.debug("error discarding dead prisma engine", exc_info=True)


def get_client() -> "Prisma":
    """Return the shared, connected client, connecting on first use."""
    global _client

    client = _client
    if client is not None and _is_usable(client):
        return client

    with _lock:
        if _client is None:
            prisma_cls = _import_prisma()
            _client = prisma_cls(auto_register=True)
        if not _is_usable(_client):
            if _client.is_connected():
                logger.warning("prisma engine is no longer usable; reconnecting")
                _discard_engine()
            timeout = timedelta(seconds=settings.PRISMA_CONNECT_TIMEOUT_SECONDS)
            try:
                _client.connect(timeout=timeout)
            except Exception:
                # Leave the client disconnected so the next request retries
                # instead of inheriting the half-built engine.
                _discard_engine()
                raise
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
