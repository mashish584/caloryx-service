"""Lifecycle of the shared Prisma client in `common.db`.

prisma-client-py attaches a new engine to the client *before* starting it, and
when startup fails the engine kills its process and closes its HTTP session but
stays attached. `is_connected()` only checks that an engine is attached, so
`get_client()` used to hand that dead engine to every later request, which then
failed with `HTTPClientClosedError` until the process restarted.

The fakes below mirror that behaviour. They do not reimplement Prisma, which
needs a query engine binary and a database to connect for real.
"""
from __future__ import annotations

import pytest

from common import db


class FakeSession:
    def __init__(self) -> None:
        self.closed = False


class FakeProcess:
    def __init__(self) -> None:
        self.returncode = None

    def poll(self):
        return self.returncode


class FakeEngine:
    def __init__(self) -> None:
        self.process = None
        self.session = FakeSession()

    def start(self) -> None:
        self.process = FakeProcess()

    def close(self) -> None:
        self.process = None
        self.session.closed = True


class FakePrisma:
    fail_next_connects = 0

    def __init__(self, *, auto_register: bool) -> None:
        self._internal_engine = None
        self.connect_calls = 0

    def is_connected(self) -> bool:
        return self._internal_engine is not None

    def connect(self, timeout) -> None:
        self.connect_calls += 1
        # Same order as prisma's SyncBasePrisma.connect(): attach, then start.
        self._internal_engine = FakeEngine()
        if FakePrisma.fail_next_connects:
            FakePrisma.fail_next_connects -= 1
            self._internal_engine.close()
            raise ConnectionError("Could not connect to the query engine")
        self._internal_engine.start()

    def disconnect(self) -> None:
        engine, self._internal_engine = self._internal_engine, None
        if engine is not None:
            engine.close()


@pytest.fixture(autouse=True)
def fake_prisma(monkeypatch):
    FakePrisma.fail_next_connects = 0
    monkeypatch.setattr(db, "_import_prisma", lambda: FakePrisma)
    monkeypatch.setattr(db, "_client", None)
    yield
    monkeypatch.setattr(db, "_client", None)


def test_healthy_client_is_reused_without_reconnecting():
    first = db.get_client()
    second = db.get_client()

    assert first is second
    assert first.connect_calls == 1


def test_failed_connect_is_retried_rather_than_returning_a_dead_client():
    FakePrisma.fail_next_connects = 1

    with pytest.raises(ConnectionError):
        db.get_client()

    client = db.get_client()

    assert client.connect_calls == 2
    assert db._is_usable(client)


def test_closed_session_is_discarded_and_reconnected():
    client = db.get_client()
    dead = client._internal_engine
    dead.session.closed = True  # what prisma's atexit engine.stop() leaves behind

    assert db.get_client() is client
    assert client._internal_engine is not dead
    assert client.connect_calls == 2


def test_exited_engine_process_is_discarded_and_reconnected():
    client = db.get_client()
    client._internal_engine.process.returncode = 1  # engine crashed on its own

    db.get_client()

    assert client.connect_calls == 2
    assert db._is_usable(client)
