"""GET /onboarding/config publishes the input bounds - PRD §9, §10.

The client cannot enforce what it cannot see, so `/config` advertises the caps
the API rejects on. The value is entirely in these numbers *staying* true, so
these tests compare the advertised block against real enforcement - the field
validators and the settings - rather than restating the constants.
"""
from __future__ import annotations

import pytest
from django.conf import settings
from django.test import Client, override_settings

from engine import DEFAULT_CONFIG
from engine.advisories import SOFT_HEIGHT_CM, SOFT_WEIGHT_KG
from onboarding.serializers import ProfileUpsertSerializer, validation_bounds
from tests.support import dob_str

VALID = {
    "sexAtBirth": "MALE",
    "dateOfBirth": dob_str(30),
    "weightKg": 90.0,
    "heightCm": 180.0,
    "goal": "LOSE",
    "activityLevel": "SEDENTARY",
}

MEASUREMENTS = [("weightKg", SOFT_WEIGHT_KG), ("heightCm", SOFT_HEIGHT_CM)]


@pytest.fixture
def bounds():
    return validation_bounds()


def accepts(**overrides):
    return ProfileUpsertSerializer(data=dict(VALID, **overrides)).is_valid()


# -- the advertised numbers are the enforced numbers ------------------------


@pytest.mark.parametrize("field", ["weightKg", "heightCm"])
def test_published_caps_are_the_validators_own_caps(bounds, field):
    """Editing a range without re-publishing it is the failure this catches."""
    validator = ProfileUpsertSerializer().fields[field]

    assert bounds[field]["min"] == validator.min_value
    assert bounds[field]["max"] == validator.max_value


def test_published_age_bounds_track_settings(bounds):
    """MINIMUM_AGE_YEARS is env-tunable, so a hardcoded client copy goes stale
    the moment a market moves it. This is the number that must be fetched."""
    assert bounds["age"]["min"] == settings.MINIMUM_AGE_YEARS
    assert bounds["age"]["max"] == settings.MAXIMUM_AGE_YEARS


@override_settings(MINIMUM_AGE_YEARS=21, MAXIMUM_AGE_YEARS=90)
def test_published_age_bounds_follow_a_retuned_setting():
    """The scenario this endpoint exists for: a market moves the minimum age and
    every installed client picks it up without an app release."""
    assert validation_bounds()["age"] == {"min": 21, "max": 90}

    body = Client().get("/api/v1/onboarding/config").json()
    assert body["validation"]["age"] == {"min": 21, "max": 90}


@pytest.mark.parametrize("field,soft", MEASUREMENTS)
def test_published_soft_bounds_are_the_advisory_bounds(bounds, field, soft):
    assert (bounds[field]["softMin"], bounds[field]["softMax"]) == soft


@pytest.mark.parametrize("field,_soft", MEASUREMENTS)
def test_soft_bounds_sit_inside_the_hard_caps(bounds, field, _soft):
    """A soft bound outside the cap would advertise a warning range the API
    rejects outright."""
    b = bounds[field]
    assert b["min"] <= b["softMin"] < b["softMax"] <= b["max"]


# -- and they describe what actually happens --------------------------------


@pytest.mark.parametrize("field", ["weightKg", "heightCm"])
def test_a_value_at_each_published_cap_is_accepted(bounds, field):
    assert accepts(**{field: bounds[field]["min"]})
    assert accepts(**{field: bounds[field]["max"]})


@pytest.mark.parametrize("field", ["weightKg", "heightCm"])
def test_a_value_past_each_published_cap_is_rejected(bounds, field):
    assert not accepts(**{field: bounds[field]["min"] - 0.1})
    assert not accepts(**{field: bounds[field]["max"] + 0.1})


@pytest.mark.parametrize("field,soft", MEASUREMENTS)
def test_between_the_caps_and_the_soft_bounds_is_accepted(bounds, field, soft):
    """§9 - unusual but plausible warns, it does not reject."""
    assert accepts(**{field: bounds[field]["min"] + 0.5})
    assert accepts(**{field: soft[1] + 1.0})


def test_target_weight_shares_the_published_weight_caps(bounds):
    assert not accepts(targetWeightKg=bounds["weightKg"]["max"] + 0.1)
    assert accepts(targetWeightKg=bounds["weightKg"]["max"])


# -- transport --------------------------------------------------------------


def test_the_bounds_are_fetchable_before_sign_in():
    """Onboarding collects these inputs before auth, so the block has to come
    back without a token (the endpoint is AllowAny)."""
    response = Client().get("/api/v1/onboarding/config")

    assert response.status_code == 200
    assert response.json()["validation"] == validation_bounds()


# -- the engine stays Django-free -------------------------------------------


def test_the_engine_payload_omits_validation_when_none_is_injected():
    """`to_public_dict` must not reach for settings itself: engine/config.py is
    deliberately importable without Django (§8)."""
    assert "validation" not in DEFAULT_CONFIG.to_public_dict()


def test_injected_bounds_are_copied_not_aliased():
    injected = validation_bounds()
    payload = DEFAULT_CONFIG.to_public_dict(injected)

    injected["age"]["min"] = 999
    assert payload["validation"]["age"]["min"] != 999
