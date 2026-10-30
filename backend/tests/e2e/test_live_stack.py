"""Functional e2e against the real uvicorn process + real compose MongoDB (Task C a)."""

from __future__ import annotations

import pytest

from backend.auth.tokens import create_access_token
from backend.tests.e2e.live import PASSWORD, bearer, new_user, post_patiently, unique_email
from backend.tests.helpers import USER_ID, assert_envelope, make_settings, parse_sse

pytestmark = pytest.mark.e2e


def test_health(http) -> None:
    r = http.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok", "mongo": "up"}


def test_full_flow_signup_login_chat_refresh_logout(http) -> None:
    email = unique_email("flow")
    r = post_patiently(http, "/auth/signup", json={"email": email.upper(), "password": PASSWORD})
    assert r.status_code == 201 and set(r.json()) == {"id", "email", "created_at"} and r.json()["email"] == email
    t1 = post_patiently(http, "/auth/login", json={"email": email, "password": PASSWORD}).json()
    assert t1["token_type"] == "bearer" and t1["expires_in"] > 0

    r = http.post("/chat", json={"message": "Plan 3 days in Lisbon", "thread_id": "e2e-1"}, headers=bearer(t1["access_token"]))
    assert r.status_code == 200 and r.json()["thread_id"] == "e2e-1" and "Lisbon" in r.json()["response"]

    t2 = post_patiently(http, "/auth/refresh", json={"refresh_token": t1["refresh_token"]}).json()
    assert t2["refresh_token"] != t1["refresh_token"]
    r = http.post("/chat", json={"message": "and day 4?", "thread_id": "e2e-1"}, headers=bearer(t2["access_token"]))
    assert r.status_code == 200 and "turn 2" in r.json()["response"]

    assert post_patiently(http, "/auth/logout", json={"refresh_token": t2["refresh_token"]}).status_code == 204
    assert post_patiently(http, "/auth/logout", json={"refresh_token": t2["refresh_token"]}).status_code == 204
    r = post_patiently(http, "/auth/refresh", json={"refresh_token": t2["refresh_token"]})
    assert_envelope(r, 401, "invalid_token")


def test_refresh_replay_revokes_family(http, bob) -> None:
    t = post_patiently(http, "/auth/login", json={"email": bob["email"], "password": PASSWORD}).json()
    t2 = post_patiently(http, "/auth/refresh", json={"refresh_token": t["refresh_token"]}).json()
    assert_envelope(post_patiently(http, "/auth/refresh", json={"refresh_token": t["refresh_token"]}), 401, "invalid_token")
    assert_envelope(post_patiently(http, "/auth/refresh", json={"refresh_token": t2["refresh_token"]}), 401, "invalid_token")


def test_stream(http, alice) -> None:
    r = http.post("/chat/stream", json={"message": "Kyoto", "thread_id": "e2e-s"}, headers=bearer(alice["access_token"]))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(r.text)
    assert events[-1] == ("done", {"thread_id": "e2e-s"})
    assert len(events) > 10 and all(e == "token" for e, _ in events[:-1])
    # turn persisted: /chat on the same thread is turn 2
    r = http.post("/chat", json={"message": "more", "thread_id": "e2e-s"}, headers=bearer(alice["access_token"]))
    assert "turn 2" in r.json()["response"]


def test_thread_isolation(http, alice, bob) -> None:
    http.post("/chat", json={"message": "ALICE-ONLY", "thread_id": "e2e-shared"}, headers=bearer(alice["access_token"]))
    r = http.post("/chat", json={"message": "bob", "thread_id": "e2e-shared"}, headers=bearer(bob["access_token"]))
    assert "ALICE-ONLY" not in r.text


def test_query_params_only_is_422(http, alice) -> None:
    r = http.post("/chat?message=hi&thread_id=x", headers=bearer(alice["access_token"]))
    assert_envelope(r, 422, "validation_error")


@pytest.mark.parametrize(
    "headers, code",
    [
        ({}, "not_authenticated"),
        ({"Authorization": "Basic dXNlcjpwYXNz"}, "not_authenticated"),
        (bearer("garbage"), "invalid_token"),
        # Signed with a key the live server does not have.
        (bearer(create_access_token(USER_ID, make_settings(jwt_secret="not-the-live-secret-0123456789abcdef"))), "invalid_token"),
    ],
    ids=["missing", "basic", "garbage", "foreign-key"],
)
@pytest.mark.parametrize("path", ["/chat", "/chat/stream"])
def test_401_matrix(http, headers, code, path) -> None:
    assert_envelope(http.post(path, json={"message": "hi"}, headers=headers), 401, code)


def test_refresh_token_as_access_is_401(http, alice) -> None:
    assert_envelope(http.post("/chat", json={"message": "hi"}, headers=bearer(alice["refresh_token"])), 401, "invalid_token")


def test_login_failures_identical(http, alice) -> None:
    wrong = post_patiently(http, "/auth/login", json={"email": alice["email"], "password": "wrong password!"})
    ghost = post_patiently(http, "/auth/login", json={"email": unique_email("ghost"), "password": "wrong password!"})
    assert_envelope(wrong, 401, "invalid_credentials")
    assert wrong.content == ghost.content


def test_duplicate_signup(http, alice) -> None:
    r = post_patiently(http, "/auth/signup", json={"email": alice["email"].upper(), "password": PASSWORD})
    assert_envelope(r, 409, "email_taken")


def test_404_405(http) -> None:
    assert_envelope(http.get("/nope"), 404, "not_found")
    assert_envelope(http.get("/chat"), 405, "method_not_allowed")


def test_cors_preflight(http) -> None:
    r = http.options("/chat/stream", headers={"Origin": "http://localhost:8501", "Access-Control-Request-Method": "POST"})
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "http://localhost:8501"


def test_chat_rate_limit_429_on_real_process(http) -> None:
    # Dedicated user, so other tests' budgets are untouched. Shared /chat + /chat/stream bucket.
    u = new_user(http)
    h = bearer(u["access_token"])
    statuses = []
    for i in range(200):
        path = "/chat" if i % 2 else "/chat/stream"
        r = http.post(path, json={"message": f"m{i}"}, headers=h)
        statuses.append(r.status_code)
        if r.status_code == 429:
            break
    assert_envelope(r, 429, "rate_limited")
    assert set(statuses[:-1]) == {200}
    print(f"\n[e2e] chat bucket exhausted after {len(statuses) - 1} requests; Retry-After={r.headers['retry-after']}s")
