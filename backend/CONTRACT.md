# WanderChat backend contracts

Two contracts live here:

1. **Graph contract** (backend and Person B's LangGraph agent): what the backend sends
   to the graph, what it expects back, and how it calls it.
2. **HTTP contract** (backend and the frontend): request/response shapes, auth header,
   and the error envelope.

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
    config={"configurable": {"thread_id": "<user_id>:<client_thread_id>"}},
)
```

- Exactly **one** new user message per call. Earlier turns are **not** re-sent. The graph's
  checkpointer must keep history per `thread_id` (as `add_messages` + `MemorySaver` /
  `MongoDBSaver` do).
- The state key is `messages`. Messages arrive as role/content dicts, which LangGraph's
  `add_messages` converts to `HumanMessage`.

### thread_id namespacing (security)

- The graph thread id is always `f"{user_id}:{thread_id}"`, where `user_id` comes from
  the verified access token and `thread_id` comes from the client (or is a server-generated
  `uuid4().hex`).
- This stops user B from reading user A's conversation memory by guessing a thread id.
  **Treat the configurable `thread_id` as opaque.** Do not parse it, and do not key
  memory on anything else.
- Client thread ids match `^[A-Za-z0-9_-]{1,64}$`, so they never contain `:`.
  Everything after the last `:` is the client's id, which helps when debugging.
- If you persist checkpoints in MongoDB, it must use the **same** `MONGO_URI`/database
  (one instance, one DB). Talk to the backend lead before adding any collection. Today
  the agreed collections are only `travel_tips` and `users`.

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
Allowed methods are `GET, POST, OPTIONS`. Allowed request headers are `Authorization` and
`Content-Type`. `Retry-After` is exposed to scripts. No cookies are used
(`allow_credentials=false`).

### Authentication

Protected endpoints require `Authorization: Bearer <access_token>`. Tokens are never
sent in URLs. Access tokens come from `/auth/login` and `/auth/refresh` (see below).
They are validated locally (signature, expiry, type), with no database lookup.

### `POST /chat` (protected)

Request:

```json
{ "message": "Plan 3 days in Lisbon on a budget", "thread_id": "trip-lisbon-1" }
```

| Field | Type | Rules |
|---|---|---|
| `message` | string | Leading and trailing whitespace is stripped, then it must be 1..4000 chars. |
| `thread_id` | string, optional | `^[A-Za-z0-9_-]{1,64}$`. Omit it to start a new conversation. |

Unknown fields are rejected with 422 (e.g. a typo `threadid`).

Response `200`:

```json
{ "response": "Day 1: ...", "thread_id": "trip-lisbon-1" }
```

- `thread_id` is the client's id, echoed as sent. If it was omitted, the server generates
  one (32 hex chars). **Send it back on the next turn** to continue the conversation.
- Thread ids are private to each user. Two users using the same `thread_id` get separate
  conversations.

Errors: `401` (missing, invalid or expired token), `422` (bad body), `429` (more than
`RATE_LIMIT_CHAT` requests per user, default 30 per 60 s, with a `Retry-After` header), and
`502` `upstream_error` (the agent failed). `/chat` and `/chat/stream` share **one**
per-user `chat` budget. A request rejected with 401 is not counted, because the limit
is keyed by the authenticated user.

Example:

```bash
curl -X POST http://localhost:8000/chat \
  -H "Authorization: Bearer $ACCESS_TOKEN" -H "Content-Type: application/json" \
  -d '{"message": "Plan 3 days in Lisbon", "thread_id": "trip-1"}'
```

PowerShell:

```powershell
Invoke-RestMethod -Method Post http://localhost:8000/chat `
  -Headers @{ Authorization = "Bearer $env:ACCESS_TOKEN" } -ContentType "application/json" `
  -Body '{"message": "Plan 3 days in Lisbon", "thread_id": "trip-1"}'
```

### `POST /chat/stream` (protected, Server-Sent Events)

This is the same conversation as `/chat`, but the reply arrives token by token. It uses the
same request body (`ChatRequest`), the same `Authorization: Bearer` header and the
same per-user `chat` rate limit (shared with `/chat`). It is **POST** because the token
must never go in a URL, so browser `EventSource` (GET-only, no headers) cannot be used.
Use `fetch` and read the body stream (example below).

Request: identical to `/chat`:

```json
{ "message": "Plan 3 days in Lisbon on a budget", "thread_id": "trip-lisbon-1" }
```

