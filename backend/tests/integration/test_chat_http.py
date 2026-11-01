"""/chat over HTTP: full flow, JSON-body-only guard, 502 mapping, threads."""

from __future__ import annotations

import re

import pytest

from backend.dependencies import get_graph
from backend.tests.helpers import assert_envelope

pytestmark = pytest.mark.integration


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


async def test_full_flow_chat_without_login(api) -> None:
    r = await api.chat("Plan 3 days in Lisbon", "trip-lisbon-1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"response", "thread_id"}
    assert body["thread_id"] == "trip-lisbon-1"
    assert isinstance(body["response"], str) and "Plan 3 days in Lisbon" in body["response"]


async def test_graph_receives_stripped_message_and_thread(api) -> None:
    g = RecordingGraph()
    api.app.dependency_overrides[get_graph] = lambda: g
    r = await api.chat("   Lisbon please \n", "t-1")
    assert r.status_code == 200 and r.json() == {"response": "Day 1: Alfama", "thread_id": "t-1"}
    assert g.calls == [
        ({"messages": [{"role": "user", "content": "Lisbon please"}]}, {"configurable": {"thread_id": "t-1"}})
    ]


async def test_locale_and_timezone_reach_the_graph(api) -> None:
    g = RecordingGraph()
    api.app.dependency_overrides[get_graph] = lambda: g
    body = {"message": "Lisbon", "thread_id": "t-2", "locale": "en-IN", "timezone": "Asia/Kolkata"}
    r = await api.client.post("/chat", json=body)
    assert r.status_code == 200
    assert g.calls[0][0] == {
        "messages": [{"role": "user", "content": "Lisbon"}],
        "user_locale": "en-IN",
        "user_timezone": "Asia/Kolkata",
    }


async def test_omitted_thread_id_is_generated_and_continues(api) -> None:
    r1 = await api.chat("first")
    tid = r1.json()["thread_id"]
    assert re.fullmatch(r"[0-9a-f]{32}", tid)
    r2 = await api.chat("second", tid)
    assert r2.json()["thread_id"] == tid
    assert "turn 2" in r2.json()["response"] and 'Last time you asked: "first"' in r2.json()["response"]
    r3 = await api.chat("fresh")
    assert r3.json()["thread_id"] != tid and "turn 1" in r3.json()["response"]


async def test_different_threads_do_not_share_history(api) -> None:
    a1 = await api.chat("PRIVATE-PLAN", "thread-a")
    b1 = await api.chat("other trip", "thread-b")
    assert "turn 1" in a1.json()["response"]
    assert "turn 1" in b1.json()["response"] and "PRIVATE-PLAN" not in b1.text


# ------------------------------------------------------------------ JSON body only


async def test_query_params_only_is_422(api) -> None:
    r = await api.client.post("/chat?message=hi&thread_id=x")
    err = assert_envelope(r, 422, "validation_error")
    assert any(d["loc"][0] == "body" for d in err["details"])


async def test_query_params_never_override_body(api) -> None:
    r = await api.client.post(
        "/chat?thread_id=evil", json={"message": "hi", "thread_id": "good"}
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
    r = await api.client.post("/chat", **kwargs)
    assert_envelope(r, 422, "validation_error")


async def test_lone_surrogate_message_is_422_not_500(api) -> None:
    r = await api.client.post(
        "/chat",
        content=b'{"message": "hi \\ud800 there"}',
        headers={"content-type": "application/json"},
    )
    assert_envelope(r, 422, "validation_error")


async def test_unicode_message_round_trips(api) -> None:
    msg = "Itinéraire 旅行 \U0001f30d"
    r = await api.chat(msg, "u1")
    assert r.status_code == 200 and msg in r.json()["response"]


# ------------------------------------------------------------------ upstream failures


async def test_graph_raising_is_502_upstream_error(api, caplog) -> None:
    api.app.dependency_overrides[get_graph] = lambda: RecordingGraph(exc=RuntimeError("model overloaded"))
    r = await api.chat("PRIVATE-TRAVEL-DETAILS", "t")
    err = assert_envelope(r, 502, "upstream_error")
    assert "model overloaded" not in r.text
    assert "PRIVATE-TRAVEL-DETAILS" not in caplog.text


async def test_unexpected_error_is_500_internal_error(api) -> None:
    def broken_graph():
        raise RuntimeError("internal detail: secret://user:pw@host")

    api.app.dependency_overrides[get_graph] = broken_graph
    r = await api.chat()
    assert_envelope(r, 500, "internal_error")
    assert "internal detail" not in r.text and "pw@host" not in r.text
