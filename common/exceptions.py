"""Uniform error envelope.

Every failure - validation, auth, domain rule, or crash - comes back as:

    {"error": {"code": "...", "message": "...", "details": {...},
               "requestId": "..."}}

so the client has one shape to branch on. Onboarding never dead-ends the user
(PRD §9), so the client can always retry on anything 5xx.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from rest_framework import status
from rest_framework.response import Response

logger = logging.getLogger(__name__)


class DomainError(Exception):
    """A rule this service enforces, as opposed to a transport or input error."""

    code = "domain_error"
    status_code = status.HTTP_400_BAD_REQUEST
    message = "Request could not be completed."

    def __init__(
        self,
        message: Optional[str] = None,
        *,
        code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        status_code: Optional[int] = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.details = details or {}
        self.status_code = status_code or self.status_code
        super().__init__(self.message)


class NotFoundError(DomainError):
    code = "not_found"
    status_code = status.HTTP_404_NOT_FOUND
    message = "Resource not found."


class ProfileRequiredError(DomainError):
    code = "profile_required"
    status_code = status.HTTP_409_CONFLICT
    message = "Complete the profile step before generating a plan."


class AgeBelowMinimumError(DomainError):
    """PRD §9 - account creation is blocked below the minimum age."""

    code = "age_below_minimum"
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    message = "You must meet the minimum age to use CaloryX."


class ConflictError(DomainError):
    code = "conflict"
    status_code = status.HTTP_409_CONFLICT
    message = "Conflicting state."


class UpstreamUnavailableError(DomainError):
    code = "upstream_unavailable"
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    message = "A dependency is unavailable. Please retry."


class UnresolvableQuantityError(DomainError):
    """A meal item's quantity could not be turned into a mass (meals app, PRD
    §8) - an unrecognised unit, or a raw/cooked conversion with no yield
    factor on the food. 422 rather than 400: the request is well-formed, the
    domain data just can't satisfy it."""

    code = "invalid_quantity"
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    message = "Could not resolve this item's quantity to a mass."


class OpenDraftExistsError(DomainError):
    """One open draft per user (assistant app, PRD §9, §12.2) - raised instead
    of silently starting a second one or clobbering the first. Callers attach
    the existing draft as `details={"draft": <serialized draft>}` so the
    client can re-render it, mirroring the PRD's §10.1 version-conflict shape."""

    code = "open_draft_exists"
    status_code = status.HTTP_409_CONFLICT
    message = "You already have an open meal draft."


class DraftVersionConflictError(DomainError):
    """Optimistic-lock mismatch (§12.1) - the request's `version` doesn't
    match the draft's current one, meaning it changed since the client last
    read it. Callers attach `details={"draft": <serialized draft>}` so the
    client re-renders rather than retrying blindly (§10.1)."""

    code = "draft_version_conflict"
    status_code = status.HTTP_409_CONFLICT
    message = "This meal changed since you last saw it."


class DraftNotOpenError(DomainError):
    """A mutation was attempted against a draft in a terminal state
    (CONFIRMED/DISCARDED/EXPIRED) - the draft state machine only allows
    mutations while OPEN (§12.2)."""

    code = "draft_not_open"
    status_code = status.HTTP_409_CONFLICT
    message = "This draft can no longer be changed."


class IdempotencyKeyReuseError(DomainError):
    """The same idempotency key arrived with a different request body (§9's
    `IdempotencyRecord.requestHash` guard) - a replay must match both the key
    AND the original content, or a client bug could silently apply someone
    else's stale request under a reused key."""

    code = "idempotency_key_reused"
    status_code = status.HTTP_409_CONFLICT
    message = "This idempotency key was already used for a different request."


def error_body(
    code: str,
    message: str,
    details: Optional[Dict[str, Any]] = None,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    if request_id:
        body["error"]["requestId"] = request_id
    return body


def api_exception_handler(exc, context):
    # Imported here, not at module scope: `rest_framework.views` resolves
    # DEFAULT_AUTHENTICATION_CLASSES on import, which imports authx, which
    # imports this module.
    from rest_framework.views import exception_handler as drf_exception_handler

    request = context.get("request")
    request_id = getattr(request, "request_id", None)

    if isinstance(exc, DomainError):
        return Response(
            error_body(exc.code, exc.message, exc.details, request_id),
            status=exc.status_code,
        )

    response = drf_exception_handler(exc, context)
    if response is None:
        logger.exception("unhandled error in %s", context.get("view"))
        return Response(
            error_body(
                "internal_error",
                "Something went wrong on our end. Please retry.",
                request_id=request_id,
            ),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    detail = response.data
    if isinstance(detail, dict) and "detail" in detail and len(detail) == 1:
        code = getattr(detail["detail"], "code", None) or "error"
        response.data = error_body(code, str(detail["detail"]), request_id=request_id)
    else:
        # Field-level validation errors keep their per-field shape under `details`.
        response.data = error_body(
            "validation_error",
            "Some fields need attention.",
            details=detail if isinstance(detail, dict) else {"errors": detail},
            request_id=request_id,
        )
    return response