**Before the stream starts**, failures are ordinary JSON error envelopes with their normal
status: `401`, `422` and `429` (with `Retry-After`). Check `response.ok` and the
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
async function streamChat(accessToken, message, threadId, onToken) {
  const res = await fetch("http://localhost:8000/chat/stream", {
    method: "POST",
    headers: { "Authorization": `Bearer ${accessToken}`, "Content-Type": "application/json" },
    body: JSON.stringify(threadId ? { message, thread_id: threadId } : { message }),
  });
  if (!res.ok) throw (await res.json()).error;          // 401 / 422 / 429 envelope
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
curl.exe -N http://localhost:8000/chat/stream -H "Authorization: Bearer $env:ACCESS_TOKEN" `
  -H "Content-Type: application/json" --data-binary "@body.json"
```

bash:

```bash
curl -N http://localhost:8000/chat/stream -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" -d '{"message": "Plan 3 days in Lisbon", "thread_id": "trip-1"}'
```

`-N` turns off curl's output buffering. Without it, the tokens appear all at once at the end.

Local-dev note (Windows + Docker Desktop): when measured from the Windows host through
the published port, the first bytes of a stream were observed to arrive about 1 s late,
with the first few events grouped together. After that they arrive incrementally. The
same request made from a container (directly, or through `host.docker.internal`)
shows no such delay. This comes from the host port forwarding, not from the API. Measure
first-token time from inside the compose network.

### `GET /health` (public)

- `200` → `{"status": "ok", "mongo": "up"}`
- `503` → `{"status": "degraded", "mongo": "down"}` (the MongoDB ping failed or took more
  than about 2 s)

This endpoint is deliberately **not** wrapped in the error envelope. It is a status report
for probes such as the compose healthcheck.

### Error envelope (every other non-2xx response)

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
- `401` responses carry `WWW-Authenticate: Bearer`. `429` responses carry `Retry-After: <seconds>`.

| HTTP | `code` | When |
|---|---|---|
| 401 | `not_authenticated` | No bearer token. |
| 401 | `invalid_token` | Token is malformed, has a bad signature, is expired or is the wrong type. |
| 401 | `invalid_credentials` | Login with a wrong email or password (same body for both). |
| 404 | `not_found` | Unknown path. |
| 405 | `method_not_allowed` | Wrong method (with an `Allow` header). |
| 409 | `email_taken` | Signup with an email that is already registered. |
| 422 | `validation_error` | Body is missing, malformed, has unknown fields or breaks a constraint. |
| 429 | `rate_limited` | Rate limit exceeded. |
| 500 | `internal_error` | Unexpected server error. The message is generic and the details are logged server-side. |
| 502 | `upstream_error` | The travel agent (graph) raised. |

Frontend guidance: switch on `error.code`, not on `message`. Messages are for humans and
may change.

---

## Auth endpoints

All four endpoints take a JSON body. Unknown fields get a 422. Tokens go only in JSON bodies
and in the `Authorization: Bearer` header, **never in URLs or query strings**. The server
ignores query parameters.

### Tokens

| Token | Lifetime | Use |
|---|---|---|
| access token | 15 min (`ACCESS_TOKEN_TTL_MINUTES`) | `Authorization: Bearer <access_token>` on protected endpoints (`/chat`). Checked locally (signature, expiry and type). |
| refresh token | 7 days (`REFRESH_TOKEN_TTL_DAYS`) | Only as the body of `/auth/refresh` and `/auth/logout`. **Single use**: every refresh returns a new one. |

Both are HS256 JWTs. Treat them as opaque strings: do not parse them client-side.
`expires_in` tells you when the access token expires. A user has at most **5** live
refresh sessions (devices or logins). A 6th login evicts the oldest.

Email addresses are trimmed and lowercased, so `" Alice@Example.com"` and
`alice@example.com` are the same account.

### Rate limits (per client IP, sliding 60 s window)

| Endpoint | Default | Env |
|---|---|---|
| `/auth/signup` | 5 / 60 s | `RATE_LIMIT_SIGNUP` |
| `/auth/login` | 10 / 60 s | `RATE_LIMIT_LOGIN` |
| `/auth/refresh` and `/auth/logout` (shared counter) | 30 / 60 s | `RATE_LIMIT_REFRESH` |

What counts toward a limit:

- **Counted:** every request whose body parses as JSON (or that has no body), including
  ones that then fail schema validation with a 422. Examples are missing fields, wrong
  types, unknown fields and a bad email or password format.
- **Not counted:** a body that is **not parseable JSON**. FastAPI rejects it with
  `422 validation_error` (detail `type` = `json_invalid`) before route dependencies run,
  so the limiter never sees it. This is accepted: such a request does no bcrypt and no
  database work.

The same rules apply to the per-user `/chat` limits. Going over a limit returns
`429 rate_limited` with a `Retry-After: <seconds>` header.

### `POST /auth/signup` → `201`

```json
{ "email": "alice@example.com", "password": "correct horse battery" }
```

- `email`: a valid address, normalized as described above.
- `password`: **8 to 72 bytes of UTF-8** (bcrypt's limit; we reject longer passwords rather
  than truncate them) and no NUL characters.

Response `201`. This returns the public user and **no tokens**. Call `/auth/login` next.

```json
{ "id": "6ae4438adafc08ce8666cf6e", "email": "alice@example.com", "created_at": "2026-10-30T07:13:14.451000Z" }
```

| HTTP | `code` | When |
|---|---|---|
| 409 | `email_taken` | The email is already registered. |
| 422 | `validation_error` | Bad email, password outside policy, missing or unknown fields. |
| 429 | `rate_limited` | Over the signup limit. |

### `POST /auth/login` → `200`

```json
{ "email": "alice@example.com", "password": "correct horse battery" }
```

Login accepts any non-empty password of up to 1024 characters. The signup policy is not
checked here, so a password that breaks the policy just fails like any other wrong password.

Response `200`:

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "refresh_token": "eyJhbGciOiJIUzI1NiIs...",
  "token_type": "bearer",
  "expires_in": 900
}
```

| HTTP | `code` | When |
|---|---|---|
| 401 | `invalid_credentials` | Unknown email **or** wrong password. The body is identical in both cases. |
| 422 | `validation_error` | Malformed body. |
| 429 | `rate_limited` | Over the login limit. |

### `POST /auth/refresh` → `200`

```json
{ "refresh_token": "eyJhbGciOiJIUzI1NiIs..." }
```

Response `200`: a **new** pair with the same shape as login. The refresh token you sent is
now dead. Store the new one and throw the old one away.

Every rejected refresh gets the same response: `401 invalid_token` with the same body.
What happens on the server differs:

| Case | Response | Other sessions of the user |
|---|---|---|
| Malformed, forged, wrong-type (e.g. an access token) or expired token (expired means `now >= exp`) | 401 `invalid_token` | untouched |
| Token whose session was **evicted** (a 6th login pushed it out) | 401 `invalid_token` | untouched |
| Token that was **logged out** (`/auth/logout`) | 401 `invalid_token` | untouched |
| Validly signed token that was never stored (e.g. user deleted) | 401 `invalid_token` | untouched |
| Token that was **already exchanged** at `/auth/refresh` (replay), including the loser of two parallel refreshes with the same token | 401 `invalid_token` | **all revoked**: every device must log in again |

| HTTP | `code` | When |
|---|---|---|
| 401 | `invalid_token` | Any row of the table above. |
| 422 | `validation_error` | Malformed body. |
| 429 | `rate_limited` | Over the refresh limit. |

**Reuse detection.** The server remembers the last 20 refresh tokens it exchanged per user
(until each one expires). If one of them is presented again, the server assumes it was stolen
and **revokes every session of that user**. That one revocation also clears the memory of
exchanged tokens, so replaying the same stolen token again later is a plain 401 and does not
keep logging the user out. Access tokens already issued stay valid until they expire (at most
15 min). So clients must:

1. Refresh **one at a time** (single-flight). If several requests get 401 together, run
   one `/auth/refresh` and let the others wait for its result. Two parallel refreshes with
   the same token count as a replay and log the user out everywhere.
2. Persist the new refresh token **before** using the new access token.

### `POST /auth/logout` → `204`

```json
{ "refresh_token": "eyJhbGciOiJIUzI1NiIs..." }
```

This revokes that one refresh session (that device). It is idempotent: logging out with an
already revoked or expired (but genuine) refresh token also returns `204`, with no body.
Access tokens already issued stay valid until they expire (at most 15 min), because access
tokens are never checked against the database. The client should discard its access token
at the same time.

| HTTP | `code` | When |
|---|---|---|
| 401 | `invalid_token` | Malformed, forged or wrong-type token. |
| 422 | `validation_error` | Malformed body. |
| 429 | `rate_limited` | Over the refresh/logout limit. |

### Using protected endpoints

```
Authorization: Bearer <access_token>
```

| HTTP | `code` | Client action |
|---|---|---|
| 401 | `not_authenticated` | No bearer header was sent. Attach the token. |
| 401 | `invalid_token` | Expired or invalid access token. Call `/auth/refresh` once (single-flight) and retry. If refresh also returns 401, send the user to login. |

All 401 responses carry `WWW-Authenticate: Bearer`.

Recommended client loop: call the API. On `401 invalid_token`, refresh once, then retry once.
If that fails, show the login screen. A client can also refresh shortly before `expires_in`
runs out.

Example (PowerShell):

```powershell
$t = Invoke-RestMethod -Method Post http://localhost:8000/auth/login -ContentType "application/json" `
  -Body '{"email": "alice@example.com", "password": "correct horse battery"}'
Invoke-RestMethod -Method Post http://localhost:8000/auth/refresh -ContentType "application/json" `
  -Body (@{ refresh_token = $t.refresh_token } | ConvertTo-Json)
```
