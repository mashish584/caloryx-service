"""Display-unit preferences - PRD §5.2.

Values are always metric on the wire and in the database; these fields record
only how the client renders them. The point of the pair is that a single
METRIC/IMPERIAL flag could not express kg + ft/in.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from common.management.commands.backfill_unit_preferences import UNIT_SYSTEM_TO_PAIR
from engine.enums import HeightUnit, UnitSystem, WeightUnit
from onboarding import repository, services
from tests.support import dob_for_age

PROFILE = {
    "sexAtBirth": "FEMALE",
    "dateOfBirth": dob_for_age(30),
    "weightKg": 68.0,
    "heightCm": 165.0,
    "goal": "LOSE",
    "activityLevel": "MODERATE",
}


@pytest.fixture
def seam(monkeypatch):
    """Capture what `save_profile` hands the repository."""
    captured = {}

    def upsert_profile(user_id, payload):
        captured["payload"] = payload
        return SimpleNamespace(
            id="p1",
            targetWeightKg=payload.get("targetWeightKg"),
            weightUnit=payload["weightUnit"],
            heightUnit=payload["heightUnit"],
            onboardedAt=None,
            updatedAt=SimpleNamespace(isoformat=lambda: "2026-08-26T12:00:00+00:00"),
            **{k: payload[k] for k in PROFILE},
        )

    monkeypatch.setattr(repository, "upsert_profile", upsert_profile)
    return captured


# -- the backfill mapping --------------------------------------------------


def test_every_legacy_unit_system_maps_to_a_pair():
    """A missed member would silently leave those users on the column defaults."""
    assert set(UNIT_SYSTEM_TO_PAIR) == {u.value for u in UnitSystem}


def test_the_legacy_mapping_is_the_obvious_one():
    assert UNIT_SYSTEM_TO_PAIR[UnitSystem.METRIC.value] == ("KG", "CM")
    assert UNIT_SYSTEM_TO_PAIR[UnitSystem.IMPERIAL.value] == ("LB", "FT_IN")


def test_the_mapping_only_produces_real_units():
    for weight, height in UNIT_SYSTEM_TO_PAIR.values():
        assert weight in {u.value for u in WeightUnit}
        assert height in {u.value for u in HeightUnit}


# -- the nested-to-flat seam -----------------------------------------------


@pytest.mark.parametrize(
    "weight,height",
    [("KG", "CM"), ("LB", "FT_IN"), ("KG", "FT_IN"), ("LB", "CM")],
)
def test_every_combination_reaches_the_columns(seam, weight, height):
    """All four pairs survive; the retired flag could hold only two of them."""
    result = services.save_profile(
        "u1", dict(PROFILE, preferredUnits={"weight": weight, "height": height})
    )

    assert seam["payload"]["weightUnit"] == weight
    assert seam["payload"]["heightUnit"] == height
    assert result["profile"]["preferredUnits"] == {"weight": weight, "height": height}


def test_the_nested_key_never_reaches_prisma(seam):
    """`upsert_profile` spreads its payload straight into Prisma, so a nested
    dict would be handed to a column that does not exist."""
    services.save_profile(
        "u1", dict(PROFILE, preferredUnits={"weight": "LB", "height": "CM"})
    )
    assert "preferredUnits" not in seam["payload"]


def test_units_default_to_metric_when_absent(seam):
    services.save_profile("u1", dict(PROFILE))
    assert seam["payload"]["weightUnit"] == "KG"
    assert seam["payload"]["heightUnit"] == "CM"


def test_units_are_a_display_choice_only(seam):
    """Choosing pounds must not change the stored measurements - the client
    converts before it posts, so the wire is metric either way."""
    services.save_profile(
        "u1", dict(PROFILE, preferredUnits={"weight": "LB", "height": "FT_IN"})
    )
    assert seam["payload"]["weightKg"] == 68.0
    assert seam["payload"]["heightCm"] == 165.0
