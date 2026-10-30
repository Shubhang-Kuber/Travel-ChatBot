"""Mongo liveness helpers with a fake client (no server): ping timeout, startup retry/fail-loud."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from backend.db import create_client, ping, wait_for_mongo
from backend.tests.helpers import make_settings

pytestmark = pytest.mark.unit


class FakeClient:
    def __init__(self, failures: int = 0, hang: bool = False) -> None:
        self.failures = failures
        self.hang = hang
        self.calls = 0
        self.admin = SimpleNamespace(command=self._command)

    async def _command(self, name: str):
        assert name == "ping"
        self.calls += 1
        if self.hang:
            await asyncio.sleep(3600)
        if self.calls <= self.failures:
            raise ConnectionError("down")
        return {"ok": 1}


async def test_ping_up() -> None:
    assert await ping(FakeClient()) is True


async def test_ping_error_is_down() -> None:
    assert await ping(FakeClient(failures=1)) is False


async def test_ping_hang_is_down_within_timeout() -> None:
    t0 = time.perf_counter()
    assert await ping(FakeClient(hang=True), timeout=0.2) is False
    assert time.perf_counter() - t0 < 1.0


async def test_wait_for_mongo_retries_until_up() -> None:
    c = FakeClient(failures=2)
    await wait_for_mongo(c, timeout_seconds=10)
    assert c.calls == 3


async def test_wait_for_mongo_fails_loudly_after_timeout() -> None:
    t0 = time.perf_counter()
    with pytest.raises(RuntimeError, match="MongoDB unreachable"):
        await wait_for_mongo(FakeClient(failures=10**6), timeout_seconds=1.0)
    assert time.perf_counter() - t0 < 3.0


async def test_wait_for_mongo_hanging_server_still_times_out() -> None:
    with pytest.raises(RuntimeError):
        await wait_for_mongo(FakeClient(hang=True), timeout_seconds=0.5)


async def test_create_client_is_lazy_and_tz_aware() -> None:
    # Constructing must not connect (no server here); it is closed without ever being used.
    client = create_client(make_settings(mongo_uri="mongodb://127.0.0.1:1/?directConnection=true"))
    try:
        assert client.codec_options.tz_aware is True
    finally:
        await client.close()
