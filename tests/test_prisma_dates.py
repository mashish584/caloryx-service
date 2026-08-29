"""Dates across the Prisma boundary.

A `datetime.date` is not serializable by prisma-client-py: its query builder
dispatches on type and registers `datetime.datetime` but not `datetime.date`, so
a bare date raises `TypeError` and the write surfaces as a 500. That is exactly
what happened to `POST /onboarding/profile` once `dateOfBirth` replaced the
stored `age`, and nothing here caught it - the repository seams the other test
modules use never hand a payload to Prisma's serializer.

So this file owns the boundary itself: what we send must be something Prisma can
render, and what Prisma returns must render as the date the API documents.
"""
from __future__ import annotations

import os
import time as _time
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

# Private module, deliberately. It holds the serializer that actually rejected
# the payload, and asserting against the real one is the entire point - a
# reimplementation here would have agreed with the broken code. `prisma` is
# pinned to a minor range (requirements.txt), so this import is stable.
from prisma._builder import dumps

from common.db import from_prisma_date, to_prisma_date
from onboarding import repository, services
from onboarding.serializers import serialize_profile
from tests.support import dob_for_age

DOB = date(1998, 2, 15)

PROFILE = {
    "sexAtBirth": "MALE",
    "dateOfBirth": DOB,
    "weightKg": 77.0,
    "heightCm": 175.0,
    "targetWeightKg": 70.0,
    "goal": "LOSE",
    "activityLevel": "MODERATE",
}


@pytest.fixture
def written(monkeypatch):
    """Capture the payload `save_profile` hands the repository."""
    captured = {}

    def upsert_profile(user_id, payload):
        captured["payload"] = payload
        # Shaped like a real Prisma row: `dateOfBirth` comes back as a
        # `datetime`, which is what the read side actually has to cope with.
        return SimpleNamespace(
            id="p1",
            onboardedAt=None,
            updatedAt=datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc),
            weightUnit=payload["weightUnit"],
            heightUnit=payload["heightUnit"],
            **{k: payload[k] for k in PROFILE},
        )

    monkeypatch.setattr(repository, "upsert_profile", upsert_profile)
    return captured


def prisma_profile(**overrides):
    """A profile row shaped the way Prisma returns one."""
    fields = {
        "id": "p1",
        "sexAtBirth": "MALE",
        "dateOfBirth": datetime(1998, 2, 15, 0, 0, tzinfo=timezone.utc),
        "weightKg": 77.0,
        "heightCm": 175.0,
        "targetWeightKg": 70.0,
        "goal": "LOSE",
        "activityLevel": "MODERATE",
        "weightUnit": "KG",
        "heightUnit": "CM",
        "onboardedAt": None,
        "updatedAt": datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc),
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


# -- the conversion helpers ------------------------------------------------


def test_a_date_becomes_something_prisma_can_serialize():
    """The bug itself: a bare `date` raises, a converted one does not."""
    with pytest.raises(TypeError):
        dumps({"dateOfBirth": DOB})

    assert dumps({"dateOfBirth": to_prisma_date(DOB)})


def test_conversion_round_trips_the_calendar_day():
    assert from_prisma_date(to_prisma_date(DOB)) == DOB


def test_nulls_survive_both_directions():
    """`dateOfBirth` is nullable until `backfill_date_of_birth` has run."""
    assert to_prisma_date(None) is None
    assert from_prisma_date(None) is None


def test_a_datetime_passes_through_unchanged():
    already = datetime(1998, 2, 15, tzinfo=timezone.utc)
    assert to_prisma_date(already) is already


def test_the_stored_instant_is_utc_midnight():
    """Prisma reads a naive datetime as UTC, so pinning it here is what keeps
    the day submitted and the day stored the same."""
    converted = to_prisma_date(DOB)

    assert converted.utcoffset().total_seconds() == 0
    assert (converted.hour, converted.minute, converted.second) == (0, 0, 0)


# -- the off-by-one ---------------------------------------------------------


