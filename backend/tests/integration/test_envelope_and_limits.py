"""Error-envelope consistency across every failing path, and HTTP rate limiting."""

from __future__ import annotations

import pytest

from backend.dependencies import get_graph
from backend.tests.helpers import Api, assert_envelope

pytestmark = pytest.mark.integration


class _Boom:
    def invoke(self, input, config):
        raise RuntimeError("upstream exploded")

    def stream(self, input, config, stream_mode="messages"):
        yield from ()


async def _404(api: Api):
    return await api.client.get("/does-not-exist")


async def _405(api: Api):
    return await api.client.get("/chat")


async def _422(api: Api):
    return await api.client.post("/chat", json={"message": ""})


async def _429(api: Api):
    for _ in range(2):
        await api.chat()
    return await api.chat()


async def _502(api: Api):
    api.app.dependency_overrides[get_graph] = lambda: _Boom()
    return await api.chat()


async def _500(api: Api):
    def broken():
        raise RuntimeError("kaboom")

    api.app.dependency_overrides[get_graph] = broken
    return await api.chat()


CASES = [
    (_404, 404, "not_found"),
    (_405, 405, "method_not_allowed"),
    (_422, 422, "validation_error"),
    (_429, 429, "rate_limited"),
    (_502, 502, "upstream_error"),
    (_500, 500, "internal_error"),
]


@pytest.mark.parametrize("scenario, status, code", CASES, ids=[f"{c[1]}-{c[2]}" for c in CASES])
async def test_envelope_shape_everywhere(app_with, scenario, status: int, code: str) -> None:
    async with app_with(limits={"chat": "2/60"} if status == 429 else None) as api:
        r = await scenario(api)
    assert_envelope(r, status, code)
    if status == 405:
        assert r.headers.get("allow")


ORIGIN = "http://localhost:3000"


@pytest.mark.parametrize("scenario, status, code", CASES, ids=[f"{c[1]}-{c[2]}" for c in CASES])
async def test_error_responses_carry_cors_headers(app_with, scenario, status: int, code: str) -> None:
    # A browser frontend can only read the envelope if the error response has ACAO.
    # Includes 500 internal_error (BUG-2 regression guard: UnhandledErrorMiddleware sits inside CORS).
    async with app_with(limits={"chat": "2/60"} if status == 429 else None) as api:
        api.client.headers["Origin"] = ORIGIN
        r = await scenario(api)
    assert_envelope(r, status, code)
    assert r.headers.get("access-control-allow-origin") == ORIGIN


# ------------------------------------------------------------------ rate limiting


async def test_chat_rate_limit_per_client(app_with) -> None:
    async with app_with(limits={"chat": "2/60"}) as api:
        assert (await api.chat()).status_code == 200
        assert (await api.chat()).status_code == 200
        r = await api.chat()
        assert_envelope(r, 429, "rate_limited")
        assert 1 <= int(r.headers["retry-after"]) <= 60


async def test_schema_invalid_requests_count_towards_the_limit(app_with) -> None:
    async with app_with(limits={"chat": "2/60"}) as api:
        assert (await api.client.post("/chat", json={})).status_code == 422
        assert (await api.client.post("/chat")).status_code == 422  # no body at all
        assert_envelope(await api.chat(), 429, "rate_limited")


async def test_unparseable_json_is_422_json_invalid_and_not_counted(app_with) -> None:
    # Unparseable JSON is rejected before route dependencies run, so it does NOT consume
    # the limit; the next well-formed requests are still allowed.
    async with app_with(limits={"chat": "2/60"}) as api:
        headers = {"content-type": "application/json"}
        for _ in range(5):
            r = await api.client.post("/chat", content=b"{bad", headers=headers)
            err = assert_envelope(r, 422, "validation_error")
            assert [d["type"] for d in err["details"]] == ["json_invalid"]
        ok = [await api.chat() for _ in range(2)]
        assert [r.status_code for r in ok] == [200, 200]
        assert_envelope(await api.chat(), 429, "rate_limited")


async def test_fresh_app_has_fresh_limiter(app_with) -> None:
    for _ in range(2):
        async with app_with(limits={"chat": "1/60"}) as api:
            assert (await api.chat()).status_code == 200
