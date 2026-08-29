"""The goal is derived from the target weight - PRD §5.3.

Onboarding used to ask for the goal on its own screen and then ask for a target
weight that already implied it. Two fields carrying one answer could contradict
each other, so the screen went and `EngineConfig.goal_for` became the only
producer of a goal. These tests pin the rule, its band, and the fact that the
band is server-tunable rather than compiled in.
"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from engine import DEFAULT_CONFIG, Goal
from onboarding import repository, services
from tests.support import dob_for_age

PROFILE = {
    "sexAtBirth": "FEMALE",
    "dateOfBirth": dob_for_age(30),
    "weightKg": 80.0,
    "heightCm": 170.0,
    "targetWeightKg": 72.0,
    "activityLevel": "MODERATE",
}


# -- the rule ---------------------------------------------------------------


@pytest.mark.parametrize(
    "target,expected",
    [
        (70.0, Goal.LOSE),
        (78.9, Goal.LOSE),
        (79.0, Goal.MAINTAIN),  # exactly on the lower edge - the band is inclusive
        (80.0, Goal.MAINTAIN),
        (81.0, Goal.MAINTAIN),  # and on the upper edge
        (81.1, Goal.GAIN),
        (90.0, Goal.GAIN),
    ],
)
def test_the_target_weight_picks_the_goal(target, expected):
    assert DEFAULT_CONFIG.goal_for(80.0, target) is expected


def test_the_band_is_what_separates_a_hold_from_an_adjustment():
    """The point of the band: a difference smaller than it must not buy a 400
    kcal deficit, which is what exact equality would have allowed."""
    near = DEFAULT_CONFIG.goal_for(80.0, 79.7)

    assert near is Goal.MAINTAIN
    assert DEFAULT_CONFIG.adjustment_for(near) == 0


def test_a_retuned_band_moves_the_boundary():
    """§10 - the band is config, so a market can widen it without an app
    release. A compiled-in threshold on the client would go stale here."""
    wide = replace(DEFAULT_CONFIG, maintain_band_kg=5.0)

    assert DEFAULT_CONFIG.goal_for(80.0, 77.0) is Goal.LOSE
    assert wide.goal_for(80.0, 77.0) is Goal.MAINTAIN


def test_the_band_is_published_to_the_client():
    """The client renders an optimistic preview from these constants (§5.5). A
    threshold it cannot see is one it would have to guess, and a wrong guess
    shows a preview 400 kcal away from the number the server returns."""
    payload = DEFAULT_CONFIG.to_public_dict()

    assert payload["maintainBandKg"] == DEFAULT_CONFIG.maintain_band_kg
    assert set(payload["goalAdjustmentsKcal"]) == {g.value for g in Goal}


# -- end to end through the write path --------------------------------------


@pytest.fixture
def captured(monkeypatch):
    """Capture what `save_profile` hands the repository."""
    state = SimpleNamespace(payload=None)

    monkeypatch.setattr(
        repository, "get_active_engine_config", lambda **kw: DEFAULT_CONFIG
    )

    def upsert_profile(user_id, payload):
        state.payload = payload
        return SimpleNamespace(
            id="p1",
            onboardedAt=None,
            updatedAt=SimpleNamespace(isoformat=lambda: "2026-08-28T12:00:00+00:00"),
            weightUnit=payload["weightUnit"],
            heightUnit=payload["heightUnit"],
            goal=payload["goal"],
            **{k: payload[k] for k in PROFILE},
        )

    monkeypatch.setattr(repository, "upsert_profile", upsert_profile)
    return state


@pytest.mark.parametrize(
    "target,expected",
    [(72.0, "LOSE"), (80.0, "MAINTAIN"), (88.0, "GAIN")],
)
def test_the_derived_goal_is_what_gets_persisted(captured, target, expected):
    response = services.save_profile("u1", dict(PROFILE, targetWeightKg=target))

    assert captured.payload["goal"] == expected
    # And it comes back on the wire: the client still renders the goal, it just
    # no longer supplies it.
    assert response["profile"]["goal"] == expected


def test_a_submitted_goal_never_survives(captured):
    """Belt and braces on the serializer having dropped the field: even if one
    reached this far, the target weight is what decides."""
    services.save_profile("u1", dict(PROFILE, goal="GAIN", targetWeightKg=72.0))

    assert captured.payload["goal"] == "LOSE"


def test_the_goal_and_target_weight_can_no_longer_disagree(captured):
    """The advisory this replaces (`goal_target_weight_conflict`) existed to
    reconcile exactly this pair. Nothing to reconcile now."""
    response = services.save_profile("u1", dict(PROFILE, targetWeightKg=72.0))

    assert response["advisories"] == []
