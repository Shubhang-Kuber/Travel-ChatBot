"""Graph adapter vs CONTRACT.md section 1: input shape, thread namespacing, output parsing,
off-loop invocation, 502 on failure without leaking message text, mock + loader behaviour."""

from __future__ import annotations

import logging
import sys
import threading
import types
from dataclasses import dataclass
from typing import Any

import pytest

from backend.errors import AppError
from backend.graph import MockGraph, TravelGraph, content_to_text, load_graph, run_chat
from backend.tests.helpers import make_settings

pytestmark = pytest.mark.unit


@dataclass
class Msg:
    content: Any


class RecordingGraph:
    def __init__(self, result: Any = None, exc: Exception | None = None) -> None:
        self.calls: list[tuple[dict, dict, int]] = []
        self.result = result if result is not None else {"messages": [{"role": "assistant", "content": "ok"}]}
        self.exc = exc

    def invoke(self, input, config):
        self.calls.append((input, config, threading.get_ident()))
        if self.exc:
            raise self.exc
        return self.result

    def stream(self, input, config, stream_mode="messages"):
        yield from ()


# ------------------------------------------------------------------ content_to_text


@pytest.mark.parametrize(
    "content, expected",
    [
        ("plain", "plain"),
        ("", ""),
        ([], ""),
        (["a", "b"], "ab"),
        ([{"type": "text", "text": "Day 1"}, " / ", {"type": "text", "text": "Day 2"}], "Day 1 / Day 2"),
        ([{"type": "tool_use", "id": "x", "input": {}}, {"type": "text", "text": "answer"}], "answer"),
        ([{"type": "image_url", "image_url": "u"}, {"type": "text"}, {"type": "text", "text": 5}, 7, None], ""),
        ([{"text": "no type key"}], ""),
    ],
)
def test_content_to_text(content, expected: str) -> None:
    assert content_to_text(content) == expected


@pytest.mark.parametrize("bad", [None, 42, {"type": "text", "text": "x"}, b"bytes"])
def test_content_to_text_rejects_unsupported(bad) -> None:
    with pytest.raises(TypeError):
        content_to_text(bad)


# ------------------------------------------------------------------ run_chat


async def test_input_and_namespaced_thread_id() -> None:
    g = RecordingGraph()
    await run_chat(g, "0123456789abcdef01234567", "trip-1", "Plan Lisbon")
    (inp, cfg, _), = g.calls
    assert inp == {"messages": [{"role": "user", "content": "Plan Lisbon"}]}
    assert cfg == {"configurable": {"thread_id": "0123456789abcdef01234567:trip-1"}}


async def test_client_thread_id_recoverable_after_last_colon() -> None:
    g = RecordingGraph()
    await run_chat(g, "aaaaaaaaaaaaaaaaaaaaaaaa", "t_9-x", "hi")
    tid = g.calls[0][1]["configurable"]["thread_id"]
    assert tid.rsplit(":", 1) == ["aaaaaaaaaaaaaaaaaaaaaaaa", "t_9-x"]


async def test_same_client_thread_different_users_differ() -> None:
    g = RecordingGraph()
    await run_chat(g, "a" * 24, "shared", "hi")
    await run_chat(g, "b" * 24, "shared", "hi")
    assert g.calls[0][1] != g.calls[1][1]


async def test_invoke_runs_off_the_event_loop_thread() -> None:
    g = RecordingGraph()
    await run_chat(g, "a" * 24, "t", "hi")
    assert g.calls[0][2] != threading.get_ident()


@pytest.mark.parametrize(
    "last, expected",
    [
        ({"role": "assistant", "content": "dict reply"}, "dict reply"),
        (Msg("object reply"), "object reply"),
        (Msg([{"type": "text", "text": "part"}, "s"]), "parts"),
    ],
)
async def test_reads_only_last_message(last, expected: str) -> None:
    g = RecordingGraph(result={"messages": [{"role": "user", "content": "ignored"}, Msg("ignored"), last]})
    assert await run_chat(g, "a" * 24, "t", "hi") == expected


@pytest.mark.parametrize(
    "graph",
    [
        RecordingGraph(exc=RuntimeError("LLM down")),
        RecordingGraph(exc=KeyError("x")),
        RecordingGraph(result={"no_messages": []}),
        RecordingGraph(result={"messages": []}),
        RecordingGraph(result={"messages": [Msg(12345)]}),
        RecordingGraph(result={"messages": [{"role": "assistant"}]}),
        RecordingGraph(result="not a dict"),
    ],
    ids=["raises", "keyerror", "no-messages-key", "empty-messages", "bad-content", "dict-without-content", "non-dict"],
)
async def test_failures_become_502_upstream_error(graph) -> None:
    with pytest.raises(AppError) as exc:
        await run_chat(graph, "a" * 24, "t", "hi")
    assert exc.value.status_code == 502 and exc.value.code == "upstream_error"
    assert exc.value.details is None


async def test_failure_log_has_user_and_length_but_never_message_text(caplog) -> None:
    secret = "my passport number is X1234567"
    caplog.set_level(logging.DEBUG)
    with pytest.raises(AppError):
        await run_chat(RecordingGraph(exc=RuntimeError("boom")), "c" * 24, "t", secret)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "c" * 24 in text and str(len(secret)) in text
    assert secret not in caplog.text


# ------------------------------------------------------------------ MockGraph


def test_mock_satisfies_protocol() -> None:
    assert isinstance(MockGraph(), TravelGraph)


