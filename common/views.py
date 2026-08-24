from __future__ import annotations

import logging

from django.http import JsonResponse

from .db import ping

logger = logging.getLogger(__name__)


def health(_request):
    """Liveness: the process is up. Deliberately does not touch the database."""
    return JsonResponse({"status": "ok"})


def readiness(_request):
    """Readiness: the process can serve traffic, database included."""
    try:
        ping()
    except Exception as exc:  # noqa: BLE001 - probe reports, never raises
        logger.warning("readiness probe failed: %s", exc)
        return JsonResponse({"status": "degraded", "database": "unavailable"}, status=503)
    return JsonResponse({"status": "ok", "database": "ok"})
