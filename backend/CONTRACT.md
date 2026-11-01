# WanderChat backend contracts

Two contracts live here:

1. **Graph contract** (backend and Person B's LangGraph agent): what the backend sends
   to the graph, what it expects back, and how it calls it.
2. **HTTP contract** (backend and the frontend): request/response shapes and the
   error envelope. There is no login: every endpoint is public.

The adapter that implements the backend side of (1) is `backend/graph.py`.

---

## 1. Graph contract (for Person B)

### Where the graph must live

- Module `agent.graph`, attribute `graph`. The backend does
  `from agent.graph import graph` (lazily, at app startup).
- `agent/` is a top-level package at the **repo root**, next to `backend/`, so it is
  importable when the app runs from the repo root (`uvicorn backend.main:app`) and in
  the Docker image (build context = repo root).
- `graph` is a compiled LangGraph graph (`builder.compile(checkpointer=...)`), built
  once at import time and reused for every request. The backend never builds prompts,
  calls tools or does retrieval. That is all inside the graph.
- It must provide `invoke(input, config)` and `stream(input, config, stream_mode="messages")`
  (the `TravelGraph` Protocol in `backend/graph.py`).

### Switching from the mock to the real graph

| Env var | Default | Meaning |
|---|---|---|
| `USE_MOCK_GRAPH` | `true` | `true`: use the in-process `MockGraph`. `false`: import `agent.graph:graph`. |
| `MOCK_GRAPH_DELAY_SECONDS` | `0` | Mock only: blocking `time.sleep` totalling this many seconds per call (spread across chunks when streaming), to imitate latency. |
| `STREAM_NODE` | `generate` | Name of the graph node whose LLM tokens go to the user when streaming. |

If `USE_MOCK_GRAPH=false` and `agent.graph` cannot be imported, startup fails with
an error naming `agent.graph:graph`. If the import fails because of a missing
dependency *inside* your module, that original `ModuleNotFoundError` is raised as-is.

### Input (what we pass)

```python
graph.invoke(
    {"messages": [{"role": "user", "content": "<the user's message, stripped, 1..4000 chars>"}]},
    config={"configurable": {"thread_id": "<client_thread_id>"}},
)
```

- Exactly **one** new user message per call. Earlier turns are **not** re-sent. The graph's
  checkpointer must keep history per `thread_id` (as `add_messages` + `InMemorySaver` does).
- The state key is `messages`. Messages arrive as role/content dicts, which LangGraph's
  `add_messages` converts to `HumanMessage`.

### thread_id

- The graph thread id is the client's `thread_id`, or a server-generated `uuid4().hex`
  when the client omits it. **Treat it as opaque.**
- Client thread ids match `^[A-Za-z0-9_-]{1,64}$`.
- There are no accounts, so anyone holding a thread id can continue that conversation.
  Server-generated ids are random 128-bit values. History lives in memory and is lost on restart.

### Output (what we read)

- `invoke` returns the final state dict. The backend reads **only**
  `result["messages"][-1]` and treats it as the assistant's reply.
- That last message may be a LangChain message object (`.content`) or a dict
  (`["content"]`). The content may be:
  - a `str`, or
  - a list of parts. The backend joins `str` parts and `{"type": "text", "text": ...}`
    parts and ignores all other parts.
- The final message must be the assistant's answer to the user, **not** a tool call or
  tool result. End the graph on the node that produces the answer.

### Sync-ness and how we call it

- The graph is assumed **synchronous**. `/chat` calls
  `await starlette.concurrency.run_in_threadpool(graph.invoke, input, config)`, so a
  slow LLM call never blocks the event loop. Several users' requests run concurrently
  on threadpool workers (AnyIO default: 40).
- So `invoke`/`stream` **must be thread-safe** for concurrent calls on *different*
  thread ids. Compiled LangGraph graphs with the stock checkpointers are. Do not keep
  per-request state in module globals.
- Do not call `asyncio.run(...)` or rely on a running event loop inside the graph. It
  runs on a worker thread that has no loop.

### Streaming contract (used by `POST /chat/stream`)

The input and config are exactly the same as for `invoke` (same namespaced `thread_id`).
`backend/streaming.py` does the equivalent of:

```python
chunks = graph.stream(input, config, stream_mode="messages")   # called on a worker thread
for chunk, metadata in chunks:                                 # each next() on a worker thread
    if metadata.get("langgraph_node") == STREAM_NODE and text_of(chunk):
        send_token(text_of(chunk))
```

- `stream_mode="messages"` yields `(message_chunk, metadata)` 2-tuples.
  `message_chunk.content` (or `["content"]` for a dict) is a token or partial text: a
  `str`, or the list-of-parts form above. `metadata["langgraph_node"]` names the node
  that produced it.
- The backend forwards **only** chunks where `metadata["langgraph_node"] == STREAM_NODE`
  (default `generate`) **and** the text is non-empty. It drops everything else, such as
  tool-calling or routing LLM calls and empty tool-call chunks.
  **Name your final answer node `generate`**, or tell the backend lead the name so
  `STREAM_NODE` can be set. If no chunk matches, the user gets zero tokens followed by
  `done`.
- Both `graph.stream(...)` and every `next()` run on threadpool workers
  (`run_in_threadpool`, then `iterate_in_threadpool`), never on the event loop.
- **Client disconnect:** the backend stops calling `next()`. A `next()` already running
  completes (a thread cannot be killed). Then the backend calls the generator's `close()`,
  so a `try/finally` in your stream code runs promptly. The turn may then be only partly
  checkpointed. That is acceptable, but your code must not crash on it.
- After streaming finishes, the checkpointer must have saved the turn, just as with
  `invoke`. The next turn on the same thread must see it.
- Any exception raised while creating or iterating the stream (including a malformed,
  non-2-tuple item) ends the stream with an `error` event. See the HTTP contract.
- Latency target: **< 500 ms to the first streamed token**.

### Exceptions

- Any exception from `invoke` (or from reading its result, e.g. no `messages`) is logged
  server-side with a traceback, the user id and the message **length**, but never
  the message text. The client gets **502** `upstream_error`
  ("The travel agent failed to respond.").
- Do not put user message text or secrets into exception messages. Tracebacks are
  logged.
- During streaming, the same logging applies, plus the number of tokens already sent.
  The client receives an `error` event carrying the `upstream_error` envelope (HTTP
  status is already 200).
- Errors are not retried by the backend.

### The mock (`MockGraph`)

- The reply is deterministic and echoes the message:
  `[mock itinerary, turn N] You said: "<msg>". Last time you asked: "<prev>". Day 1: ...`
- It keeps history in memory per thread id (lost on restart), so multi-turn behaviour can
  be demonstrated before the real graph exists.
- `MOCK_GRAPH_DELAY_SECONDS` uses a blocking `time.sleep`, like the real graph, so
  tests can show that concurrent chats do not block the event loop. `invoke` sleeps the
  full amount once. `stream` takes the same total time, split evenly before every chunk,
  so incremental delivery is observable.
- `stream` first yields one chunk from node `retrieve` (which must be filtered out),
  then the reply word by word from node `STREAM_NODE`.

---

## 2. HTTP contract (for the frontend)

Base URL (local): `http://localhost:8000`. All request and response bodies are JSON.
Every endpoint that takes input takes a **JSON body**. Query parameters are never read.

CORS: origins from `CORS_ORIGINS` (default `http://localhost:8501,http://localhost:3000`).
Allowed methods are `GET, POST, OPTIONS`. The allowed request header is `Content-Type`. `Retry-After` is exposed to scripts. No cookies are used
(`allow_credentials=false`).

### `POST /chat`

Request:

```json
{ "message": "Plan 3 days in Lisbon on a budget", "thread_id": "trip-lisbon-1" }
```

| Field | Type | Rules |
|---|---|---|
| `message` | string | Leading and trailing whitespace is stripped, then it must be 1..4000 chars. |
| `thread_id` | string, optional | `^[A-Za-z0-9_-]{1,64}$`. Omit it to start a new conversation. |
| `locale` | string, optional | Browser language (BCP 47, e.g. `en-IN`). Passed to the graph as `user_locale` so prices use the traveller's home currency. |
| `timezone` | string, optional | IANA timezone (e.g. `Asia/Kolkata`). Passed to the graph as `user_timezone`. |

Unknown fields are rejected with 422 (e.g. a typo `threadid`).

Response `200`:

```json
{ "response": "Day 1: ...", "thread_id": "trip-lisbon-1" }
```

- `thread_id` is the client's id, echoed as sent. If it was omitted, the server generates
  one (32 hex chars). **Send it back on the next turn** to continue the conversation.

Errors: `422` (bad body), `429` (more than `RATE_LIMIT_CHAT` requests per client IP,
default 30 per 60 s, with a `Retry-After` header), and `502` `upstream_error` (the agent
failed). `/chat` and `/chat/stream` share **one** per-IP `chat` budget.

Example:

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Plan 3 days in Lisbon", "thread_id": "trip-1"}'
```

PowerShell:

```powershell
Invoke-RestMethod -Method Post http://localhost:8000/chat `
  -ContentType "application/json" `
  -Body '{"message": "Plan 3 days in Lisbon", "thread_id": "trip-1"}'
```

### `POST /chat/stream` (Server-Sent Events)

This is the same conversation as `/chat`, but the reply arrives token by token. It uses the
same request body (`ChatRequest`) and the same per-IP `chat` rate limit (shared with
`/chat`). It is **POST** because it takes a JSON body, so browser `EventSource` (GET-only)
cannot be used. Use `fetch` and read the body stream (example below).

Request: identical to `/chat`:

```json
{ "message": "Plan 3 days in Lisbon on a budget", "thread_id": "trip-lisbon-1" }
```

**Before the stream starts**, failures are ordinary JSON error envelopes with their normal
status: `422` and `429` (with `Retry-After`). Check `response.ok` and the
`Content-Type` before reading events.

**Success**: `200`. The response headers are:

```
Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-cache
X-Accel-Buffering: no
```

The body is a sequence of SSE events. Each event is an `event:` line, then one `data:`
line carrying **one JSON value**, then a blank line. Newlines inside content are
JSON-escaped (`\n`), so a `data:` line never spans lines.

| Event | `data` JSON | When |
|---|---|---|
| `token` | `{"content": "<text>"}` | Zero or more times. Append `content` to the reply. Never empty. |
| `done` | `{"thread_id": "<id>"}` | Exactly once on success, as the last event. Echoes the client's `thread_id`, or the server-generated one (32 hex chars) if it was omitted. |
| `error` | `{"error": {"code": "upstream_error", "message": "The travel agent failed to respond.", "details": null}}` | Instead of `done` if the agent fails mid-stream. It is the normal error envelope. |

Exactly one of `done` and `error` ends every stream that the server finishes, and the
connection then closes. **The HTTP status of a mid-stream failure is still `200`**,
because headers were already sent. A client must treat `error` as failure, and must
treat a stream that ends with neither `done` nor `error` (network drop or server crash)
as failure too. Tokens already received may be shown as partial text. The turn may or
may not be in the thread's memory, so a retry on the same `thread_id` can leave the
message in the history twice.

Example stream:

```
event: token
data: {"content": "Day "}

event: token
data: {"content": "1: arrive\nand explore"}

event: done
data: {"thread_id": "trip-lisbon-1"}

```

Latency target: **< 500 ms to the first streamed token**.

If the client disconnects (aborts `fetch`, closes the tab), the server stops reading from
the agent. The token that is in flight is discarded.

JavaScript (browser or Node 18+):

```js
async function streamChat(message, threadId, onToken) {
  const res = await fetch("http://localhost:8000/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(threadId ? { message, thread_id: threadId } : { message }),
  });
  if (!res.ok) throw (await res.json()).error;          // 422 / 429 envelope
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) throw { code: "stream_interrupted", message: "Stream ended early." };
    buf += value;
    let sep;
    while ((sep = buf.indexOf("\n\n")) !== -1) {          // one SSE event per blank line
      const block = buf.slice(0, sep); buf = buf.slice(sep + 2);
      const event = block.match(/^event: (.*)$/m)?.[1];
      const data = JSON.parse(block.match(/^data: (.*)$/m)?.[1] ?? "null");
      if (event === "token") onToken(data.content);
      else if (event === "done") return data.thread_id;  // keep for the next turn
      else if (event === "error") throw data.error;
    }
  }
}
```

PowerShell (`curl.exe`, not the `curl` alias). The body goes in a file, because Windows
PowerShell 5.1 strips the inner double quotes from JSON passed to native programs:

```powershell
'{"message": "Plan 3 days in Lisbon", "thread_id": "trip-1"}' | Set-Content -Encoding ascii body.json
curl.exe -N http://localhost:8000/chat/stream `
  -H "Content-Type: application/json" --data-binary "@body.json"
```

bash:

```bash
curl -N http://localhost:8000/chat/stream \
  -H "Content-Type: application/json" -d '{"message": "Plan 3 days in Lisbon", "thread_id": "trip-1"}'
```

`-N` turns off curl's output buffering. Without it, the tokens appear all at once at the end.

Local-dev note (Windows + Docker Desktop): when measured from the Windows host through
the published port, the first bytes of a stream were observed to arrive about 1 s late,
with the first few events grouped together. After that they arrive incrementally. The
same request made from a container (directly, or through `host.docker.internal`)
shows no such delay. This comes from the host port forwarding, not from the API. Measure
first-token time from inside the compose network.

### `GET /health`

- `200` → `{"status": "ok"}` while the process is up.

### Error envelope (every non-2xx response)

```json
{
  "error": {
    "code": "validation_error",
    "message": "Request validation failed.",
    "details": [
      { "loc": ["body", "thread_id"], "msg": "String should match pattern '^[A-Za-z0-9_-]{1,64}$'", "type": "string_pattern_mismatch" }
    ]
  }
}
```

- `details` is a list only for `validation_error`. Otherwise it is `null`. Validation
  details **never echo input values**.
- `429` responses carry `Retry-After: <seconds>`.

| HTTP | `code` | When |
|---|---|---|
| 404 | `not_found` | Unknown path. |
| 405 | `method_not_allowed` | Wrong method (with an `Allow` header). |
| 422 | `validation_error` | Body is missing, malformed, has unknown fields or breaks a constraint. |
| 429 | `rate_limited` | Rate limit exceeded. |
| 500 | `internal_error` | Unexpected server error. The message is generic and the details are logged server-side. |
| 502 | `upstream_error` | The travel agent (graph) raised. |

Frontend guidance: switch on `error.code`, not on `message`. Messages are for humans and
may change.
