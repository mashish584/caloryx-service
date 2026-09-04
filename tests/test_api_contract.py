"""Transport-level contract: auth gating, error envelope, and the endpoints that
work without a database."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.test import Client
from django.urls import reverse

from authx import repository as authx_repository
from authx.tokens import issue_guest_token
from common.exceptions import AgeBelowMinimumError, DomainError, error_body


@pytest.fixture
def client():
    return Client()


PROTECTED = [
    "/api/v1/onboarding/profile",
    # POST-only, and still a 401 rather than a 405 for the GET these tests
    # issue: DRF authenticates in `initial()`, before it resolves the handler.
    "/api/v1/onboarding/advisories",
    "/api/v1/onboarding/plan",
    "/api/v1/auth/session",
]


@pytest.mark.parametrize("path", PROTECTED)
def test_protected_endpoints_reject_anonymous_callers(client, path):
    response = client.get(path)
    assert response.status_code == 401
    assert response.json()["error"]["code"]


@pytest.mark.parametrize("path", PROTECTED)
def test_a_malformed_authorization_header_is_rejected(client, path):
    response = client.get(path, HTTP_AUTHORIZATION="Token abc123")
    assert response.status_code == 401


def test_garbage_bearer_token_is_rejected(client):
    response = client.get(
        "/api/v1/auth/session", HTTP_AUTHORIZATION="Bearer not-a-real-jwt"
    )
    assert response.status_code == 401


def test_engine_config_is_public_so_the_client_preview_can_match(client):
    """§10 - the constants live in server config, not client constants."""
    response = client.get("/api/v1/onboarding/config")
    body = response.json()

    assert response.status_code == 200
    assert body["goalAdjustmentsKcal"] == {"LOSE": -400, "MAINTAIN": 0, "GAIN": 400}
    assert body["activityMultipliers"]["SEDENTARY"] == 1.2
    assert body["activityMultipliers"]["EXTREME"] == 1.9
    assert body["macros"] == {
        "proteinPerKg": 1.8,
        "fatPct": 0.25,
        "fiberGPer1000Kcal": 15.0,
    }
    assert body["safetyFloorsKcal"] == {"MALE": 1500, "FEMALE": 1200, "UNSPECIFIED": 1500}
    # §9 - the bounds the API rejects on ship alongside the tuning constants, so
    # the client can stop a bad value at the field. See tests/test_config_bounds.py.
    assert body["validation"]["age"] == {"min": 18, "max": 100}
    assert body["validation"]["weightKg"]["max"] == 500.0
    assert body["validation"]["heightCm"]["softMin"] == 130.0


@pytest.fixture
def guest(monkeypatch):
    """A usable guest session without a database.

    `BearerAuthentication` verifies the token itself and then looks the row up,
    so the repository is the only seam standing between a signed token and an
    authenticated request - the same seam `tests/test_services.py` uses.
    """
    monkeypatch.setattr(
        authx_repository,
        "get_user",
        lambda user_id: SimpleNamespace(id=user_id, claimedAt=None),
    )
    return "Bearer " + issue_guest_token("user-1")["token"]


def test_advisories_answers_with_hints_and_no_stored_record(client, guest):
    """§9 through the real route: the values are judged, not kept, so there is
    no `profile` key to hand back."""
    response = client.post(
        "/api/v1/onboarding/advisories",
        data={"weightKg": 300.0, "heightCm": 175.0, "targetWeightKg": 90.0},
        content_type="application/json",
        HTTP_AUTHORIZATION=guest,
    )
    body = response.json()

    assert response.status_code == 200
    assert set(body) == {"advisories"}
    assert body["advisories"][0]["code"] == "weight_out_of_typical_range"
    assert body["advisories"][0]["field"] == "weightKg"


def test_advisories_returns_an_empty_list_when_the_values_look_fine(client, guest):
    response = client.post(
        "/api/v1/onboarding/advisories",
        data={"weightKg": 80.0, "heightCm": 175.0, "targetWeightKg": 72.0},
        content_type="application/json",
        HTTP_AUTHORIZATION=guest,
    )

    assert response.status_code == 200
    assert response.json() == {"advisories": []}


def test_advisories_still_rejects_a_value_outside_the_hard_caps(client, guest):
    """The soft ranges advise; the caps in `validation_bounds` reject, here as
    much as on the save."""
    response = client.post(
        "/api/v1/onboarding/advisories",
        data={"weightKg": 900.0, "heightCm": 175.0, "targetWeightKg": 72.0},
        content_type="application/json",
        HTTP_AUTHORIZATION=guest,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


def test_health_does_not_touch_the_database(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_reports_degraded_when_the_database_is_unreachable(client):
    """The probe reports; it never raises."""
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["database"] == "unavailable"


def test_every_response_carries_a_request_id(client):
    response = client.get("/healthz")
    assert response["X-Request-Id"]


def test_an_inbound_request_id_is_echoed_back(client):
    response = client.get("/healthz", HTTP_X_REQUEST_ID="trace-me-123")
    assert response["X-Request-Id"] == "trace-me-123"


def test_error_envelope_shape():
    body = error_body("some_code", "Some message.", {"field": "x"}, "req-1")
    assert body == {
        "error": {
            "code": "some_code",
            "message": "Some message.",
            "details": {"field": "x"},
            "requestId": "req-1",
        }
    }


def test_domain_errors_carry_their_own_code_and_status():
    err = AgeBelowMinimumError(details={"minimumAge": 18})
    assert isinstance(err, DomainError)
    assert (err.code, err.status_code) == ("age_below_minimum", 422)


def test_urls_resolve():
    assert reverse("onboarding-advisories") == "/api/v1/onboarding/advisories"
    assert reverse("onboarding-plan") == "/api/v1/onboarding/plan"
    assert reverse("onboarding-complete") == "/api/v1/onboarding/complete"
    assert reverse("auth-guest") == "/api/v1/auth/guest"
    assert reverse("auth-claim") == "/api/v1/auth/claim"
