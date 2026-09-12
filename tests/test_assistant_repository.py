"""`try_consume_quota`'s check-and-consume logic (§12.8) against a fake
Prisma client that mirrors the real client's shape: `update_many` returns a
plain `int` row count (see `.venv/lib/python3.12/site-packages/prisma/actions.py`),
not a `{count: int}` object like Prisma's JS client. A prior version of
`try_consume_quota` read `.count` off that int and crashed with
`AttributeError: 'int' object has no attribute 'count'` on every call -
untested because other suites monkeypatch `try_consume_quota` itself rather
than exercising it against something shaped like the real client.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from assistant import repository


@dataclass
class FakeCounter:
    userId: str
    windowStart: datetime
    count: int


class FakeAiQuotaCounter:
    def __init__(self) -> None:
        self.rows: dict[str, FakeCounter] = {}

    def find_unique(self, where):
        return self.rows.get(where["userId"])

    def create(self, data):
        row = FakeCounter(userId=data["userId"], windowStart=data["windowStart"], count=data["count"])
        self.rows[row.userId] = row
        return row

    def update_many(self, where, data) -> int:
        row = self.rows.get(where["userId"])
        if row is None:
            return 0

        if "gt" in where.get("windowStart", {}) and row.windowStart <= where["windowStart"]["gt"]:
            return 0
        if "lte" in where.get("windowStart", {}) and row.windowStart > where["windowStart"]["lte"]:
            return 0
        if "count" in where and "lt" in where["count"] and not row.count < where["count"]["lt"]:
            return 0

        count_data = data.get("count")
        if isinstance(count_data, dict) and "increment" in count_data:
            row.count += count_data["increment"]
        elif isinstance(count_data, int):
            row.count = count_data
        if "windowStart" in data:
            row.windowStart = data["windowStart"]
        return 1


@dataclass
class FakeClient:
    aiquotacounter: FakeAiQuotaCounter = field(default_factory=FakeAiQuotaCounter)


@pytest.fixture
def fake_client(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(repository, "get_client", lambda: client)
    return client


def _now():
    return datetime.now(timezone.utc)


def test_first_call_ever_creates_counter(fake_client):
    counter, consumed = repository.try_consume_quota("u1", limit=5, window=timedelta(hours=1))

    assert consumed is True
    assert counter.count == 1
    assert fake_client.aiquotacounter.rows["u1"].count == 1


def test_within_window_under_limit_increments(fake_client):
    fake_client.aiquotacounter.rows["u1"] = FakeCounter(userId="u1", windowStart=_now(), count=2)

    counter, consumed = repository.try_consume_quota("u1", limit=5, window=timedelta(hours=1))

    assert consumed is True
    assert counter.count == 3


def test_within_window_at_limit_is_exhausted(fake_client):
    fake_client.aiquotacounter.rows["u1"] = FakeCounter(userId="u1", windowStart=_now(), count=5)

    counter, consumed = repository.try_consume_quota("u1", limit=5, window=timedelta(hours=1))

    assert consumed is False
    assert counter.count == 5


def test_expired_window_resets_counter(fake_client):
    stale_start = _now() - timedelta(hours=2)
    fake_client.aiquotacounter.rows["u1"] = FakeCounter(userId="u1", windowStart=stale_start, count=5)

    counter, consumed = repository.try_consume_quota("u1", limit=5, window=timedelta(hours=1))

    assert consumed is True
    assert counter.count == 1
    assert counter.windowStart > stale_start
