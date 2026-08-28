"""Reusable OpenAPI declarations for the error envelope.

Every failure returns the same shape (see common.exceptions), but the *status*
alone is not enough for a client to act on: 409 covers both `profile_required`
and `plan_required`, and 404 covers `plan_not_found`, `guest_not_found` and
`user_not_found`. The `code` is the discriminator, so it goes into the schema as
an example on every response that can carry it.

Declared per operation rather than as one shared enum: a global list of codes
would imply every code is reachable on every endpoint, which is worse than
saying nothing.
"""
from __future__ import annotations

from drf_spectacular.utils import OpenApiExample, OpenApiResponse

from common.serializers import ErrorResponseSerializer

# Sample messages, keyed by code, so the generated docs show a realistic body
# rather than a placeholder. Kept short - the message is not a contract, the
# code is.
_MESSAGES = {
    "validation_error": "Some fields need attention.",
    "authentication_failed": "Authentication credentials were not provided.",
    "permission_denied": "Sign in to continue.",
    "throttled": "Request was throttled. Expected available in 60 seconds.",
    "profile_required": "Complete the profile step before generating a plan.",
    "plan_required": "Generate a plan before completing onboarding.",
    "age_below_minimum": "CaloryX is available to people aged 18 and over.",
    "plan_not_found": "No plan has been generated yet.",
    "invalid_guest_token": "Guest token is invalid or expired.",
    "guest_not_found": "Guest session not found.",
    "user_not_found": "Account not found.",
    "not_a_guest_session": "That session is not a guest session.",
    "guest_already_claimed": "That guest session has already been claimed.",
    "claim_conflict": "This account already has onboarding data.",
    "internal_error": "Something went wrong on our end. Please retry.",
}


def _example(code: str) -> OpenApiExample:
    return OpenApiExample(
        code,
        value={
            "error": {
                "code": code,
                "message": _MESSAGES.get(code, "Request could not be completed."),
                "requestId": "3f1c0a7e9b2d4c8e",
            }
        },
        response_only=True,
    )


def error_response(description: str, *codes: str) -> OpenApiResponse:
    """The standard envelope, with one example per `code` this status can carry."""
    return OpenApiResponse(
        ErrorResponseSerializer,
        description=description,
        examples=[_example(code) for code in codes],
    )


# Shared across most operations. Anything endpoint-specific is declared inline
# at the view, next to the code that raises it.
UNAUTHORIZED = error_response(
    "Missing, malformed, or expired bearer token.", "authentication_failed"
)
FORBIDDEN = error_response(
    "Authenticated, but this session may not perform the action.", "permission_denied"
)
VALIDATION_ERROR = error_response(
    "One or more fields failed validation; see `details` for the per-field errors.",
    "validation_error",
)
THROTTLED = error_response("Rate limit exceeded; retry later.", "throttled")
SERVER_ERROR = error_response(
    "Unexpected failure. Onboarding never dead-ends the user (§9), so the client\n"
    "may always retry a 5xx.",
    "internal_error",
)
