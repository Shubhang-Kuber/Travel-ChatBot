"""Sliding-window limiter: window, Retry-After, per-key/bucket isolation, reset, 429 envelope."""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI, Request

from backend.errors import install_exception_handlers
from backend.ratelimit import RateLimiter, client_ip, rate_limit
from backend.tests.helpers import assert_envelope, client_for, make_settings

pytestmark = pytest.mark.unit


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_allows_up_to_max_then_blocks() -> None:
    clock = FakeClock()
    rl = RateLimiter(clock=clock)
    assert [rl.hit("b", "k", 3, 60) for _ in range(3)] == [None, None, None]
    assert rl.hit("b", "k", 3, 60) == pytest.approx(60)


def test_retry_after_is_time_until_oldest_hit_leaves_window() -> None:
    clock = FakeClock(0)
    rl = RateLimiter(clock=clock)
    rl.hit("b", "k", 2, 60)
    clock.t = 10
    rl.hit("b", "k", 2, 60)
    clock.t = 20
    assert rl.hit("b", "k", 2, 60) == pytest.approx(40)


def test_window_slides() -> None:
    clock = FakeClock(0)
    rl = RateLimiter(clock=clock)
    rl.hit("b", "k", 2, 60)
    clock.t = 30
    rl.hit("b", "k", 2, 60)
    clock.t = 59.9
    assert rl.hit("b", "k", 2, 60) is not None
    clock.t = 60.0  # first hit (t=0) leaves the window exactly now
    assert rl.hit("b", "k", 2, 60) is None
    clock.t = 60.1
    assert rl.hit("b", "k", 2, 60) is not None  # t=30 and t=60 still inside


def test_blocked_requests_do_not_extend_the_window() -> None:
    clock = FakeClock(0)
    rl = RateLimiter(clock=clock)
    rl.hit("b", "k", 1, 10)
    for t in (1, 5, 9):
        clock.t = t
        assert rl.hit("b", "k", 1, 10) is not None
    clock.t = 10
    assert rl.hit("b", "k", 1, 10) is None


def test_keys_isolated() -> None:
    rl = RateLimiter(clock=FakeClock())
    rl.hit("b", "alice", 1, 60)
    assert rl.hit("b", "alice", 1, 60) is not None
    assert rl.hit("b", "bob", 1, 60) is None


def test_buckets_isolated() -> None:
    rl = RateLimiter(clock=FakeClock())
    rl.hit("login", "1.2.3.4", 1, 60)
    assert rl.hit("login", "1.2.3.4", 1, 60) is not None
    assert rl.hit("signup", "1.2.3.4", 1, 60) is None


def test_reset_clears_everything() -> None:
    rl = RateLimiter(clock=FakeClock())
    rl.hit("b", "k", 1, 60)
    rl.hit("c", "k", 1, 60)
    rl.reset()
    assert rl.hit("b", "k", 1, 60) is None
    assert rl.hit("c", "k", 1, 60) is None


def test_many_keys_still_correct_across_sweeps() -> None:
    # Enough hits to trigger internal housekeeping several times; behaviour must not change.
    clock = FakeClock(0)
    rl = RateLimiter(clock=clock)
    rl.hit("b", "victim", 1, 60)
    for i in range(5000):
        clock.t = i * 0.001
        rl.hit("b", f"ip-{i}", 1, 60)
    assert rl.hit("b", "victim", 1, 60) is not None
    clock.t = 100
    assert rl.hit("b", "victim", 1, 60) is None


# ------------------------------------------------------------------ as a FastAPI dependency


def limited_app(clock: FakeClock, limit: str = "2/60") -> FastAPI:
    app = FastAPI()
    app.state.settings = make_settings(limits={"login": limit})
    app.state.rate_limiter = RateLimiter(clock=clock)
    install_exception_handlers(app)

    def header_key(request: Request) -> str:
        return request.headers.get("x-key", "anon")

    @app.post("/by-ip", dependencies=[Depends(rate_limit("login", client_ip))])
    async def by_ip() -> dict:
        return {"ok": True}

    @app.post("/by-key", dependencies=[Depends(rate_limit("login", header_key))])
    async def by_key() -> dict:
        return {"ok": True}

    return app


async def test_dependency_returns_429_envelope_with_retry_after() -> None:
    clock = FakeClock(0)
    async with client_for(limited_app(clock)) as c:
        assert (await c.post("/by-ip")).status_code == 200
        clock.t = 15.2
        assert (await c.post("/by-ip")).status_code == 200
        clock.t = 20
        r = await c.post("/by-ip")
        assert_envelope(r, 429, "rate_limited")
        assert r.headers["retry-after"] == "40"
        clock.t = 59.9  # 0.1 s left -> rounded UP to at least 1
        r = await c.post("/by-ip")
        assert r.status_code == 429 and r.headers["retry-after"] == "1"
        clock.t = 60
        assert (await c.post("/by-ip")).status_code == 200


async def test_dependency_keys_by_key_func() -> None:
    clock = FakeClock(0)
    async with client_for(limited_app(clock, "1/60")) as c:
        assert (await c.post("/by-key", headers={"x-key": "a"})).status_code == 200
        assert (await c.post("/by-key", headers={"x-key": "a"})).status_code == 429
        assert (await c.post("/by-key", headers={"x-key": "b"})).status_code == 200


async def test_x_forwarded_for_is_not_trusted() -> None:
    clock = FakeClock(0)
    async with client_for(limited_app(clock, "1/60")) as c:
        assert (await c.post("/by-ip", headers={"X-Forwarded-For": "1.1.1.1"})).status_code == 200
        r = await c.post("/by-ip", headers={"X-Forwarded-For": "2.2.2.2"})
        assert r.status_code == 429


def test_client_ip_without_client_is_unknown() -> None:
    req = Request({"type": "http", "headers": [], "client": None})
    assert client_ip(req) == "unknown"