def test_mock_is_deterministic_and_remembers_per_thread() -> None:
    g = MockGraph()
    cfg = {"configurable": {"thread_id": "u:t"}}
    r1 = g.invoke({"messages": [{"role": "user", "content": "first"}]}, cfg)["messages"][-1]["content"]
    r2 = g.invoke({"messages": [{"role": "user", "content": "second"}]}, cfg)["messages"][-1]["content"]
    assert "turn 1" in r1 and 'You said: "first"' in r1 and "Last time" not in r1
    assert "turn 2" in r2 and 'You said: "second"' in r2 and 'Last time you asked: "first"' in r2
    other = g.invoke({"messages": [{"role": "user", "content": "x"}]}, {"configurable": {"thread_id": "v:t"}})
    assert "turn 1" in other["messages"][-1]["content"]
    assert MockGraph().invoke({"messages": [{"role": "user", "content": "first"}]}, cfg)["messages"][-1]["content"] == r1


def test_mock_stream_filters_by_node_and_matches_invoke() -> None:
    cfg = {"configurable": {"thread_id": "u:s"}}
    chunks = list(MockGraph(stream_node="answer").stream({"messages": [{"role": "user", "content": "hi"}]}, cfg))
    assert chunks[0][1]["langgraph_node"] == "retrieve"
    answer = "".join(c.content for c, meta in chunks if meta["langgraph_node"] == "answer")
    expected = MockGraph().invoke({"messages": [{"role": "user", "content": "hi"}]}, cfg)["messages"][-1]["content"]
    assert answer == expected


def test_mock_stream_saves_turn() -> None:
    g = MockGraph()
    cfg = {"configurable": {"thread_id": "u:s"}}
    list(g.stream({"messages": [{"role": "user", "content": "one"}]}, cfg))
    assert "turn 2" in g.invoke({"messages": [{"role": "user", "content": "two"}]}, cfg)["messages"][-1]["content"]


def test_mock_stream_only_messages_mode() -> None:
    with pytest.raises(ValueError):
        list(MockGraph().stream({"messages": []}, {"configurable": {"thread_id": "x"}}, stream_mode="values"))


def test_mock_delay_is_blocking_sleep() -> None:
    import time

    g = MockGraph(delay_seconds=0.2)
    t0 = time.perf_counter()
    g.invoke({"messages": [{"role": "user", "content": "x"}]}, {"configurable": {"thread_id": "d"}})
    assert time.perf_counter() - t0 >= 0.2


# ------------------------------------------------------------------ load_graph


def test_load_graph_mock() -> None:
    g = load_graph(make_settings(use_mock_graph=True, mock_graph_delay_seconds=0.1, stream_node="n"))
    assert isinstance(g, MockGraph)


@pytest.fixture
def no_agent(monkeypatch):
    for name in list(sys.modules):
        if name == "agent" or name.startswith("agent."):
            monkeypatch.delitem(sys.modules, name)
    return monkeypatch


def test_load_real_graph_missing_names_module(no_agent, tmp_path) -> None:
    no_agent.syspath_prepend(str(tmp_path))  # empty dir: still no agent package
    with pytest.raises(RuntimeError, match="agent.graph:graph"):
        load_graph(make_settings(use_mock_graph=False))


def test_load_real_graph_inner_missing_dependency_surfaces_as_is(no_agent, tmp_path) -> None:
    pkg = tmp_path / "agent"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "graph.py").write_text("import definitely_missing_dependency_xyz\n")
    no_agent.syspath_prepend(str(tmp_path))
    with pytest.raises(ModuleNotFoundError) as exc:
        load_graph(make_settings(use_mock_graph=False))
    assert exc.value.name == "definitely_missing_dependency_xyz"


def _fake_agent(monkeypatch, graph_obj) -> None:
    pkg = types.ModuleType("agent")
    pkg.__path__ = []  # type: ignore[attr-defined]
    mod = types.ModuleType("agent.graph")
    if graph_obj is not None:
        mod.graph = graph_obj  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agent", pkg)
    monkeypatch.setitem(sys.modules, "agent.graph", mod)


def test_load_real_graph_ok(no_agent) -> None:
    g = RecordingGraph()
    _fake_agent(no_agent, g)
    assert load_graph(make_settings(use_mock_graph=False)) is g


@pytest.mark.parametrize("obj", [None, object(), types.SimpleNamespace(invoke=lambda i, c: None)])
def test_load_real_graph_rejects_non_graph(no_agent, obj) -> None:
    _fake_agent(no_agent, obj)
    with pytest.raises(RuntimeError):
        load_graph(make_settings(use_mock_graph=False))


def test_mock_stream_delay_is_spread_per_chunk() -> None:
    import time

    delay = 0.6
    g = MockGraph(delay_seconds=delay)
    t0 = time.perf_counter()
    arrivals = []
    for chunk, meta in g.stream({"messages": [{"role": "user", "content": "x"}]}, {"configurable": {"thread_id": "p"}}):
        if meta["langgraph_node"] == "generate":
            arrivals.append(time.perf_counter() - t0)
    total = time.perf_counter() - t0
    assert delay * 0.95 <= total < delay * 2
    assert arrivals[0] < total * 0.5  # first token early, not after the whole delay
    gaps = [b - a for a, b in zip(arrivals, arrivals[1:])]
    assert min(gaps) > 0.001  # every chunk waited: spread, not one blob
