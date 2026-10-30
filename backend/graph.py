"""Boundary to Person B's LangGraph agent. See backend/CONTRACT.md.

The backend never builds prompts or does retrieval; it hands the user's message to a
graph object and returns the final message's text. The graph is synchronous, so it is
always called off the event loop (``run_in_threadpool``).
"""

from __future__ import annotations

import importlib
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterator, Protocol, runtime_checkable

from starlette.concurrency import run_in_threadpool

from backend.config import Settings
from backend.errors import AppError

log = logging.getLogger(__name__)

REAL_GRAPH_MODULE = "agent.graph"
REAL_GRAPH_ATTR = "graph"


@runtime_checkable
class TravelGraph(Protocol):
    """The subset of a compiled LangGraph ``StateGraph`` the backend relies on."""

    def invoke(self, input: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]: ...

    def stream(
        self, input: dict[str, Any], config: dict[str, Any], stream_mode: str = "messages"
    ) -> Iterator[tuple[Any, dict[str, Any]]]: ...


# --------------------------------------------------------------------------- mock


@dataclass(frozen=True)
class MockChunk:
    """Stand-in for a LangChain ``AIMessageChunk`` (only ``.content`` is used)."""

    content: str


class MockGraph:
    """Deterministic, in-memory stand-in for the real graph.

    - Echoes the user's message inside a canned itinerary.
    - Remembers history per ``thread_id`` (like a LangGraph checkpointer), so the reply
      reports the turn number and the previous message -- multi-turn is observable.
    - Sleeps ``delay_seconds`` with ``time.sleep``: deliberately blocking, like the real
      graph, so a test can prove the event loop is not blocked by concurrent chats.
    """

    def __init__(self, delay_seconds: float = 0.0, stream_node: str = "generate") -> None:
        self.delay_seconds = delay_seconds
        self.stream_node = stream_node
        self._lock = threading.Lock()  # invoke() runs on threadpool workers concurrently
        self._threads: dict[str, list[dict[str, str]]] = {}

    @staticmethod
    def _thread_id(config: dict[str, Any]) -> str:
        return str(config["configurable"]["thread_id"])

    def _reply_for(self, history: list[dict[str, str]], message: str) -> str:
        turn = sum(1 for m in history if m["role"] == "user")
        previous = [m["content"] for m in history if m["role"] == "user"][:-1]
        recall = f' Last time you asked: "{previous[-1]}".' if previous else ""
        return (
            f'[mock itinerary, turn {turn}] You said: "{message}".{recall} '
            "Day 1: arrive and explore the old town. "
            "Day 2: local food tour and a museum. "
            "Day 3: day trip to the nearby coast."
        )

    def _add_user_messages(self, thread_id: str, input: dict[str, Any]) -> tuple[list[dict[str, str]], str]:
        new = [{"role": m["role"], "content": m["content"]} for m in input.get("messages", [])]
        with self._lock:
            history = self._threads.setdefault(thread_id, [])
            history.extend(new)
            snapshot = list(history)
        last_user = next((m["content"] for m in reversed(new) if m["role"] == "user"), "")
        return snapshot, last_user

    def _add_assistant(self, thread_id: str, text: str) -> list[dict[str, str]]:
        with self._lock:
            history = self._threads[thread_id]
            history.append({"role": "assistant", "content": text})
            return list(history)

    def invoke(self, input: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        thread_id = self._thread_id(config)
        history, message = self._add_user_messages(thread_id, input)
        if self.delay_seconds:
            time.sleep(self.delay_seconds)  # intentionally sync (see class docstring)
        return {"messages": self._add_assistant(thread_id, self._reply_for(history, message))}

    def stream(
        self, input: dict[str, Any], config: dict[str, Any], stream_mode: str = "messages"
    ) -> Iterator[tuple[MockChunk, dict[str, Any]]]:
        if stream_mode != "messages":
            raise ValueError("MockGraph only supports stream_mode='messages'")
        thread_id = self._thread_id(config)
        history, message = self._add_user_messages(thread_id, input)
        text = self._reply_for(history, message)
        words = text.split(" ")
        # The total delay matches invoke(), spread evenly before every chunk, so incremental
        # delivery is observable. Blocking sleep, like a real sync LLM stream.
        pause = self.delay_seconds / (len(words) + 1)
        if pause:
            time.sleep(pause)
        # A non-generating node's chunk: consumers must filter on langgraph_node.
        yield MockChunk("(retrieved 3 travel tips)"), {"langgraph_node": "retrieve"}
        for i, word in enumerate(words):
            if pause:
                time.sleep(pause)
            yield MockChunk(word if i == len(words) - 1 else word + " "), {"langgraph_node": self.stream_node}
        self._add_assistant(thread_id, text)


# --------------------------------------------------------------------------- loading


def load_graph(settings: Settings) -> TravelGraph:
    if settings.use_mock_graph:
        log.info("Using MockGraph (USE_MOCK_GRAPH=true)")
        return MockGraph(settings.mock_graph_delay_seconds, settings.stream_node)
    try:
        module = importlib.import_module(REAL_GRAPH_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name not in ("agent", REAL_GRAPH_MODULE):
            raise  # a dependency *inside* Person B's module is missing: surface it as-is
        raise RuntimeError(
            f"USE_MOCK_GRAPH=false but '{REAL_GRAPH_MODULE}' could not be imported. "
            f"Person B's package must expose '{REAL_GRAPH_MODULE}:{REAL_GRAPH_ATTR}' on the "
            "Python path (repo root). Set USE_MOCK_GRAPH=true to run without it."
        ) from exc
    graph = getattr(module, REAL_GRAPH_ATTR, None)
    if graph is None or not isinstance(graph, TravelGraph):
        raise RuntimeError(f"'{REAL_GRAPH_MODULE}.{REAL_GRAPH_ATTR}' must have invoke() and stream() methods")
    log.info("Using real graph from %s:%s", REAL_GRAPH_MODULE, REAL_GRAPH_ATTR)
    return graph


# --------------------------------------------------------------------------- calling


def graph_thread_id(user_id: str, thread_id: str) -> str:
    """Namespace the client's thread id per user so users cannot read each other's memory."""
    return f"{user_id}:{thread_id}"


def build_graph_call(user_id: str, thread_id: str, message: str) -> tuple[dict[str, Any], dict[str, Any]]:
    graph_input = {"messages": [{"role": "user", "content": message}]}
    config = {"configurable": {"thread_id": graph_thread_id(user_id, thread_id)}}
    return graph_input, config


def content_to_text(content: Any) -> str:
    """LangChain content is a str or a list of parts (str or {"type": "text", "text": ...})."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "".join(parts)
    raise TypeError(f"unsupported message content type: {type(content).__name__}")


def _final_text(result: Any) -> str:
    last = result["messages"][-1]
    content = last["content"] if isinstance(last, dict) else last.content
    return content_to_text(content)


async def run_chat(graph: TravelGraph, user_id: str, thread_id: str, message: str) -> str:
    graph_input, config = build_graph_call(user_id, thread_id, message)
    try:
        result = await run_in_threadpool(graph.invoke, graph_input, config)
        return _final_text(result)
    except Exception:
        # Never log message content (user data); length is enough to correlate.
        log.exception("Graph invoke failed (user_id=%s, message_len=%d)", user_id, len(message))
        raise AppError(502, "upstream_error", "The travel agent failed to respond.") from None
