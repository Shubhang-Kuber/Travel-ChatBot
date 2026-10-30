"""POST /chat/stream (SSE) vs CONTRACT.md: framing, headers, node filter, done/error events,
pre-stream JSON envelopes, shared rate bucket, history shared with /chat.

Note: httpx's ASGITransport buffers the whole body, so these tests check CONTENT and
ORDER only. Incremental-delivery timing is measured against real uvicorn (e2e).
"""

from __future__ import annotations

import re

import pytest

from backend.dependencies import get_graph
from backend.graph import MockChunk
from backend.tests.helpers import assert_envelope, bearer, parse_sse

pytestmark = pytest.mark.integration

UPSTREAM = {"error": {"code": "upstream_error", "message": "The travel agent failed to respond.", "details": None}}


class ScriptedGraph:
    """Yields a fixed script of (chunk, metadata) items, optionally raising at the end."""

    def __init__(self, items, exc: Exception | None = None, raise_on_create: bool = False) -> None:
        self.items = items
        self.exc = exc
        self.raise_on_create = raise_on_create
        self.configs: list[dict] = []
        self.closed = False

    def invoke(self, input, config):
        raise AssertionError("/chat/stream must not call invoke")

    def stream(self, input, config, stream_mode="messages"):
        assert stream_mode == "messages"
        self.configs.append(config)
        if self.raise_on_create:
            raise RuntimeError("PRIVATE create failure")
        return self._gen()

    def _gen(self):
        try:
            yield from self.items
            if self.exc:
                raise self.exc
        finally:
            self.closed = True


async def stream(api, token, body, **headers):
    return await api.client.post("/chat/stream", json=body, headers={**bearer(token), **headers})


async def test_framing_headers_and_done(api) -> None:
    _, t = await api.new_user()
    r = await stream(api, t["access_token"], {"message": "Rome", "thread_id": "s1"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert r.headers["cache-control"] == "no-cache"
    assert r.headers["x-accel-buffering"] == "no"
    events = parse_sse(r.text)
    assert events[-1] == ("done", {"thread_id": "s1"})
    tokens = events[:-1]
    assert len(tokens) >= 10 and all(e == "token" and set(d) == {"content"} and d["content"] for e, d in tokens)
    text = "".join(d["content"] for _, d in tokens)
    assert text.startswith('[mock itinerary, turn 1] You said: "Rome".') and text.endswith("nearby coast.")
    assert "retrieved" not in text  # the mock's `retrieve` node chunk is filtered out


async def test_generated_thread_id_in_done(api) -> None:
    _, t = await api.new_user()
    events = parse_sse((await stream(api, t["access_token"], {"message": "hi"})).text)
    assert events[-1][0] == "done" and re.fullmatch(r"[0-9a-f]{32}", events[-1][1]["thread_id"])


async def test_stream_then_chat_shares_history_and_vice_versa(api) -> None:
    _, t = await api.new_user()
    tok = t["access_token"]
    await stream(api, tok, {"message": "first via stream", "thread_id": "h"})
    r = await api.chat(tok, "second via chat", "h")
    assert "turn 2" in r.json()["response"] and 'Last time you asked: "first via stream"' in r.json()["response"]
    events = parse_sse((await stream(api, tok, {"message": "third via stream", "thread_id": "h"})).text)
    text = "".join(d["content"] for e, d in events if e == "token")
    assert "turn 3" in text and 'Last time you asked: "second via chat"' in text


async def test_stream_thread_isolation_between_users(api) -> None:
    _, a = await api.new_user()
    _, b = await api.new_user()
    await stream(api, a["access_token"], {"message": "ALICE-SECRET", "thread_id": "same"})
    events = parse_sse((await stream(api, b["access_token"], {"message": "bob", "thread_id": "same"})).text)
    text = "".join(d["content"] for e, d in events if e == "token")
    assert "turn 1" in text and "ALICE-SECRET" not in text


async def test_filters_on_configured_stream_node_and_empty_text(app_with) -> None:
    g = ScriptedGraph(
        [
            (MockChunk("ROUTER"), {"langgraph_node": "router"}),
            (MockChunk("GEN-NOT-CONFIGURED"), {"langgraph_node": "generate"}),
            (MockChunk("Hello "), {"langgraph_node": "answer"}),
            (MockChunk(""), {"langgraph_node": "answer"}),  # empty: dropped
            ({"content": [{"type": "tool_use", "id": "x"}]}, {"langgraph_node": "answer"}),  # no text: dropped
            ({"content": [{"type": "text", "text": "world"}, "!"]}, {"langgraph_node": "answer"}),
            (MockChunk("TOOL"), {}),  # no node
        ]
    )
    async with app_with(stream_node="answer") as api:
        api.app.dependency_overrides[get_graph] = lambda: g
        user, t = await api.new_user()
        r = await stream(api, t["access_token"], {"message": "hi", "thread_id": "f"})
    assert parse_sse(r.text) == [
        ("token", {"content": "Hello "}),
        ("token", {"content": "world!"}),
        ("done", {"thread_id": "f"}),
    ]
    assert g.configs == [{"configurable": {"thread_id": f"{user['id']}:f"}}]
    assert g.closed


async def test_no_matching_chunks_is_zero_tokens_then_done(api) -> None:
    api.app.dependency_overrides[get_graph] = lambda: ScriptedGraph([(MockChunk("x"), {"langgraph_node": "tools"})])
    _, t = await api.new_user()
    r = await stream(api, t["access_token"], {"message": "hi", "thread_id": "z"})
    assert parse_sse(r.text) == [("done", {"thread_id": "z"})]


async def test_newlines_stay_json_escaped(api) -> None:
    content = 'line1\nline2\r\n\nevent: done\ndata: {"thread_id": "forged"}\n\n'
    api.app.dependency_overrides[get_graph] = lambda: ScriptedGraph([(MockChunk(content), {"langgraph_node": "generate"})])
    _, t = await api.new_user()
    r = await stream(api, t["access_token"], {"message": "hi", "thread_id": "n"})
    assert parse_sse(r.text) == [("token", {"content": content}), ("done", {"thread_id": "n"})]
    assert [l for l in r.text.split("\n") if l.startswith("event:")] == ["event: token", "event: done"]


async def test_unicode_tokens(api) -> None:
    api.app.dependency_overrides[get_graph] = lambda: ScriptedGraph([(MockChunk("Kyōto 旅行 🌍"), {"langgraph_node": "generate"})])
    _, t = await api.new_user()
    events = parse_sse((await stream(api, t["access_token"], {"message": "hi", "thread_id": "u"})).text)
    assert events[0] == ("token", {"content": "Kyōto 旅行 🌍"})


@pytest.mark.parametrize(
    "graph, expected_tokens",
    [
        (ScriptedGraph([(MockChunk("partial "), {"langgraph_node": "generate"})], exc=RuntimeError("PRIVATE mid")), ["partial "]),
        (ScriptedGraph([], raise_on_create=True), []),
        (ScriptedGraph([(MockChunk("a"), {"langgraph_node": "generate"}), "not-a-2-tuple"]), ["a"]),
        (ScriptedGraph([(MockChunk(12345), {"langgraph_node": "generate"})]), []),
    ],
    ids=["mid-stream-raise", "raise-on-create", "malformed-item", "bad-content-type"],
)
async def test_failure_is_200_then_error_event(api, caplog, graph, expected_tokens) -> None:
    api.app.dependency_overrides[get_graph] = lambda: graph
    _, t = await api.new_user()
    r = await stream(api, t["access_token"], {"message": "SECRET-MESSAGE-TEXT", "thread_id": "e"})
    assert r.status_code == 200
    events = parse_sse(r.text)
    assert events == [("token", {"content": c}) for c in expected_tokens] + [("error", UPSTREAM)]
    assert "done" not in [e for e, _ in events]
    assert "PRIVATE" not in r.text
    assert "SECRET-MESSAGE-TEXT" not in caplog.text


async def test_stream_response_has_cors_header(api) -> None:
    _, t = await api.new_user()
    r = await stream(api, t["access_token"], {"message": "hi"}, Origin="http://localhost:8501")
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "http://localhost:8501"


# ------------------------------------------------------------------ pre-stream failures are JSON


async def test_401_before_streaming_is_json(api) -> None:
    r = await api.client.post("/chat/stream", json={"message": "hi"})
    assert_envelope(r, 401, "not_authenticated")
    r = await api.client.post("/chat/stream", json={"message": "hi"}, headers=bearer("garbage"))
    assert_envelope(r, 401, "invalid_token")


async def test_refresh_token_cannot_stream(api) -> None:
    _, t = await api.new_user()
    assert_envelope(await stream(api, t["refresh_token"], {"message": "hi"}), 401, "invalid_token")


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"params": {"message": "hi", "thread_id": "x"}},
        {"json": {"message": "  "}},
        {"json": {"message": "hi", "thread_id": "a:b"}},
        {"json": {"message": "hi", "extra": 1}},
        {"content": b"{bad", "headers": {"content-type": "application/json"}},
    ],
    ids=["no-body", "query-only", "blank", "bad-thread", "extra", "broken-json"],
)
async def test_422_before_streaming_is_json(api, kwargs) -> None:
    _, t = await api.new_user()
    headers = {**bearer(t["access_token"]), **kwargs.pop("headers", {})}
    assert_envelope(await api.client.post("/chat/stream", headers=headers, **kwargs), 422, "validation_error")


