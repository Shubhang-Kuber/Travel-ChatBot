"""/chat over HTTP: full flow, failure matrix, constraint 1 by observation, constraint 4
regression guard, 502 mapping, thread isolation."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest

from backend.auth.tokens import create_access_token, utcnow
from backend.dependencies import get_graph
from backend.tests.helpers import OTHER_SECRET, assert_envelope, bearer, make_settings

pytestmark = pytest.mark.integration

SETTINGS = make_settings()


class RecordingGraph:
    def __init__(self, exc: Exception | None = None) -> None:
        self.calls: list[tuple[dict, dict]] = []
        self.exc = exc

    def invoke(self, input, config):
        self.calls.append((input, config))
        if self.exc:
            raise self.exc
        return {"messages": [{"role": "assistant", "content": [{"type": "text", "text": "Day 1: Alfama"}]}]}

    def stream(self, input, config, stream_mode="messages"):
        yield from ()


async def test_full_flow_signup_login_chat(api) -> None:
    user = await api.signup()
    tokens = await api.login(user["email"])
    r = await api.chat(tokens["access_token"], "Plan 3 days in Lisbon", "trip-lisbon-1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"response", "thread_id"}
    assert body["thread_id"] == "trip-lisbon-1"
    assert isinstance(body["response"], str) and "Plan 3 days in Lisbon" in body["response"]


async def test_graph_receives_stripped_message_and_namespaced_thread(api) -> None:
    g = RecordingGraph()
    api.app.dependency_overrides[get_graph] = lambda: g
    user, t = await api.new_user()
    r = await api.chat(t["access_token"], "   Lisbon please \n", "t-1")
    assert r.status_code == 200 and r.json() == {"response": "Day 1: Alfama", "thread_id": "t-1"}
    assert g.calls == [
        ({"messages": [{"role": "user", "content": "Lisbon please"}]}, {"configurable": {"thread_id": f"{user['id']}:t-1"}})
    ]


async def test_omitted_thread_id_is_generated_and_continues(api) -> None:
    _, t = await api.new_user()
    r1 = await api.chat(t["access_token"], "first")
    tid = r1.json()["thread_id"]
    assert re.fullmatch(r"[0-9a-f]{32}", tid)
    r2 = await api.chat(t["access_token"], "second", tid)
    assert r2.json()["thread_id"] == tid
    assert "turn 2" in r2.json()["response"] and 'Last time you asked: "first"' in r2.json()["response"]
    r3 = await api.chat(t["access_token"], "fresh")
    assert r3.json()["thread_id"] != tid and "turn 1" in r3.json()["response"]


async def test_thread_isolation_between_users(api) -> None:
    _, alice = await api.new_user()
    _, bob = await api.new_user()
    a1 = await api.chat(alice["access_token"], "ALICE-PRIVATE-PLAN", "shared")
    b1 = await api.chat(bob["access_token"], "bob here", "shared")
    assert "turn 1" in a1.json()["response"]
    assert "turn 1" in b1.json()["response"] and "ALICE-PRIVATE-PLAN" not in b1.text
    a2 = await api.chat(alice["access_token"], "again", "shared")
    assert "turn 2" in a2.json()["response"] and "ALICE-PRIVATE-PLAN" in a2.json()["response"]
    assert "bob here" not in a2.text


# ------------------------------------------------------------------ auth failure matrix


async def test_missing_authorization_header(api) -> None:
    assert_envelope(await api.chat(None), 401, "not_authenticated")


@pytest.mark.parametrize(
    "token",
    [
        "malformed",
        "a.b.c",
        create_access_token("0123456789abcdef01234567", make_settings(jwt_secret=OTHER_SECRET)),
        create_access_token("0123456789abcdef01234567", SETTINGS, now=utcnow() - timedelta(minutes=16)),
    ],
    ids=["malformed", "three-segments", "wrong-secret", "expired"],
)
async def test_invalid_tokens(api, token: str) -> None:
    assert_envelope(await api.chat(token), 401, "invalid_token")


async def test_refresh_token_cannot_call_chat(api) -> None:
    _, t = await api.new_user()
    assert_envelope(await api.chat(t["refresh_token"]), 401, "invalid_token")


# ------------------------------------------------------------------ hard constraint 1, observed


async def test_protected_request_makes_zero_db_commands(api, spy) -> None:
    spy.recording = True
    user = await api.signup()
    tokens = await api.login(user["email"])
    # Sanity: the spy does see `users` traffic (signup insert, login find + session update).
    seen = {name for name, _, _ in spy.touching("users")}
    assert {"insert", "find", "update"} <= seen, spy.events

    spy.clear()
    for i in range(3):
        r = await api.chat(tokens["access_token"], f"message {i}", "c1")
        assert r.status_code == 200
    assert spy.touching("users") == [], spy.events
    assert spy.events == [], f"/chat issued DB commands: {spy.events}"


async def test_rejected_protected_request_makes_zero_db_commands(api, spy) -> None:
    spy.recording = True
    for headers in ({}, bearer("garbage")):
        r = await api.client.post("/chat", json={"message": "x"}, headers=headers)
        assert r.status_code == 401
    assert spy.events == []


# ------------------------------------------------------------------ hard constraint 4 (JSON body only)


async def test_query_params_only_is_422(api) -> None:
    _, t = await api.new_user()
    r = await api.client.post("/chat?message=hi&thread_id=x", headers=bearer(t["access_token"]))
    err = assert_envelope(r, 422, "validation_error")
    assert any(d["loc"][0] == "body" for d in err["details"])


async def test_query_params_never_override_body(api) -> None:
    _, t = await api.new_user()
    r = await api.client.post(
        "/chat?thread_id=evil", json={"message": "hi", "thread_id": "good"}, headers=bearer(t["access_token"])
    )
    assert r.status_code == 200 and r.json()["thread_id"] == "good"


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"json": {}},
        {"json": {"message": ""}},
        {"json": {"message": "   "}},
        {"json": {"message": "x" * 4001}},
        {"json": {"message": "hi", "thread_id": "bad:id"}},
        {"json": {"message": "hi", "thread_id": "x" * 65}},
        {"json": {"message": "hi", "threadid": "typo"}},
        {"json": ["hi"]},
        {"content": b"message=hi", "headers": {"content-type": "application/x-www-form-urlencoded"}},
        {"content": b"{broken", "headers": {"content-type": "application/json"}},
    ],
    ids=["no-body", "empty-obj", "empty-msg", "blank-msg", "too-long", "colon-thread", "long-thread", "unknown-field", "array", "form", "broken-json"],
)
async def test_bad_bodies_are_422(api, kwargs) -> None:
    _, t = await api.new_user()
    headers = {**bearer(t["access_token"]), **kwargs.pop("headers", {})}
    r = await api.client.post("/chat", headers=headers, **kwargs)
    assert_envelope(r, 422, "validation_error")


async def test_lone_surrogate_message_is_422_not_500(api) -> None:
    _, t = await api.new_user()
    r = await api.client.post(
        "/chat",
        content=b'{"message": "hi \\ud800 there"}',
        headers={**bearer(t["access_token"]), "content-type": "application/json"},
    )
    assert_envelope(r, 422, "validation_error")


async def test_unicode_message_round_trips(api) -> None:
    _, t = await api.new_user()
    msg = "Itinéraire 旅行 \U0001f30d"
    r = await api.chat(t["access_token"], msg, "u1")
    assert r.status_code == 200 and msg in r.json()["response"]


# ------------------------------------------------------------------ upstream failures


async def test_graph_raising_is_502_upstream_error(api, caplog) -> None:
    api.app.dependency_overrides[get_graph] = lambda: RecordingGraph(exc=RuntimeError("model overloaded"))
    _, t = await api.new_user()
    r = await api.chat(t["access_token"], "PRIVATE-TRAVEL-DETAILS", "t")
    err = assert_envelope(r, 502, "upstream_error")
    assert "model overloaded" not in r.text
    assert "PRIVATE-TRAVEL-DETAILS" not in caplog.text


async def test_unexpected_error_is_500_internal_error(api) -> None:
    def broken_graph():
        raise RuntimeError("internal detail: mongodb://user:pw@host")

    api.app.dependency_overrides[get_graph] = broken_graph
    _, t = await api.new_user()
    r = await api.chat(t["access_token"])
    assert_envelope(r, 500, "internal_error")
    assert "internal detail" not in r.text and "pw@host" not in r.text
