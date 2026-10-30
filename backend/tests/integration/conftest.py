"""Integration fixtures: a real AsyncMongoClient on the compose mongo, test DB dropped per test."""

from __future__ import annotations

import threading
from typing import Any, AsyncIterator, Callable

import pytest
from pymongo import AsyncMongoClient, monitoring

from backend.tests.helpers import MONGO_DB, MONGO_URI, Api, make_settings, running_app


class CommandSpy(monitoring.CommandListener):
    """Records every command the driver sends: (command_name, db, target collection)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.events: list[tuple[str, str, Any]] = []
        self.recording = False

    def started(self, event: monitoring.CommandStartedEvent) -> None:
        if not self.recording:
            return
        cmd = event.command
        target = cmd.get(event.command_name)
        if event.command_name == "getMore":
            target = cmd.get("collection")
        with self._lock:
            self.events.append((event.command_name, event.database_name, target))

    def succeeded(self, event) -> None:  # noqa: D401 - required by the interface
        pass

    def failed(self, event) -> None:
        pass

    def clear(self) -> None:
        with self._lock:
            self.events.clear()

    def touching(self, collection: str) -> list[tuple[str, str, Any]]:
        return [e for e in self.events if e[2] == collection]


def _client(**kw: Any) -> AsyncMongoClient:
    return AsyncMongoClient(MONGO_URI, serverSelectionTimeoutMS=5000, tz_aware=True, **kw)


@pytest.fixture
async def spy() -> CommandSpy:
    return CommandSpy()


@pytest.fixture
async def mongo_client(spy: CommandSpy) -> AsyncIterator[AsyncMongoClient]:
    client = _client(event_listeners=[spy])
    await client.drop_database(MONGO_DB)  # isolation: every test starts from an empty DB
    yield client
    await client.close()


@pytest.fixture
def db(mongo_client: AsyncMongoClient):
    return mongo_client[MONGO_DB]


@pytest.fixture
async def api(mongo_client) -> AsyncIterator[Api]:
    async with running_app(make_settings(), mongo_client) as a:
        yield a


@pytest.fixture
def app_with(mongo_client) -> Callable[..., Any]:
    """``async with app_with(limits={...}, **settings) as api:`` for custom settings."""

    def factory(**kw: Any):
        return running_app(make_settings(**kw), mongo_client)

    return factory