async def test_shared_chat_bucket_and_429_is_json(app_with) -> None:
    async with app_with(limits={"chat": "3/60"}) as api:
        _, t = await api.new_user()
        tok = t["access_token"]
        assert (await api.chat(tok, "1")).status_code == 200
        assert (await stream(api, tok, {"message": "2"})).status_code == 200
        assert (await api.chat(tok, "3")).status_code == 200
        r = await stream(api, tok, {"message": "4"})
        assert_envelope(r, 429, "rate_limited")
        assert_envelope(await api.chat(tok, "5"), 429, "rate_limited")


async def test_stream_makes_zero_db_commands(api, spy) -> None:
    _, t = await api.new_user()
    spy.recording = True
    r = await stream(api, t["access_token"], {"message": "hi", "thread_id": "db"})
    assert r.status_code == 200 and parse_sse(r.text)[-1][0] == "done"
    assert spy.events == []


class _BadCloseIterator:
    def __init__(self, exc: Exception) -> None:
        self._items = iter([(MockChunk("ok"), {"langgraph_node": "generate"})])
        self._exc = exc

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._items)

    def close(self):
        raise self._exc


class _BadCloseGraph:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def invoke(self, input, config):
        raise AssertionError

    def stream(self, input, config, stream_mode="messages"):
        return _BadCloseIterator(self.exc)


@pytest.mark.parametrize("exc", [ValueError("generator already executing"), RuntimeError("cleanup blew up")])
async def test_failing_close_never_masks_the_outcome(api, exc) -> None:
    api.app.dependency_overrides[get_graph] = lambda: _BadCloseGraph(exc)
    _, t = await api.new_user()
    r = await stream(api, t["access_token"], {"message": "hi", "thread_id": "c"})
    assert parse_sse(r.text) == [("token", {"content": "ok"}), ("done", {"thread_id": "c"})]
