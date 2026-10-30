"""Error-envelope consistency across every failing path, and HTTP rate limiting."""

from __future__ import annotations

import pytest

from backend.dependencies import get_graph
from backend.tests.helpers import PASSWORD, Api, assert_envelope, bearer, unique_email

pytestmark = pytest.mark.integration


class _Boom:
    def invoke(self, input, config):
        raise RuntimeError("upstream exploded")

    def stream(self, input, config, stream_mode="messages"):
        yield from ()


async def _401_missing(api: Api):
    return await api.client.post("/chat", json={"message": "hi"})


async def _401_invalid(api: Api):
    return await api.client.post("/chat", json={"message": "hi"}, headers=bearer("nope"))


async def _401_credentials(api: Api):
    return await api.client.post("/auth/login", json={"email": "ghost@example.com", "password": PASSWORD})


async def _401_refresh(api: Api):
    return await api.client.post("/auth/refresh", json={"refresh_token": "x.y.z"})


async def _404(api: Api):
    return await api.client.get("/does-not-exist")


async def _405(api: Api):
    return await api.client.get("/auth/login")


async def _409(api: Api):
    u = await api.signup()
    return await api.client.post("/auth/signup", json={"email": u["email"], "password": PASSWORD})


async def _422(api: Api):
    return await api.client.post("/auth/signup", json={"email": "bad", "password": "x"})


async def _429(api: Api):
    for _ in range(2):
        await api.client.post("/auth/login", json={"email": "a@example.com", "password": "pw"})
    return await api.client.post("/auth/login", json={"email": "a@example.com", "password": "pw"})


async def _502(api: Api):
    _, t = await api.new_user()
    api.app.dependency_overrides[get_graph] = lambda: _Boom()
    return await api.chat(t["access_token"])


async def _500(api: Api):
    _, t = await api.new_user()

    def broken():
        raise RuntimeError("kaboom")

    api.app.dependency_overrides[get_graph] = broken
    return await api.chat(t["access_token"])


CASES = [
    (_401_missing, 401, "not_authenticated"),
    (_401_invalid, 401, "invalid_token"),
    (_401_credentials, 401, "invalid_credentials"),
    (_401_refresh, 401, "invalid_token"),
    (_404, 404, "not_found"),
    (_405, 405, "method_not_allowed"),
    (_409, 409, "email_taken"),
    (_422, 422, "validation_error"),
    (_429, 429, "rate_limited"),
    (_502, 502, "upstream_error"),
    (_500, 500, "internal_error"),
]


@pytest.mark.parametrize("scenario, status, code", CASES, ids=[f"{c[1]}-{c[2]}" for c in CASES])
async def test_envelope_shape_everywhere(app_with, scenario, status: int, code: str) -> None:
    async with app_with(limits={"login": "2/60"} if status == 429 else None) as api:
        r = await scenario(api)
    assert_envelope(r, status, code)
    if status == 405:
        assert r.headers.get("allow")


ORIGIN = "http://localhost:3000"
@pytest.mark.parametrize("scenario, status, code", CASES, ids=[f"{c[1]}-{c[2]}" for c in CASES])
async def test_error_responses_carry_cors_headers(app_with, scenario, status: int, code: str) -> None:
    # A browser frontend can only read the envelope if the error response has ACAO.
    # Includes 500 internal_error (BUG-2 regression guard: UnhandledErrorMiddleware sits inside CORS).
    async with app_with(limits={"login": "2/60"} if status == 429 else None) as api:
        api.client.headers["Origin"] = ORIGIN
        r = await scenario(api)
    assert_envelope(r, status, code)
    assert r.headers.get("access-control-allow-origin") == ORIGIN


# ------------------------------------------------------------------ rate limiting


async def test_login_rate_limit(app_with) -> None:
    async with app_with(limits={"login": "3/60"}) as api:
        u = await api.signup()
        for _ in range(3):
            assert (await api.client.post("/auth/login", json={"email": u["email"], "password": PASSWORD})).status_code == 200
        r = await api.client.post("/auth/login", json={"email": u["email"], "password": PASSWORD})
        assert_envelope(r, 429, "rate_limited")
        assert 1 <= int(r.headers["retry-after"]) <= 60
        # other buckets on the same IP are unaffected
        assert (await api.client.post("/auth/signup", json={"email": unique_email(), "password": PASSWORD})).status_code == 201


