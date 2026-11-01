"""Shared test helpers: settings, app/client builders, envelope assertions.

Everything here goes through public surfaces only: ``Settings(...)``, ``create_app``
and HTTP.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient, Response

from backend.config import RateLimit, Settings
from backend.main import create_app

GENEROUS = RateLimit(10_000, 60)


def make_settings(limits: dict[str, str] | None = None, **overrides: Any) -> Settings:
    """Test settings. ``limits`` overrides buckets with "N/seconds" strings; the rest are generous."""
    rate_limits = {"chat": GENEROUS}
    for bucket, raw in (limits or {}).items():
        rate_limits[bucket] = RateLimit.parse(raw)
    base: dict[str, Any] = dict(rate_limits=rate_limits)
    base.update(overrides)
    return Settings(**base)


def client_for(app: Any) -> AsyncClient:
    # raise_app_exceptions=False: behave like a real server (500 body, not a re-raise).
    return AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://testserver")


class Api:
    """Thin HTTP helper bound to one running app."""

    def __init__(self, app: Any, client: AsyncClient) -> None:
        self.app = app
        self.client = client

    async def chat(self, message: str = "Plan 3 days in Lisbon", thread_id: str | None = None) -> Response:
        body: dict[str, Any] = {"message": message}
        if thread_id is not None:
            body["thread_id"] = thread_id
        return await self.client.post("/chat", json=body)


@asynccontextmanager
async def running_app(settings: Settings) -> AsyncIterator[Api]:
    """A fresh app (fresh rate limiter, fresh mock graph) with its lifespan running."""
    app = create_app(settings)
    async with LifespanManager(app, startup_timeout=60, shutdown_timeout=10) as manager:
        async with client_for(manager.app) as client:
            yield Api(app, client)


def assert_envelope(resp: Response, status: int, code: str) -> dict[str, Any]:
    """Exact error envelope shape per CONTRACT.md. Returns the inner error object."""
    assert resp.status_code == status, (resp.status_code, resp.text)
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert isinstance(body, dict) and set(body) == {"error"}, body
    err = body["error"]
    assert isinstance(err, dict) and set(err) == {"code", "message", "details"}, err
    assert err["code"] == code, err
    assert isinstance(err["message"], str) and err["message"], err
    if code == "validation_error":
        assert isinstance(err["details"], list) and err["details"], err
        for d in err["details"]:
            assert set(d) == {"loc", "msg", "type"}, d
            assert isinstance(d["loc"], list) and isinstance(d["msg"], str) and isinstance(d["type"], str)
    else:
        assert err["details"] is None, err
    if status == 429:
        retry = resp.headers.get("retry-after")
        assert retry is not None and retry.isdigit() and int(retry) >= 1, retry
    return err


def parse_sse(raw: str) -> list[tuple[str, Any]]:
    """Strict SSE parser for our framing: each event is exactly ``event: X`` + ``data: <one
    JSON value>`` + blank line. Fails on anything else (e.g. a raw newline inside data)."""
    import json

    assert raw.endswith("\n\n"), repr(raw[-40:])
    events: list[tuple[str, Any]] = []
    for block in raw[:-2].split("\n\n"):
        lines = block.split("\n")
        assert len(lines) == 2, f"bad SSE block: {block!r}"
        ev, data = lines
        assert ev.startswith("event: ") and data.startswith("data: "), block
        events.append((ev[len("event: ") :], json.loads(data[len("data: ") :])))
    return events
