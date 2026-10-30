"""Auth endpoints over HTTP against the real test DB (CONTRACT.md "Auth endpoints")."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta

import pytest

from backend.auth.tokens import create_access_token, create_refresh_token, utcnow
from backend.tests.helpers import (
    OTHER_SECRET,
    PASSWORD,
    USER_ID,
    assert_envelope,
    bearer,
    make_settings,
    unique_email,
)

pytestmark = pytest.mark.integration

SETTINGS = make_settings()


# ------------------------------------------------------------------ signup


async def test_signup_returns_public_user_only(api, db) -> None:
    r = await api.client.post("/auth/signup", json={"email": "  Alice@Example.COM ", "password": PASSWORD})
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == {"id", "email", "created_at"}  # no tokens, no hash
    assert re.fullmatch(r"[0-9a-f]{24}", body["id"])
    assert body["email"] == "alice@example.com"
    datetime.fromisoformat(body["created_at"].replace("Z", "+00:00"))
    stored = await db["users"].find_one({"email": "alice@example.com"})
    assert stored is not None and str(stored["_id"]) == body["id"]
    assert stored["password_hash"].startswith("$2b$12$") and PASSWORD not in str(stored)


async def test_duplicate_signup_is_409_case_insensitive(api) -> None:
    await api.signup("bob@example.com")
    for variant in ("bob@example.com", "BOB@Example.com", "  bob@EXAMPLE.COM  "):
        r = await api.client.post("/auth/signup", json={"email": variant, "password": "another password"})
        assert_envelope(r, 409, "email_taken")


async def test_concurrent_duplicate_signups_one_wins(api) -> None:
    rs = await asyncio.gather(
        *(api.client.post("/auth/signup", json={"email": "race@example.com", "password": PASSWORD}) for _ in range(3))
    )
    assert sorted(r.status_code for r in rs) == [201, 409, 409]


@pytest.mark.parametrize(
    "body",
    [
        {"email": "not-an-email", "password": PASSWORD},
        {"email": "a@example.com", "password": "short"},
        {"email": "a@example.com", "password": "a" * 73},
        {"email": "a@example.com"},
        {"email": "a@example.com", "password": PASSWORD, "is_admin": True},
    ],
    ids=["bad-email", "short-pw", "long-pw", "missing-pw", "extra-field"],
)
async def test_signup_validation(api, body) -> None:
    r = await api.client.post("/auth/signup", json=body)
    assert_envelope(r, 422, "validation_error")
    if "password" in body:
        assert body["password"] not in r.text


# ------------------------------------------------------------------ login


async def test_login_returns_token_pair(api) -> None:
    user = await api.signup()
    r = await api.client.post("/auth/login", json={"email": user["email"].upper(), "password": PASSWORD})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"access_token", "refresh_token", "token_type", "expires_in"}
    assert body["token_type"] == "bearer" and body["expires_in"] == 900
    assert body["access_token"] != body["refresh_token"]


async def test_wrong_password_and_unknown_user_are_byte_identical(api) -> None:
    user = await api.signup()
    wrong = await api.client.post("/auth/login", json={"email": user["email"], "password": "wrong password!"})
    ghost = await api.client.post("/auth/login", json={"email": "ghost@example.com", "password": "wrong password!"})
    assert_envelope(wrong, 401, "invalid_credentials")
    assert_envelope(ghost, 401, "invalid_credentials")
    assert wrong.content == ghost.content
    drop = {"date", "content-length"}
    assert {k: v for k, v in wrong.headers.items() if k not in drop} == {
        k: v for k, v in ghost.headers.items() if k not in drop
    }


@pytest.mark.parametrize("pw", ["x", "a" * 73, "a" * 1024, "correct horse battery\x00", "correct horse batter"])
async def test_login_policy_violations_are_just_wrong_passwords(api, pw: str) -> None:
    user = await api.signup()
    r = await api.client.post("/auth/login", json={"email": user["email"], "password": pw})
    assert_envelope(r, 401, "invalid_credentials")


async def test_login_credentials_in_query_are_ignored(api) -> None:
    user = await api.signup()
    r = await api.client.post("/auth/login", params={"email": user["email"], "password": PASSWORD})
    assert_envelope(r, 422, "validation_error")


# ------------------------------------------------------------------ refresh


async def test_refresh_rotates(api) -> None:
    _, t1 = await api.new_user()
    r = await api.client.post("/auth/refresh", json={"refresh_token": t1["refresh_token"]})
    assert r.status_code == 200, r.text
    t2 = r.json()
    assert set(t2) == {"access_token", "refresh_token", "token_type", "expires_in"}
    assert t2["refresh_token"] != t1["refresh_token"]
    assert (await api.chat(t2["access_token"])).status_code == 200
    # the new refresh token is itself usable once
    assert (await api.client.post("/auth/refresh", json={"refresh_token": t2["refresh_token"]})).status_code == 200


async def test_replayed_refresh_token_revokes_the_whole_family(api) -> None:
    _, t1 = await api.new_user()
    t2 = (await api.client.post("/auth/refresh", json={"refresh_token": t1["refresh_token"]})).json()
    replay = await api.client.post("/auth/refresh", json={"refresh_token": t1["refresh_token"]})
    assert_envelope(replay, 401, "invalid_token")
    after = await api.client.post("/auth/refresh", json={"refresh_token": t2["refresh_token"]})
    assert_envelope(after, 401, "invalid_token")


async def test_reuse_revokes_other_devices_too(api) -> None:
    user, phone = await api.new_user()
    laptop = await api.login(user["email"])
    rotated = (await api.client.post("/auth/refresh", json={"refresh_token": phone["refresh_token"]})).json()
    await api.client.post("/auth/refresh", json={"refresh_token": phone["refresh_token"]})  # replay
    for tok in (laptop["refresh_token"], rotated["refresh_token"]):
        assert_envelope(await api.client.post("/auth/refresh", json={"refresh_token": tok}), 401, "invalid_token")
    # Access tokens are local-only: still valid until expiry (documented).
    assert (await api.chat(laptop["access_token"])).status_code == 200


async def test_parallel_refresh_with_same_token_one_wins_then_all_revoked(api) -> None:
    _, t1 = await api.new_user()
    rs = await asyncio.gather(
        *(api.client.post("/auth/refresh", json={"refresh_token": t1["refresh_token"]}) for _ in range(2))
    )
    assert sorted(r.status_code for r in rs) == [200, 401]
    winner = next(r for r in rs if r.status_code == 200).json()
    r = await api.client.post("/auth/refresh", json={"refresh_token": winner["refresh_token"]})
    assert_envelope(r, 401, "invalid_token")


async def test_sixth_login_evicts_oldest_session(api) -> None:
    user = await api.signup()
    sessions = [await api.login(user["email"]) for _ in range(6)]
    oldest = await api.client.post("/auth/refresh", json={"refresh_token": sessions[0]["refresh_token"]})
    assert_envelope(oldest, 401, "invalid_token")


async def test_five_sessions_are_all_live(api) -> None:
    user = await api.signup()
    sessions = [await api.login(user["email"]) for _ in range(5)]
    for s in sessions:
        r = await api.client.post("/auth/refresh", json={"refresh_token": s["refresh_token"]})
        assert r.status_code == 200, r.text


@pytest.mark.parametrize(
    "make",
    [
        lambda uid: create_access_token(uid, SETTINGS),
        lambda uid: create_refresh_token(uid, SETTINGS, now=utcnow() - timedelta(days=8)).token,
        lambda uid: create_refresh_token(uid, make_settings(jwt_secret=OTHER_SECRET)).token,
        lambda uid: "not.a.jwt",
    ],
    ids=["access-as-refresh", "expired", "forged", "garbage"],
)
async def test_refresh_rejects_bad_tokens(api, make) -> None:
    user, _ = await api.new_user()
    r = await api.client.post("/auth/refresh", json={"refresh_token": make(user["id"])})
    assert_envelope(r, 401, "invalid_token")


async def test_refresh_token_never_from_query(api) -> None:
    _, t = await api.new_user()
    r = await api.client.post("/auth/refresh", params={"refresh_token": t["refresh_token"]})
    assert_envelope(r, 422, "validation_error")


async def test_refresh_for_unissued_but_validly_signed_token(api) -> None:
    # Signed with our key but never stored (e.g. user deleted / DB restored): rejected.
    r = await api.client.post("/auth/refresh", json={"refresh_token": create_refresh_token(USER_ID, SETTINGS).token})
    assert_envelope(r, 401, "invalid_token")


# ------------------------------------------------------------------ logout


async def test_logout_revokes_that_session_only_and_is_idempotent(api) -> None:
    user, a = await api.new_user()
    b = await api.login(user["email"])
    r = await api.client.post("/auth/logout", json={"refresh_token": a["refresh_token"]})
    assert r.status_code == 204 and r.content == b""
    again = await api.client.post("/auth/logout", json={"refresh_token": a["refresh_token"]})
    assert again.status_code == 204
    # the other device is unaffected (logout never triggers revoke-all)
    assert (await api.client.post("/auth/refresh", json={"refresh_token": b["refresh_token"]})).status_code == 200
    # the logged-out token is dead
    dead = await api.client.post("/auth/refresh", json={"refresh_token": a["refresh_token"]})
    assert_envelope(dead, 401, "invalid_token")


async def test_access_token_survives_logout_until_expiry(api) -> None:
    _, t = await api.new_user()
    assert (await api.client.post("/auth/logout", json={"refresh_token": t["refresh_token"]})).status_code == 204
    assert (await api.chat(t["access_token"])).status_code == 200


async def test_logout_with_expired_genuine_token_is_204(api) -> None:
    user, _ = await api.new_user()
    expired = create_refresh_token(user["id"], SETTINGS, now=utcnow() - timedelta(days=8)).token
    r = await api.client.post("/auth/logout", json={"refresh_token": expired})
    assert r.status_code == 204


@pytest.mark.parametrize(
    "make",
    [
        lambda: create_access_token(USER_ID, SETTINGS),
        lambda: create_refresh_token(USER_ID, make_settings(jwt_secret=OTHER_SECRET)).token,
        lambda: "garbage",
    ],
    ids=["access-token", "forged", "garbage"],
)
async def test_logout_rejects_unauthentic_tokens(api, make) -> None:
    assert_envelope(await api.client.post("/auth/logout", json={"refresh_token": make()}), 401, "invalid_token")


@pytest.mark.parametrize("path", ["/auth/refresh", "/auth/logout"])
@pytest.mark.parametrize("body", [{}, {"refresh_token": ""}, {"refresh_token": "a.b.c", "x": 1}])
async def test_refresh_logout_validation(api, path: str, body) -> None:
    assert_envelope(await api.client.post(path, json=body), 422, "validation_error")
