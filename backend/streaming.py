"""Server-Sent Events for ``POST /chat/stream``. See backend/CONTRACT.md.

The graph's ``stream`` is a SYNC generator. It is created and advanced only on threadpool
workers (``run_in_threadpool`` / ``iterate_in_threadpool``), never on the event loop.

Client disconnect: uvicorn advertises ASGI spec 2.3, so Starlette's StreamingResponse
runs a ``listen_for_disconnect`` task beside the body iterator. When ``http.disconnect``
arrives, it cancels this generator at its current ``await``, and no further chunk is
pulled from the graph. We do not also poll ``request.is_disconnected()``, because it
would compete with that listener for ``receive``. A worker thread already inside
``next()`` cannot be killed: it finishes that one chunk, and its result is discarded.
The sync generator is then closed (shielded), so the graph's own cleanup runs right away.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

import anyio
from starlette.concurrency import iterate_in_threadpool, run_in_threadpool

from backend.graph import TravelGraph, build_graph_call, content_to_text
from backend.schemas.common import ErrorBody, ErrorResponse

log = logging.getLogger(__name__)

SSE_MEDIA_TYPE = "text/event-stream"
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def sse_event(event: str, data: Any) -> str:
    # json.dumps escapes newlines in content, so a token can never break SSE framing.
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _chunk_text(chunk: Any) -> str:
    content = chunk.get("content", "") if isinstance(chunk, dict) else getattr(chunk, "content", "")
    return content_to_text(content)


async def stream_chat_events(
    graph: TravelGraph, stream_node: str, user_id: str, thread_id: str, message: str
) -> AsyncIterator[str]:
    """Yield ``token``* then ``done``, or ``token``* then ``error`` if the graph fails."""
    graph_input, config = build_graph_call(user_id, thread_id, message)
    sent = 0
    chunks: Any = None
    try:
        chunks = await run_in_threadpool(graph.stream, graph_input, config, stream_mode="messages")
        async for chunk, metadata in iterate_in_threadpool(chunks):
            if (metadata or {}).get("langgraph_node") != stream_node:
                continue
            text = _chunk_text(chunk)
            if text:
                sent += 1
                yield sse_event("token", {"content": text})
    except Exception:
        # The 200 status is already sent, so the error travels as an in-band event.
        log.exception("Graph stream failed (user_id=%s, message_len=%d, tokens_sent=%d)", user_id, len(message), sent)
        body = ErrorResponse(error=ErrorBody(code="upstream_error", message="The travel agent failed to respond."))
        yield sse_event("error", body.model_dump())
        return
    finally:
        await _close_quietly(chunks)
    yield sse_event("done", {"thread_id": thread_id})


async def _close_quietly(chunks: Any) -> None:
    """Close the graph's sync generator so its own ``finally`` cleanup runs now, not at GC.

    Shielded, so it still runs when a client disconnect cancelled us. By then the in-flight
    ``next()`` has returned: anyio's ``to_thread.run_sync`` defers cancellation until the
    worker thread finishes. A ``ValueError`` ("generator already executing") is ignored.
    """
    close = getattr(chunks, "close", None)
    if close is None:
        return
    with anyio.CancelScope(shield=True):
        try:
            await run_in_threadpool(close)
        except ValueError:
            pass
        except Exception:  # cleanup must never mask the stream's outcome
            log.warning("Closing the graph stream raised", exc_info=True)