async def test_schema_invalid_requests_count_towards_the_limit(app_with) -> None:
    async with app_with(limits={"login": "2/60"}) as api:
        assert (await api.client.post("/auth/login", json={})).status_code == 422
        assert (await api.client.post("/auth/login")).status_code == 422  # no body at all
        r = await api.client.post("/auth/login", json={"email": "a@example.com", "password": PASSWORD})
        assert_envelope(r, 429, "rate_limited")


@pytest.mark.parametrize("path", ["/auth/login", "/chat"])
async def test_unparseable_json_is_422_json_invalid_and_not_counted(app_with, path: str) -> None:
    # CONTRACT (BUG-1 ruling): unparseable JSON is rejected before route dependencies run,
    # so it does NOT consume the limit; the next well-formed requests are still allowed.
    async with app_with(limits={"login": "2/60", "chat": "2/60"}) as api:
        headers = {"content-type": "application/json"}
        if path == "/chat":
            _, t = await api.new_user()
            headers.update(bearer(t["access_token"]))
        for _ in range(5):
            r = await api.client.post(path, content=b"{bad", headers=headers)
            err = assert_envelope(r, 422, "validation_error")
            assert [d["type"] for d in err["details"]] == ["json_invalid"]
        if path == "/chat":
            ok = [await api.client.post(path, json={"message": "hi"}, headers=headers) for _ in range(2)]
            assert [r.status_code for r in ok] == [200, 200]
            assert_envelope(await api.client.post(path, json={"message": "hi"}, headers=headers), 429, "rate_limited")
        else:
            body = {"email": "a@example.com", "password": PASSWORD}
            ok = [await api.client.post(path, json=body) for _ in range(2)]
            assert [r.status_code for r in ok] == [401, 401]
            assert_envelope(await api.client.post(path, json=body), 429, "rate_limited")


async def test_signup_rate_limit(app_with) -> None:
    async with app_with(limits={"signup": "2/60"}) as api:
        await api.signup()
        await api.signup()
        r = await api.client.post("/auth/signup", json={"email": unique_email(), "password": PASSWORD})
        assert_envelope(r, 429, "rate_limited")


async def test_refresh_and_logout_share_a_counter(app_with) -> None:
    async with app_with(limits={"refresh": "2/60"}) as api:
        _, t = await api.new_user()
        t2 = (await api.client.post("/auth/refresh", json={"refresh_token": t["refresh_token"]})).json()
        assert (await api.client.post("/auth/logout", json={"refresh_token": t2["refresh_token"]})).status_code == 204
        r = await api.client.post("/auth/refresh", json={"refresh_token": t2["refresh_token"]})
        assert_envelope(r, 429, "rate_limited")


async def test_chat_rate_limit_is_per_user(app_with) -> None:
    async with app_with(limits={"chat": "2/60"}) as api:
        _, alice = await api.new_user()
        _, bob = await api.new_user()
        assert (await api.chat(alice["access_token"])).status_code == 200
        assert (await api.chat(alice["access_token"])).status_code == 200
        r = await api.chat(alice["access_token"])
        assert_envelope(r, 429, "rate_limited")
        # same IP, different user: own budget
        assert (await api.chat(bob["access_token"])).status_code == 200
        # a second access token for alice shares her budget (keyed by user id, not token)
        alice2 = (await api.client.post("/auth/refresh", json={"refresh_token": alice["refresh_token"]})).json()
        assert (await api.chat(alice2["access_token"])).status_code == 429


async def test_unauthenticated_chat_is_401_not_429(app_with) -> None:
    async with app_with(limits={"chat": "1/60"}) as api:
        for _ in range(3):
            assert (await api.client.post("/chat", json={"message": "hi"})).status_code == 401


async def test_fresh_app_has_fresh_limiter(app_with) -> None:
    for _ in range(2):
        async with app_with(limits={"login": "1/60"}) as api:
            r = await api.client.post("/auth/login", json={"email": "a@example.com", "password": PASSWORD})
            assert r.status_code == 401
