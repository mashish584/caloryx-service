"""Transport-level contract: auth gating, error envelope, and the endpoints that
work without a database."""
from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from common.exceptions import AgeBelowMinimumError, DomainError, error_body


@pytest.fixture
def client():
    return Client()


PROTECTED = ["/api/v1/onboarding/profile", "/api/v1/onboarding/plan", "/api/v1/auth/session"]


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
    assert reverse("onboarding-plan") == "/api/v1/onboarding/plan"
    assert reverse("onboarding-complete") == "/api/v1/onboarding/complete"
    assert reverse("auth-guest") == "/api/v1/auth/guest"
    assert reverse("auth-claim") == "/api/v1/auth/claim"
