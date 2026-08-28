"""Every error the service can return is declared in the schema.

The envelope was already uniform (common/exceptions.py); what was missing was
saying so in OpenAPI. A generated client could see exactly one error response
across ten operations, so anything it did with failures was hand-rolled and free
to drift from the server.

These tests are deliberately structural. Restating the declarations would just
be a second copy of them; what is worth pinning is that the *shape* holds for
every operation, including ones added later.
"""
from __future__ import annotations

import pytest
from drf_spectacular.generators import SchemaGenerator
from rest_framework.test import APIRequestFactory

from common.exceptions import (
    AgeBelowMinimumError,
    ConflictError,
    DomainError,
    NotFoundError,
    ProfileRequiredError,
    api_exception_handler,
)

ERROR_REF = "#/components/schemas/ErrorResponse"


@pytest.fixture(scope="module")
def schema():
    return SchemaGenerator().get_schema(request=None, public=True)


def operations(schema):
    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            yield path, method, operation


def requires_auth(operation) -> bool:
    """drf-spectacular writes `security: [{}]` for AllowAny and a named scheme
    otherwise."""
    return any(entry for entry in operation.get("security", []))


def error_statuses(operation):
    return [code for code in operation["responses"] if code[0] in "45"]


# -- structural: holds for operations that do not exist yet -----------------


def test_every_authenticated_operation_declares_a_401(schema):
    """Token expiry is the most common failure a mobile client meets. This is
    the test that catches the next endpoint someone adds."""
    missing = [
        "{} {}".format(method.upper(), path)
        for path, method, op in operations(schema)
        if requires_auth(op) and "401" not in op["responses"]
    ]
    assert not missing, "authenticated but no 401 declared: {}".format(missing)


def test_public_operations_do_not_claim_a_401(schema):
    """/onboarding/config and /auth/guest are AllowAny; declaring a 401 there
    would send a client looking for a token it never needs."""
    wrong = [
        "{} {}".format(method.upper(), path)
        for path, method, op in operations(schema)
        if not requires_auth(op) and "401" in op["responses"]
    ]
    assert not wrong


def test_every_declared_error_uses_the_shared_envelope(schema):
    """One shape to branch on - an ad-hoc inline body would defeat the point."""
    for path, method, op in operations(schema):
        for code in error_statuses(op):
            content = op["responses"][code].get("content", {})
            ref = content["application/json"]["schema"].get("$ref")
            assert ref == ERROR_REF, "{} {} {} -> {}".format(
                method.upper(), path, code, ref
            )


def test_every_operation_declares_at_least_one_failure(schema):
    """A response block with only a 2xx is what this whole change was about."""
    bare = [
        "{} {}".format(method.upper(), path)
        for path, method, op in operations(schema)
        if not error_statuses(op)
    ]
    assert not bare, "no error declared at all: {}".format(bare)


# -- the codes that route the client somewhere ------------------------------


@pytest.mark.parametrize(
    "path,method,status,code",
    [
        ("/api/v1/onboarding/profile", "post", "422", "age_below_minimum"),
        ("/api/v1/onboarding/plan", "post", "409", "profile_required"),
        ("/api/v1/onboarding/plan", "get", "404", "plan_not_found"),
        ("/api/v1/onboarding/complete", "post", "409", "profile_required"),
        ("/api/v1/onboarding/complete", "post", "409", "plan_required"),
    ],
)
def test_the_routing_codes_are_named_in_the_schema(schema, path, method, status, code):
    """Status alone is not enough: /complete returns 409 for both a missing
    profile and a missing plan, and they route to different screens. The code
    has to be discoverable, so it is carried as an example."""
    response = schema["paths"][path][method]["responses"][status]
    examples = response["content"]["application/json"].get("examples", {})
    codes = {ex["value"]["error"]["code"] for ex in examples.values()}
    assert code in codes, "{} {} {} declares {}".format(method, path, status, codes)


@pytest.mark.parametrize(
    "exc,status,code",
    [
        (AgeBelowMinimumError(), 422, "age_below_minimum"),
        (ProfileRequiredError(), 409, "profile_required"),
        (ProfileRequiredError("x", code="plan_required"), 409, "plan_required"),
        (NotFoundError("x", code="plan_not_found"), 404, "plan_not_found"),
        (ConflictError("x", code="claim_conflict"), 409, "claim_conflict"),
    ],
)
def test_the_declared_codes_are_what_the_exceptions_actually_raise(exc, status, code):
    """Guards the other direction: the schema promises these pairs, so the
    exceptions have to keep producing them."""
    assert isinstance(exc, DomainError)
    assert (exc.status_code, exc.code) == (status, code)


# -- regression: the one error that skipped the envelope --------------------


def test_a_domain_error_response_carries_a_request_id():
    """`plan_not_found` used to be assembled inline in the view, bypassing
    `error_body`, which made it the only error in the service with no request id
    - and so the only one that could not be traced in support."""
    request = APIRequestFactory().get("/api/v1/onboarding/plan")
    request.request_id = "req-abc"

    response = api_exception_handler(
        NotFoundError("No plan has been generated yet.", code="plan_not_found"),
        {"request": request},
    )

    assert response.status_code == 404
    assert response.data["error"]["code"] == "plan_not_found"
    assert response.data["error"]["requestId"] == "req-abc"