@pytest.fixture
def west_of_utc():
    """Run the body in a negative-offset zone.

    The conversion must not consult local time in either direction. If it did,
    UTC midnight would land on the previous day here and every birth date would
    shift back one - taking the derived age with it on a birthday. A machine in
    UTC or IST cannot show that, so the zone is forced.
    """
    before = os.environ.get("TZ")
    os.environ["TZ"] = "America/Los_Angeles"
    _time.tzset()
    yield
    if before is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = before
    _time.tzset()


def test_the_calendar_day_holds_west_of_utc(west_of_utc):
    assert to_prisma_date(DOB).date() == DOB
    assert from_prisma_date(datetime(1998, 2, 15, 0, 0, tzinfo=timezone.utc)) == DOB
    assert serialize_profile(prisma_profile())["dateOfBirth"] == "1998-02-15"


# -- the write path ---------------------------------------------------------


def test_the_upsert_payload_is_serializable_by_prisma(written):
    """The general net. `upsert_profile` spreads this dict straight into Prisma,
    so any value Prisma cannot render is a 500 - not just this one field."""
    services.save_profile("u1", dict(PROFILE))

    assert dumps(written["payload"])


def test_the_birth_date_reaches_prisma_as_a_datetime(written):
    services.save_profile("u1", dict(PROFILE))
    stored = written["payload"]["dateOfBirth"]

    assert isinstance(stored, datetime)
    assert stored.date() == DOB


def test_a_profile_without_a_birth_date_still_writes(written):
    """Not reachable through the serializer, which requires one - but
    `save_profile` must not crash on a payload assembled elsewhere."""
    services.save_profile("u1", dict(PROFILE, dateOfBirth=None))

    assert written["payload"]["dateOfBirth"] is None
    assert dumps(written["payload"])


# -- the read path ----------------------------------------------------------


def test_a_stored_row_renders_as_a_plain_date(written):
    """Prisma hands back a `datetime` even for a `@db.Date` column. Rendering it
    raw emits a full timestamp for a field published as a `DateField`, which a
    client parsing a date would choke on."""
    result = services.save_profile("u1", dict(PROFILE))

    assert result["profile"]["dateOfBirth"] == "1998-02-15"


def test_serialize_profile_accepts_both_shapes():
    """A real row carries a `datetime`; the repository seams elsewhere in this
    suite carry a `date`. Both must render identically."""
    as_datetime = serialize_profile(prisma_profile())["dateOfBirth"]
    as_date = serialize_profile(prisma_profile(dateOfBirth=DOB))["dateOfBirth"]

    assert as_datetime == as_date == "1998-02-15"


def test_a_null_birth_date_renders_as_null():
    assert serialize_profile(prisma_profile(dateOfBirth=None))["dateOfBirth"] is None


def test_age_derives_correctly_from_a_prisma_datetime():
    """`_profile_age` reads the same `datetime` the read path gets."""
    thirty = dob_for_age(30)
    row = prisma_profile(
        dateOfBirth=datetime(thirty.year, thirty.month, thirty.day, tzinfo=timezone.utc)
    )

    assert services._profile_age(row) == 30


# -- the backfill command ---------------------------------------------------


def test_the_backfill_writes_a_serializable_birth_date(monkeypatch):
    """`backfill_date_of_birth` reconstructs a `date` and writes it directly,
    bypassing the service layer - so it hit the same `TypeError` and had never
    once run to completion. Covered here rather than against Postgres: the
    command only reaches this line for rows that predate the column."""
    from common.management.commands import backfill_date_of_birth as cmd

    writes = []

    class FakeProfile:
        def find_many(self, where):
            return [
                SimpleNamespace(
                    id="p1",
                    age=30,
                    updatedAt=datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc),
                )
            ]

        def update(self, where, data):
            writes.append(data)

    monkeypatch.setattr(
        cmd, "get_client", lambda: SimpleNamespace(profile=FakeProfile())
    )
    cmd.Command().handle(dry_run=False)

    assert len(writes) == 1
    assert isinstance(writes[0]["dateOfBirth"], datetime)
    assert writes[0]["dateOfBirth"].date() == date(1996, 8, 26)
    assert dumps(writes[0])
