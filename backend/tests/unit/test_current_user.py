"""``get_current_user`` on a minimal app with NO database: local JWT validation only."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi import Depends, FastAPI

import backend.auth.dependencies as auth_deps
from backend.auth.dependencies import get_current_user
from backend.auth.tokens import InvalidTokenError, TokenClaims, create_access_token, create_refresh_token, utcnow
from backend.errors import install_exception_handlers
from backend.models.user import CurrentUser
from backend.tests.helpers import OTHER_SECRET, USER_ID, assert_envelope, bearer, client_for, make_settings

pytestmark = pytest.mark.unit

SETTINGS = make_settings()


def minimal_app() -> FastAPI:
    app = FastAPI()
    app.state.settings = SETTINGS  # deliberately no mongo client / db on app.state
    install_exception_handlers(app)

    @app.get("/me")
    async def me(user: CurrentUser = Depends(get_current_user)) -> dict:
        return {"user_id": user.user_id}

    return app


async def get_me(headers: dict[str, str] | None = None):
    async with client_for(minimal_app()) as c:
        return await c.get("/me", headers=headers or {})


async def test_valid_token_yields_user() -> None:
    r = await get_me(bearer(create_access_token(USER_ID, SETTINGS)))
    assert r.status_code == 200 and r.json() == {"user_id": USER_ID}


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": ""},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer    "},
        {"Authorization": "Basic dXNlcjpwYXNz"},
        {"Authorization": "Token abc"},
        {"X-Access-Token": "abc"},
    ],
    ids=["none", "empty", "scheme-only", "scheme-space", "scheme-blank", "basic", "token-scheme", "other-header"],
)
async def test_missing_or_non_bearer_is_not_authenticated(headers) -> None:
    assert_envelope(await get_me(headers), 401, "not_authenticated")


async def test_token_in_query_string_is_ignored() -> None:
    tok = create_access_token(USER_ID, SETTINGS)
    async with client_for(minimal_app()) as c:
        r = await c.get("/me", params={"access_token": tok, "token": tok})
    assert_envelope(r, 401, "not_authenticated")


@pytest.mark.parametrize(
    "token",
    [
        "garbage",
        "a.b.c",
        create_refresh_token(USER_ID, SETTINGS).token,  # wrong type
        create_access_token(USER_ID, make_settings(jwt_secret=OTHER_SECRET)),  # wrong secret
        "x" * 5000,
    ],
    ids=["garbage", "three-parts", "refresh-as-access", "wrong-secret", "oversized"],
)
async def test_invalid_token(token: str) -> None:
    err = assert_envelope(await get_me(bearer(token)), 401, "invalid_token")
    assert token not in err["message"]


async def test_expired_token_is_invalid_token() -> None:
    tok = create_access_token(USER_ID, SETTINGS, now=utcnow() - timedelta(minutes=16))
    assert_envelope(await get_me(bearer(tok)), 401, "invalid_token")


# --- token layer mocked: proves get_current_user is "decode, then map" and nothing else.
# (Patches the name the dependency module uses; the one deliberate coupling in this file.)


async def test_mocked_decoder_success(monkeypatch) -> None:
    calls = []

    def fake_decode(token, expected_type, settings, **kw):
        calls.append((token, expected_type, settings))
        return TokenClaims(sub="fedcba9876543210fedcba98", type="access", iat=1, exp=2)

    monkeypatch.setattr(auth_deps, "decode_token", fake_decode)
    r = await get_me(bearer("opaque-token"))
    assert r.status_code == 200 and r.json() == {"user_id": "fedcba9876543210fedcba98"}
    assert calls == [("opaque-token", "access", SETTINGS)]


async def test_mocked_decoder_failure(monkeypatch) -> None:
    def fake_decode(*a, **kw):
        raise InvalidTokenError

    monkeypatch.setattr(auth_deps, "decode_token", fake_decode)
    assert_envelope(await get_me(bearer("whatever")), 401, "invalid_token")
