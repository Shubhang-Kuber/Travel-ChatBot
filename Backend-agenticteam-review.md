# Backend Agentic Team Review: WanderChat

Scope: the backend layer (Person C). That covers FastAPI, auth, rate limiting, the Mongo connection plus the `users` collection, and Docker.
State: built and tested on branch `backend`. **Nothing is committed**; the human owner commits.
Date: 2026-10-30.

---

## 1. Methodology

### Team structure
- **`backend-lead`** (the orchestrating session). It wrote one binding brief: the hard constraints, every lead decision and a file-ownership map. It dispatched each subagent with that brief, reviewed every report against the spec, issued rulings, and wrote this file.
- **`api-architect`**: the FastAPI app, config, the DB client, the error envelope, the rate limiter, the graph adapter, `/chat`, `/chat/stream`, `/health` and `CONTRACT.md`.
- **`auth-engineer`**: signup, login, refresh and logout, passwords, JWTs, the `users` repository and `get_current_user`.
- **`infra-engineer`**: the Dockerfile, compose, requirements, `.env.example`, `.gitignore` and `backend/README.md`.
- **`test-engineer`**: `backend/tests/`, `pytest.ini` and `scripts/`.
- **Final reviewer**: a fresh, independent agent that reviewed code and security for the whole implementation. It had no edit rights.

### Why it was split this way
- The split follows the three risk surfaces: the HTTP boundary, auth (where "it works" is not the bar) and reproducibility. Each surface has a different failure mode and a different reviewer mindset.
- Ownership was assigned per file before any code was written. No file had two owners. An agent that needed a change in a file it did not own reported it to the lead instead of editing.

### Build order actually followed
The `subagent-driven-development` skill overrides the prompt's "api and auth in parallel": it says *never dispatch multiple implementation subagents in parallel*. So the implementers ran one at a time, with a lead spec review between tasks.
1. The lead read `techinfo.md`, the diagram and the skills, then wrote the brief.
2. `api-architect` Task 1: the core app and non-streaming `/chat`. It built against stubs for auth (marked TEMPORARY STUB) and against `MockGraph`.
3. `auth-engineer` replaced the stubs with real auth.
4. `infra-engineer`:
   - The first run was stopped by the human (low laptop battery) after writing only the requirements files.
   - A fresh `infra-engineer` was dispatched with the same brief and took over those files.
5. `test-engineer` Tasks A, B and D in-process. It found 2 bugs and left them as `xfail(strict)` tests.
6. `api-architect` Task 2:
   - The two bug fixes.
   - Logging.
   - SSE streaming, built only after the non-streaming suite was green.
7. `infra-engineer` follow-up: `LOG_LEVEL` and the streaming docs.
8. `test-engineer` final pass:
   - Flipped the bug tests and added the SSE suite.
   - Task C: e2e, clean-machine, healthcheck and startup-order checks, plus the smoke scripts.
   - Task D against the live stack.
9. The final independent reviewer ran.
10. The lead wrote this review.

### How the mock boundaries kept agents unblocked
- **Graph (Person B):** `backend/graph.py` defines the `TravelGraph` protocol (sync `invoke` / `stream`).
  - `MockGraph` stands in for it. It is deliberately **sync**, using `time.sleep(MOCK_GRAPH_DELAY_SECONDS)`, so it behaves like the real LangGraph call. It keeps a per-thread history, so multi-turn semantics can be tested.
  - Switching to the real graph needs `USE_MOCK_GRAPH=false` and an `agent.graph:graph` module.
- **Auth, for `api-architect`:** it wrote stub `get_current_user`, `CurrentUser` and `ensure_indexes` files under the same public names, and `auth-engineer` later replaced them. Nothing outside those files changed.
- **Host tooling:** the host has **no Python**. Every agent verified in throwaway containers before infra existed, then through `docker compose --profile test run test`.

### Process notes (honest)
- **Reviews:**
  - The skill's per-task spec review was done by the lead.
  - The per-task *code-quality* review was replaced by one final independent review of the whole implementation. That deviates from the skill and was chosen to limit agent count and cost.
  - Every task was still gated by `test-engineer` results.
- **Concurrent compose runs:** one e2e run was killed by a concurrent `docker compose` stop on the same project. The cause was two agents using compose on one daemon. The unchanged rerun passed. Lesson: compose runs must be serialized.

---

## 2. Subagent Reports

### 2.1 `api-architect`
- **Files owned and produced:**
  - `backend/__init__.py`, `config.py`, `db.py`, `errors.py`, `ratelimit.py`, `dependencies.py`, `graph.py`, `streaming.py`, `main.py`
  - `routes/{__init__,chat,health}.py`, `schemas/{__init__,chat,common}.py`, `CONTRACT.md`
  - Temporary auth stubs, later replaced.
- **What it built:**
  - App factory `create_app(settings=None, mongo_client=None)`. The lifespan pings Mongo with backoff up to 30 s, then fails loudly; then it runs `ensure_indexes` and loads the graph.
  - CORS configured from env.
  - Error envelope with handlers for HTTP, validation, `AppError` and unhandled errors.
  - Pure-ASGI `UnhandledErrorMiddleware`, placed inside CORS.
  - In-process sliding-window limiter.
  - `MockGraph`, plus the `run_chat` adapter, which runs the graph through `run_in_threadpool`.
  - `POST /chat`, `POST /chat/stream` (SSE) and `GET /health`.
  - `LOG_LEVEL` and the backend log handler.
- **Independent decisions, with reasoning:**
  - **Module-level `app` is lazy** (PEP 562 `__getattr__`). Tests can import `create_app` without `JWT_SECRET` set, and `uvicorn backend.main:app` still resolves it.
  - **The rate-limit key function is itself a FastAPI dependency.** The per-user key reuses the cached `get_current_user`, so the token is never decoded twice, and an unauthenticated request gets 401 before any 429.
  - **CORS exposes `Retry-After`,** so browser clients can honour the backoff.
  - **The mock stream emits a `retrieve` chunk first.** That forces the SSE layer to actually apply the `stream_node` filter.
  - **`graph.stream()` itself is called on a worker thread** (and then iterated there). This guards against a graph that does sync work before the first yield.
  - **The graph generator gets a shielded `close()` after client disconnect.** Without it, the graph's cleanup waited for GC (verified: `finalized=False` before, `True` after).
  - **No SSE keep-alive comments.** Deferred: only an idle-timeout proxy would need them.
- **Tradeoffs accepted, with cost:**
  - **The rate limiter counts requests that then fail schema validation.** That is intended; unparseable JSON does not count (see BUG-1).
  - **`log.exception` on graph failure could include text that Person B puts into exception messages.** `CONTRACT.md` tells B not to put user content in exceptions.
  - **A disconnect cannot interrupt the in-flight `next()` on the worker thread.** The thread finishes the current chunk, about one chunk of wasted work.
- **Skills invoked:**
  - `systematic-debugging`:
    - BUG-2: Starlette source was read to locate `ServerErrorMiddleware` before writing the fix.
    - The "buffered stream" anomaly: four client paths were measured, which isolated it to Windows host port forwarding and the PowerShell pipe, not the server. That investigation also exposed the missing generator `close()`.
  - `prompt-master`: not invoked. The mock fixtures are deterministic text and did not need LLM-shaped prompts.
- **Flagged/escalated to the lead:**
  - Circular-import risk for auth: auth must not import `backend.dependencies`.
  - `techinfo.md` puts the graph at `backend/agent.py`, but it lives in Person B's `agent/` package.
  - App INFO logs were being dropped.
  - Starlette 1.10 deprecates `httpx` with `TestClient`.
  - The ~1 s time-to-first-byte from the Windows host.
  - The "~250 ms bcrypt" comment in `passwords.py`. Lead ruling: it is a CPU-cost note, not an API latency claim, so it is allowed.

### 2.2 `auth-engineer`
- **Files owned and produced:**
  - `backend/auth/{__init__,passwords,tokens,repository,service,dependencies}.py`
  - `backend/models/{__init__,user}.py`, `backend/schemas/auth.py`, `backend/routes/auth.py`
  - The auth section of `CONTRACT.md`, which the lead merged in.
- **What it built:**
  - **Passwords:** bcrypt cost 12, called directly, with a dummy-hash timing-parity path. Every bcrypt call runs on the threadpool.
  - **Tokens:** HS256 with `algorithms=["HS256"]` pinned, plus a second check of the header `alg`. It enforces the required claims (`sub` must match ObjectId hex, `type`, `iat`, `exp`, and `jti` for refresh) and has a 4096-char token cap.
  - **Repository:** the `users` collection with a unique `email` index. Refresh sessions are rotated in one atomic update.
  - **Service:** signup, login, refresh (with reuse detection, which revokes all sessions) and logout.
  - **`get_current_user`:** decodes the token locally, with zero DB access.
- **Independent decisions, with reasoning:**
  - **One atomic aggregation-pipeline update for rotation,** instead of the brief's `$pull` then `$push`.
    - The `$elemMatch` filter on a live jti is the gate.
    - There is no crash window that could leave a user with zero sessions.
    - It also prunes expired sessions and moves the active device to the end of the 5-slot list.
    - Lead accepted it, since it is strictly better.
  - **Logout semantics:**
    - An expired but genuine token gives 204.
    - An already-revoked token gives 204 and does **not** trigger revoke-all, so logout stays idempotent.
    - A forged token or an access token gives 401 `invalid_token`.
  - **Login does not apply the signup password policy** (only 1..1024 chars). A policy change cannot lock existing users out, and a policy violation at login gets the same 401 rather than a revealing 422.
  - **The whole email is lowercased,** not just the domain as `EmailStr` does.
  - **Logout shares the `refresh` rate bucket.** No separate bucket was added to config, which it does not own.
- **Tradeoffs accepted, with cost:**
  - **Revoke-all on reuse also fires when a benign client race happens** (two parallel refreshes). Clients must run refresh single-flight; this is documented.
  - **An access token stays valid for up to 15 minutes after logout or revocation.** That is the direct cost of constraint #1.
  - **Signup's 409 reveals that an email is registered.** Accepted, mitigated by a 5/min/IP limit.
  - **Importing `backend.auth` builds the dummy hash once,** about 250 ms of CPU.
- **Skills invoked:** `systematic-debugging`, for the one failure.
  - **Symptom:** logout with an expired token returned 401.
  - **Root cause:** python-jose 3.5.0 `_validate_claims` forces `verify_exp=True` whenever `require_exp=True`. Reproduced in isolation and confirmed in the library source.
  - **Fix:** one change, `require_exp` now follows `verify_exp`. `exp` is still enforced by its own type check.
- **Flagged:**
  - Merge the auth contract (done by the lead).
  - Pinned versions for infra.
  - python-jose is thinly maintained; PyJWT is a drop-in replacement for our ~10 lines.
  - Optional separate logout bucket.

### 2.3 `infra-engineer`
(The original instance was stopped mid-task on low battery. The replacement instance produced everything except the two requirements files, which it reviewed and re-verified unchanged.)
- **Files owned and produced:** `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `.env.example`, `.gitignore`, `requirements.txt`, `requirements-dev.txt` and `backend/README.md`.
- **What it built:**
  - **Multi-stage Dockerfile** (`base → deps → test-deps / src → runtime / test`):
    - Pinned to `python:3.12.16-slim-trixie`.
    - The deps layer depends only on `requirements.txt`. It installs with `--no-deps` followed by `pip check`, so an incomplete lock fails the build.
    - Runtime runs as non-root `app` (uid 10001) on read-only source and is 262 MB, with no tests or dev deps.
    - Stdlib urllib `HEALTHCHECK`; the CMD is `uvicorn ... --workers 1`.
  - **Compose:**
    - `mongo` is `mongodb/mongodb-atlas-local:8.0.34-20261029T122752Z` with three named volumes.
    - `backend` has `depends_on: condition: service_healthy`, `restart: unless-stopped`, a healthcheck and a fail-fast `JWT_SECRET`.
    - The `ollama`, `frontend` and `test` services are behind profiles.
  - **Docs:** a PowerShell-first README with bash dual-listing.
- **Independent decisions, with reasoning:**
  - **`--no-deps` plus `pip check`** proves the lock is complete, which is stronger than pinning alone.
  - **A third volume for `/data/mongot`.** The image only declares `/data/db` and `/data/configdb`, but mongot keeps search-index state in `/data/mongot`. Without the volume, Person A's vector index would be lost when the container is recreated.
  - **`hostname: mongo` plus `?directConnection=true`.** Atlas Local is a single-node replica set.
  - **Published ports are bound to `127.0.0.1` only.** Atlas Local runs without auth.
  - **Removed the `# syntax=docker/dockerfile:1` directive.** It is a floating tag fetched from the network on every build.
  - **`MONGO_HOST_PORT` and `BACKEND_HOST_PORT` overrides.** A native Windows MongoDB service on this laptop already holds 27017.
  - **The `test` service does not read `.env`,** so tests are hermetic. It uses an obviously fake inline secret.
- **Tradeoffs accepted, with cost:**
  - **Images are pinned by tag, not digest.** That keeps them portable across CPU architectures, at the risk of a re-pushed tag drifting.
  - **About 2 GB Atlas Local pull on first run.**
  - **Backend crash-loops about every 35 s while Mongo is down.** Deliberate: it fails loudly and lets the restart policy retry.
  - **One manual step on a fresh clone** (`.env` plus `JWT_SECRET`), per the lead ruling under constraint 8.
- **Skills invoked:** `systematic-debugging`, for two failures.
  - **Port bind failure.** Evidence: `Get-NetTCPConnection` showed `mongod` PID 5740 on 27017. Fix: a host-port override; the human's service was not stopped.
  - **`ModuleNotFoundError: backend` in the test container.** Evidence: `sys.path` lacked `/app`; the `pytest` console script does not add the cwd. Fix: `ENV PYTHONPATH=/app`.
- **Flagged:**
  - App INFO logs were dropped (since fixed by `api-architect`).
  - The test profile still needs `JWT_SECRET` to resolve, because compose interpolates the whole file before applying profiles.
  - The `ollama pull` README snippet was not executed.

### 2.4 `test-engineer`
- **Files owned and produced:**
  - `pytest.ini`, `backend/tests/coverage.ini`, `backend/tests/helpers.py`
  - Unit tests in `backend/tests/unit/` (10 modules), integration tests in `backend/tests/integration/` (7 modules incl. `conftest.py`), and `backend/tests/e2e/` (5 files).
  - `scripts/smoke.ps1`, `scripts/smoke.sh` and `scripts/ops-checks.ps1`.
- **What it built:** 529 tests (363 unit, 139 integration, 27 e2e), the smoke scripts and the ops-check scripts. Details are in §6.
- **Independent decisions, with reasoning:**
  - **The graph and Mongo client are overridden via `app.dependency_overrides`,** which is public API, rather than by mutating `app.state`.
  - **Exactly one deliberately implementation-coupled test.** It monkeypatches `decode_token`, because the brief asked for the token layer to be mocked.
  - **e2e tests are gated on `E2E_BASE_URL`, and timing tests also on `E2E_EXPECT_DELAY`.** A default `pytest` run is never flaky.
  - **e2e helpers wait out a 429 using its `Retry-After`** rather than weakening the real limits.
  - **A safety guard refuses to run integration tests unless `MONGO_DB` ends in `_test`.**
- **Tradeoffs accepted, with cost:**
  - **e2e runs leave random-email users in the live stack's `wanderchat` DB.**
  - **Timing runs need scratchpad compose overrides** (raised limits and a delay); `.env` was never edited.
- **Skills invoked:** `systematic-debugging`, for four investigations.
  - **BUG-1:** probed per body type, then traced to FastAPI's `routing.py`, which reads the body before resolving dependencies.
  - **BUG-2:** inspected `app.build_middleware_stack()`.
  - **The externally killed run:** exit codes 137/143 and `OOMKilled=false` ruled out OOM.
  - **Its own faulty assertion:** read the actual response and found the defect in the assertion, not the server.
- **Flagged:**
  - **BUG-1:** routed to the lead, resolved as a contract change.
  - **BUG-2:** routed to `api-architect`, fixed.
  - **The 40-thread AnyIO ceiling,** and that bcrypt shares that pool with the graph.
  - **Compose runs must be serialized** across agents.

---

## 3. Team Leader Decisions

Each decision has the format: Decision, Alternatives, Why, Cost.

1. **Access-token lifetime: 15 minutes, HS256.**
   - **Alternatives:** 5 min; 60 min; no expiry.
   - **Why:** 15 minutes is the conventional balance between the revocation gap and refresh frequency. Local validation (constraint 1) means a token cannot be revoked before it expires.
   - **Cost:** a stolen or logged-out access token stays usable for up to 15 minutes.
2. **Refresh tokens are in scope for v1** (7 days, with rotation on every use).
   - **Alternatives:** no refresh, forcing re-login every 15 minutes; long-lived access tokens.
   - **Why:** the diagram already specifies a "JWT refresh token". Short access tokens are only usable with refresh.
   - **Cost:** two more endpoints, session state, and reuse-detection logic.
3. **Refresh tokens are stored server-side and revocable, inside the user document** (`refresh_sessions: [{jti, expires_at}]`, max 5).
   - **Alternatives:** stateless refresh (cannot be revoked; a stolen token is valid for 7 days); a separate `refresh_tokens` collection (violates constraint 3's two-collection rule).
   - **Why:** logout and theft response require revocation. The DB is hit only on refresh and logout, which constraint 1 permits.
   - **Cost:**
     - One DB round-trip per refresh (about every 15 minutes per user).
     - A benign parallel refresh by one client triggers revoke-all, so clients must refresh single-flight.
     - Logging in on a 6th device evicts the oldest session.
4. **Rate limiting: in-process sliding window, hand-rolled (~90 lines), with no Redis and no slowapi.**
   - **Keys:** `/auth/*` per client IP (`request.client.host`; X-Forwarded-For is **not** trusted). `/chat*` per authenticated user id.
   - **Limits:** login 10/min, signup 5/min, refresh and logout 30/min, chat 30/min (all configurable through env).
   - **Alternatives:** Redis-backed; slowapi; per-IP everywhere.
   - **Why:**
     - There is one backend replica, so Redis would be premature infrastructure.
     - slowapi is a non-load-bearing dependency with decorator ergonomics that fight `Depends`.
     - A per-user key for chat is fairer behind a shared NAT. Login has no user yet, so it is keyed per IP.
   - **Cost:**
     - Limiter state is per process: lost on restart and not shared across replicas.
     - So uvicorn runs exactly **1 worker** and compose runs **1 replica**.
     - **Load balancing / horizontal scaling is therefore deferred** until the limiter moves to Redis. That is the honest "load balancing story".
5. **Event loop: the graph runs via `run_in_threadpool`; streaming uses `run_in_threadpool` + `iterate_in_threadpool`.** Bcrypt also runs on the threadpool. No sync call runs inline in an `async def`.
   - **Alternatives:** require Person B to ship `ainvoke` / `astream`; declare the handler as plain `def`.
   - **Why:**
     - LangGraph's sync API is what the plan uses.
     - The explicit threadpool keeps the async handler and still keeps the loop free.
     - Measured: 20 concurrent 0.5 s calls take 0.637 s on the live stack, not 10 s.
   - **Cost:**
     - AnyIO's default 40-thread pool caps graph concurrency at 40 per process; 50 calls take 2 waves (1.15 s).
     - Bcrypt shares that pool, so a chat burst delays logins.
     - If B later ships native async, switch to `ainvoke` / `astream`.
6. **Project layout: a single `backend/` package** (app at `backend.main:app`), with Person B in a separate `agent/` package and the Dockerfile at the repo root.
   - **Alternatives:** split services (an auth service and a chat service); the plan's flat `backend/main.py` + `agent.py`.
   - **Why:** one deployable, one team, no network hop between auth and chat. The `agent/` package keeps B's code out of our files, matching the README's no-shared-files rule.
   - **Cost:** auth and chat scale together. That is acceptable at this scale.
7. **One error envelope:** `{"error": {"code", "message", "details"}}` on every non-2xx response, except `/health`'s 503.
   - **Alternatives:** FastAPI's default `{"detail": ...}` (whose shape differs between 422 and other errors); RFC 7807 problem+json.
   - **Why:** clients get one shape to parse and a stable machine `code`. Validation details drop `input` and `ctx`, so passwords are never echoed.
   - **Cost:** custom handlers, plus a pure-ASGI middleware to keep 500s inside CORS (BUG-2).
   - `/health` is deliberately exempt: it is a probe status report.
8. **bcrypt is used directly, not `passlib[bcrypt]`.** This deviates from the listed stack.
   - **Alternatives:** passlib with `bcrypt` pinned to 4.0.1.
   - **Why:** passlib 1.7.4 has been unmaintained since 2020. It breaks with bcrypt ≥ 4.1 (version-detection error) and with bcrypt 5 (its 72-byte wrap-bug probe raises). It is a non-load-bearing wrapper.
   - **Cost:** about 20 lines of our own code. Passwords over 72 bytes are explicitly rejected at signup instead of being silently truncated.
9. **Mongo driver: PyMongo's native `AsyncMongoClient`.**
   - **Alternatives:** sync PyMongo (blocks the loop on auth paths); Motor (deprecated).
   - **Why:** async I/O on the auth paths.
   - **Cost:** no `mongomock` for unit tests, so DB tests are integration tests against real Mongo.
10. **`thread_id` is namespaced per user.** The graph receives `"{user_id}:{thread_id}"`.
    - **Alternative:** pass the client value through, as the plan's snippet does.
    - **Why:** otherwise any user who guesses a `thread_id` can read another user's conversation memory.
    - **Cost:** Person B must treat `thread_id` as opaque.
11. **Startup ordering: ping with backoff for up to 30 s, then fail startup.**
    - **Alternative:** start in a degraded mode and create indexes lazily.
    - **Why:** fail closed. Combined with `service_healthy` and `restart: unless-stopped`, it self-heals (measured: healthy 2.4 s after Mongo comes up).
    - **Cost:** a crash-loop about every 35 s while Mongo is down.
12. **Compose profiles: `ollama`, `frontend` and `test` are opt-in.**
    - **Why:**
      - No frontend exists yet, so an unconditional `build: ./frontend` would break a clean `up`.
      - Ollama is multi-GB and is the *offline toggle*.
    - **Cost:** the default `up` is mongo + backend only. Ollama is pinned (`0.40.13`) but was never pulled or run.
13. **One manual step on a fresh clone** (`Copy-Item .env.example .env`, then set `JWT_SECRET`).
    - **Alternative:** a committed default dev secret.
    - **Why:** constraint 8. A working default secret eventually ships to production.
    - **Cost:** "zero manual steps" holds only after `.env` exists.
14. **BUG-1 ruling: fix the contract, not the code.**
    - **Alternative:** move the limiter into middleware that runs before body parsing.
    - **Why:** unparseable JSON is rejected before any bcrypt or DB work, so it is not an amplification vector, and middleware would add complexity.
15. **Logout's `204` response.** Accepted, as defined by `auth-engineer`.

---

## 4. Constraint Enforcement

1. **JWT validated locally, with no DB lookup per request.**
   - **Status:** holds, verified by observation.
   - **Code:** `get_current_user` (`backend/auth/dependencies.py`) only calls `decode_token`.
   - **Tests:**
     - A pymongo `CommandListener` recorded **zero commands of any kind** during authenticated `/chat` and `/chat/stream` calls. The same listener does record `users` commands during login, which proves the spy works.
     - `/chat` also returns 200 on an app with no DB attached at all.
   - **Attempted violations:** none. The plan diagram's "validate token → Login storage" arrow describes this exact violation; it was rejected at the design stage.
2. **bcrypt only on signup and login.**
   - **Status:** holds.
   - **Code:** bcrypt is imported only in `auth/passwords.py`, which is used only by the service's signup and login functions. The token module never touches it.
   - **Attempted violations:** none.
3. **One Mongo instance, two collections.**
   - **Status:** holds.
   - **How:** one `MONGO_URI`, one `mongo` service, `users` only. Refresh sessions live inside the user document *specifically* to avoid a third collection. `travel_tips` is never referenced.
   - **Attempted violations:** none.
   - **Note:** `README.md` (team split) lists the `users` schema under Person A, while the prompt assigns it to this team. We built it; the teammates should reconcile this.
4. **Pydantic body models, not the plan's query-param signature.**
   - **Status:** holds.
   - **How:** every input endpoint takes a `BaseModel` with `extra="forbid"`.
   - **Regression guard:** `/chat?message=hi&thread_id=x` returns 422, tested at the integration and e2e levels.
   - **Attempted violations:** none.
5. **CORS from the start.**
   - **Status:** holds.
   - **How:** CORS was configured in Task 1.
   - **Gap found and fixed:** BUG-2 found that 500s lacked CORS headers; fixed with a middleware inside CORS. A parametrized test now covers 11 error classes.
6. **Latency target "<500 ms to first streamed token".**
   - **Status:** holds.
   - **Measured (MOCK graph, inside the compose network):**
     - Time to first token was 129–135 ms with a 2 s mock delay spread over the chunks.
     - With 10 concurrent streams, time to first token was 67–73 ms.
     - These numbers measure the backend plus the mock, **not** the real agent.
   - **Corrections and rulings:**
     - `api-architect` removed a "2–6 s full response" sentence from `CONTRACT.md`.
     - The bcrypt "~250 ms" comment is a CPU note, not an API claim. Lead ruling: allowed.
   - **Other documentation flaw:** the diagram's "<500ms" NFR is an inaccurate full-response claim.
7. **Windows-compatible commands.**
   - **Status:** holds.
   - **How:**
     - The README is PowerShell-first with bash dual-listing.
     - `smoke.ps1` and `ops-checks.ps1` are compatible with PowerShell 5.1 (no `&&`, no ternary) and were run on the host.
     - The PowerShell secret generator uses `RandomNumberGenerator`, not `Get-Random`.
   - **Exception:** `smoke.sh` is the bash twin.
8. **No secrets in committed files.**
   - **Status:** holds.
   - **How:**
     - `.env` is gitignored, and `.dockerignore` excludes `.env*`.
     - `.env.example` leaves `JWT_SECRET` and `GEMINI_API_KEY` empty.
     - Compose fails fast if `JWT_SECRET` is unset.
     - Startup rejects a secret shorter than 32 chars.
   - **Exception:** the `test` service has an inline, obviously fake `test-only-not-a-secret-…` value.
   - **Attempted violations:** none.

---

## 5. Interface Contracts Defined

The full, authoritative version is in **`backend/CONTRACT.md`**. Hand that file to teammates. Summary:

### 5.1 `graph` contract (Person B must satisfy)
- **Location:** a module `agent/graph.py` at the repo root exposing `graph`: a compiled LangGraph with sync `invoke` and `stream`.
- **Switching on:** set `USE_MOCK_GRAPH=false`. The Dockerfile copies `agent/` automatically when it exists.
- **Call:** `graph.invoke(input, config)`.
  - `input = {"messages": [{"role": "user", "content": "<message>"}]}`, one new user turn per call. History comes from B's checkpointer.
  - `config = {"configurable": {"thread_id": "<user_id>:<client_thread_id>"}}`.
- **`thread_id`:** opaque to B. It is already namespaced per user, so B must not parse it. The same value means the same conversation.
- **Output:** a dict whose `["messages"][-1]` has `.content` (or `["content"]`). It must be a `str` or a list of text parts; the backend joins them.
- **Streaming:**
  - The call is `graph.stream(input, config, stream_mode="messages")`, yielding `(message_chunk, metadata)`.
  - The backend forwards only chunks where `metadata["langgraph_node"] == STREAM_NODE` (default `"generate"`) and the text is non-empty. That keeps grader and planner output out of the user's stream.
- **Threading:**
  - Both methods are called **on a worker thread** (`run_in_threadpool` / `iterate_in_threadpool`), so they may block.
  - On client disconnect, the generator is `close()`d.
- **Exceptions:**
  - Any exception becomes 502 `upstream_error` on `/chat`, and an `error` SSE event on `/chat/stream`.
  - Never put user content or secrets into exception messages; they are logged.

### 5.2 HTTP contract (frontend)
- **Errors:** every non-2xx response except `/health` uses `{"error": {"code": str, "message": str, "details": list | null}}`. Every 401 carries `WWW-Authenticate: Bearer`.
- **`POST /auth/signup`:**
  - Request: `{"email": EmailStr, "password": str}`. The password must be 8–72 UTF-8 bytes; the email is lowercased.
  - Responses: **201** `{"id", "email", "created_at"}` (no tokens); 409 `email_taken`; 422; 429.
- **`POST /auth/login`:**
  - Request: `{"email", "password"}`.
  - Responses: **200** `{"access_token", "refresh_token", "token_type": "bearer", "expires_in": 900}`; 401 `invalid_credentials` (identical for an unknown email and a wrong password); 422; 429.
- **`POST /auth/refresh`:**
  - Request: `{"refresh_token"}`.
  - Responses: **200** with a new token pair (rotation: the old refresh token is dead); 401 `invalid_token`.
  - Only replaying an *already-rotated* token, including losing a parallel refresh, revokes **all** of that user's sessions, so clients must refresh single-flight.
  - Evicted, logged-out or unknown tokens get a plain 401.
- **`POST /auth/logout`:**
  - Request: `{"refresh_token"}`.
  - Responses: **204** (idempotent; an expired but genuine token also gets 204); 401 for a forged token or an access token.
- **`POST /chat`** (Bearer token):
  - Request: `{"message": str (1..4000, stripped), "thread_id"?: ^[A-Za-z0-9_-]{1,64}$}`. Unknown fields give 422.
  - Responses: **200** `{"response": str, "thread_id": str}` (the server generates a 32-hex `thread_id` if it is omitted); 401; 422; 429; 502 `upstream_error`.
- **`POST /chat/stream`** (Bearer token, same body as `/chat`, shares the `/chat` rate-limit bucket):
  - **Content type:** `text/event-stream`.
  - **Events:**
    - `event: token` / `data: {"content": "..."}`, one per chunk.
    - Then exactly one of `event: done` / `data: {"thread_id": "..."}`, or `event: error` / `data: <error envelope>`.
  - **Failures:**
    - A failure mid-stream arrives as status 200 followed by an `error` event.
    - 401, 422 and 429 arrive *before* the stream, as normal JSON envelopes.
    - A stream that ends without `done` or `error` must be treated as a failure.
  - **Why POST:** a token must never go in a URL, so `EventSource` GET is not used. Clients use `fetch` + `ReadableStream`; `CONTRACT.md` has a JS example.
- **`GET /health`** (public): 200 `{"status": "ok", "mongo": "up"}` or 503 `{"status": "degraded", "mongo": "down"}`.
- **Rate limits** (per minute):
  - login 10 per IP, signup 5 per IP, refresh and logout 30 per IP, chat and stream 30 per user.
  - Over the limit: 429 `rate_limited` with a `Retry-After` header (exposed via CORS).
  - Schema-invalid bodies count toward the limit; unparseable JSON does not.

---

## 6. Testing Report

All numbers come from runs inside Docker. The host has no Python.

- **Final full run** (`docker compose --profile test run --rm --build test`): **`502 passed, 27 skipped in 94.68s`**. The 27 skipped tests are e2e, which skip without `E2E_BASE_URL`.
- **Unit tests: 363.** No services are needed; they also pass with Mongo stopped (`--no-deps`). They cover:
  - Passwords: round-trip, rejection, `$2b$12$` cost, 72-byte handling, NUL, surrogates, non-ASCII.
  - JWT:
    - round-trips for access and refresh tokens
    - rejection of expired, tampered, `alg:none`/`None`, HS512, RS256-header, wrong-secret and wrong-type tokens
    - missing claims, oversized tokens
  - Every Pydantic schema: valid input accepted, and each invalid field rejected.
  - `get_current_user`, both with the token layer mocked and with real tokens.
  - Config parsing (`JWT_SECRET`, rate-limit strings, `LOG_LEVEL`).
  - The rate limiter: window, `Retry-After`, key isolation, reset.
  - The graph adapter: content extraction, thread namespacing, the 502 wrapping, the mock's per-chunk delay.
  - The error-envelope shape, and the `UnhandledErrorMiddleware` cases (before and after the response starts, non-HTTP scopes).
- **Integration tests: 139.** These use the real app, a real `wanderchat_test` DB (dropped before every test) and the mocked graph. They cover:
  - The full flow: signup → login → `/chat`.
  - The auth failure matrix, including byte-identical bodies for wrong password vs unknown user.
  - Refresh: rotation, replay revoking all sessions, parallel refresh giving `[200, 401]`, eviction of a 6th session.
  - Logout semantics.
  - **Constraint 1 by observation:** zero DB commands during `/chat` and `/chat/stream`.
  - The 422 guard against query params; 401s; 502 on graph failure; thread isolation between users.
  - `/health`: 200, and 503 with a dead client injected.
  - Error-envelope plus CORS consistency across 11 error classes.
  - 429 on login and chat.
  - BUG-1's documented behaviour.
  - The SSE suite (26 tests):
    - framing, headers and the node filter
    - the `done` event
    - mid-stream, creation-time and malformed-item errors
    - JSON-escaping of newlines and injected `event: done`
    - pre-stream envelopes
    - the shared rate bucket and history persistence
    - generator close
  - The in-process concurrency test.
- **End-to-end / clean machine:**
  - `docker compose --profile test down -v` (all volumes removed), then `docker compose up --build -d --wait`: **healthy in 21.8 s** with exit 0. Image layers were cached; a no-cache backend build took 28 s, and the Atlas Local image is about 2 GB on first pull.
  - **e2e functional suite against `http://backend:8000`, from inside the network: 20/20 passed.** The chat bucket returned 429 after exactly 30 requests.
  - **Verdict:** a working stack with no manual steps beyond the documented `.env` step.
  - **`scripts/smoke.ps1` run on the Windows host:**
    - Every step PASSed: `.env`, compose up, health, signup, login, `/chat`, and `/chat/stream` (42 tokens then `done`).
    - It printed `SMOKE: ALL PASS` with exit 0.
    - The failure path exits 1.
    - `smoke.sh` behaves the same.
  - **Healthcheck truth** (`ops-checks.ps1 -Check health`):

    | Time | Observation |
    |---|---|
    | 0.5 s | mongo stopped |
    | 6.7 s | `/health` returns 503 |
    | 39.8 s | container `unhealthy` |
    | 39.8 s | mongo restarted |
    | 41.5 s | `/health` returns 200 |
    | 46.3 s | container `healthy` |

    There were 0 backend restarts.
  - **Startup ordering** (`-Check startup`): with mongo down, the backend made 5 ping attempts over 30 s, exited, and was restarted by the policy. It was healthy **2.4 s** after mongo started, and never wedged.
  - **SSE incremental delivery** (real uvicorn, mock delay 2 s, 3 runs):
    - 34 tokens plus `done`.
    - Time to first token **129 / 130 / 135 ms**; total 2.056–2.065 s.
    - All 33 inter-token gaps were about 58 ms, so delivery is not buffered.
- **Concurrency (Task D):** a mock graph that blocks with a sync `time.sleep(0.5)`, live uvicorn container, measured from inside the network, 2 runs:

  | N concurrent `/chat` | wall time | serial would be |
  |---|---|---|
  | 1 | 0.523 / 0.514 s | 0.5 s |
  | 5 | 0.535 / 0.530 s | 2.5 s |
  | 10 | 0.539 / 0.546 s | 5.0 s |
  | 20 | 0.637 / 0.630 s | 10.0 s |
  | 50 | 1.154 / 1.141 s | 25.0 s |

  - **Wall time does not scale linearly, so the event loop is not blocked.** The threadpool decision is confirmed.
  - N=50 takes two waves because of AnyIO's 40-thread default pool.
  - The in-process figures were nearly identical: 0.507 / 0.521 / 0.535 / 0.555 / 1.179 s.
  - `/chat/stream` with N=10: time to first token min/median/max **67 / 71 / 73 ms**.
- **Coverage (before the post-review fix round; the final figure is 98%, see §6.1):** **99%** (871 statements, 5 missed; branch coverage on, 126 branches, 5 partial). Every module is at 100% except `auth/repository.py` (96%), `auth/service.py` (97%), `auth/tokens.py` (96%) and `ratelimit.py` (97%).
- **Bugs found during testing:**
  - **BUG-1: unparseable JSON bypasses rate limits.**
    - **Cause:** FastAPI reads and parses the body *before* resolving route dependencies, so a `JSONDecodeError` returns 422 before the limiter runs.
    - **Resolution (lead ruling):** a contract change only, since those requests do no bcrypt or DB work. Tested.
  - **BUG-2: 500 responses had no CORS headers.**
    - **Cause:** Starlette routes `Exception` handlers to `ServerErrorMiddleware`, which sits outside `CORSMiddleware`.
    - **Fix:** a pure-ASGI `UnhandledErrorMiddleware` inside CORS. Tested.
  - **python-jose `require_exp` forces `verify_exp`:** logout with an expired token returned 401. Fixed in `tokens.py` (found by `auth-engineer`).
  - **Infra:** the port 27017 clash with a native Windows MongoDB service (fixed with a host-port override) and the `PYTHONPATH` issue in the test image (fixed with `ENV PYTHONPATH=/app`).
  - **Test-side defects (in tests and scripts, not the backend):**
    - a substring assertion in the SSE newline test
    - PowerShell 5.1 stderr handling and a function name shadowing `docker` in `ops-checks.ps1`
- **Code paths knowingly left untested:**
  - `repository.py:49`: a non-str guard that the schemas make unreachable.
  - `service.py:65`: a user deleted between lookup and session write (a race with no hook).
  - `tokens.py:114`: an unknown `expected_type` (a programmer error).
  - `tokens.py:147`: a non-dict JWT payload (python-jose cannot produce one).
  - `ratelimit.py:59`: deletion of stale keys during the sweep.
  - SSE client-disconnect behaviour. It was verified once by `api-architect`'s probe on real uvicorn, but there is no suite test (httpx's ASGITransport cannot simulate a disconnect).
  - Login timing parity: measured once (medians 394.5 vs 394.1 ms) but not asserted in the suite.
  - Multi-worker and multi-replica behaviour.
  - `USE_MOCK_GRAPH=false` with a real `agent.graph` (it does not exist yet).
  - The `ollama` and `frontend` profiles were never run.

### 6.1 Final independent review and the fix round
- **Final reviewer verdict:** no blockers. All 8 hard constraints HOLD, each with file:line evidence. The reviewer's own suite run gave `502 passed, 27 skipped`.
- **Fixed after the review** (all owners used reproduce-first systematic debugging):
  - **#1 Threadpool starvation (should-fix), `api-architect`.**
    - **Cause:** plain-`def` dependencies such as `get_mongo_client` waited for a token from AnyIO's 40-thread pool, behind blocking graph calls.
    - **Fix:** request-path dependencies are now `async`.
    - **Measured `/health`** with 20 / 40 / 60 chats in flight: before **3 / 1755 / 1766 ms**, after **2 / 2 / 2 ms**.
    - **Login still waits about 2 s at 40+ in flight,** because bcrypt genuinely needs a pool thread. This is accepted as a known gap (§7).
  - **#2 Revoke-all on an evicted or logged-out token (should-fix), `auth-engineer`.**
    - **Fix:** a capped `rotated_jtis` list (20 entries, inside the user document, so still no new collection) is appended in the same atomic rotation update. Revoke-all now fires **only** on a genuine replay of an already-rotated token.
    - **Evicted, logged-out or unknown tokens** get a plain 401.
    - **A revoke-all clears the list,** so a stolen token cannot log the user out repeatedly.
  - **#3 Tests needed `.env` on a fresh clone (should-fix), `infra-engineer`.**
    - **Fix:** removed the `${JWT_SECRET:?}` interpolation and set `env_file: required: false`.
    - **Verified on a scratch copy without `.env`:** the test profile resolves, and the backend still fails loudly at startup with `ConfigError: JWT_SECRET is required…`.
    - **Cost:** the error now appears in `docker compose logs backend` rather than at `compose config` time.
  - **#4 Unlabelled latency numbers in the README (constraint 6 wording), `infra-engineer`.** Every number is now labelled as an in-network mock-graph measurement with its `MOCK_GRAPH_DELAY_SECONDS`.
  - **#6 Expiry boundary, `auth-engineer`:** `decode_token` now rejects `exp <= now` (RFC 7519 §4.1.4), with one `now` shared by decode and the DB gate.
  - **#7 Claim regex `$` (graded a nit, actually a fail-open), `auth-engineer`:**
    - **Symptom:** a signed token with `sub="<hex>\n"` caused a **500** on `/auth/refresh` and a 200 on `/chat`.
    - **Exploitable?** Only with the secret.
    - **Fix:** `re.fullmatch`.
  - **#8 TTLs of 0 or less accepted, `api-architect`:** now rejected with `ConfigError`.
  - **#11 Nits:** dead code removed (the unused 401 mapping and stale re-exports), the README's duplicated `--cov` removed, and a NAT note added.
- **Recorded as known gaps instead of fixed** (§7):
  - **#5:** per-IP limits are shared behind NAT or a proxy.
  - **#9:** a real LangGraph generator advanced across worker threads could hit ContextVar issues; the mock cannot reveal this.
  - **#10:** email-validator messages can quote fragments of the submitted email.
- **Final full run after the fix round** (lead, `docker compose --profile test run --rm --build test`):
  - **Result:** **`502 passed, 27 skipped in 93.61s`**. **Coverage 98%** (892 statements, 8 missed; branch coverage on).
  - **Why coverage fell from 99%:** the new fix branches have no dedicated tests yet. Examples: `tokens.py` (the `exp <= now` rejection and the `jti` fullmatch failure), and `config.py` (the TTL checks).
  - **The `test-engineer` regression round for the fixes was skipped at the human owner's request (time).** The regression tests the owners requested are listed in §7.

---

## 7. Known Gaps and Deferred Work

- **Regression tests for the post-review fixes are not written.** The `test-engineer` round was skipped at the owner's request, and the fixes were verified only by the owners' scratch repro scripts (`auth-engineer` 21/21; `api-architect` probe) plus the unchanged 502-test suite. Still to add:
  - An evicted token's refresh does not revoke other devices.
  - A logged-out token's refresh does not revoke other devices.
  - A second replay after re-login gets a plain 401.
  - `rotated_jtis` DB shape and cap.
  - The `exp == now` boundary.
  - A trailing `\n` in `sub` or `jti` gets 401, not 500.
  - `/health` stays under 100 ms with 40+ chats in flight.
  - A static check that every request-path dependency is a coroutine.
  - `rate_limit()` with a sync key function.
  - TTL ≤ 0 raises `ConfigError`.
- **The `pytest.ini` marker text still says e2e is "reserved; none yet",** which is stale (reviewer nit).
- **The graph is fully mocked.**
  - Nothing here proves behaviour against the real LangGraph agent, Gemini or Ollama: real latency, real stream chunk shapes, or real exception types.
  - Every latency number above measures the backend plus `MockGraph`.
  - The "<500 ms to first token" target must be re-measured once Person B's graph is plugged in.
- **Not horizontally scalable yet.** The in-process rate limiter forces 1 worker and 1 replica. "Load balancing" from the diagram is **not implemented**; it needs a Redis-backed limiter first.
- **The threadpool ceiling.** AnyIO's default limit is 40 threads per process, shared by graph calls and bcrypt. It was not tuned; `anyio.to_thread.current_default_thread_limiter().total_tokens` is the knob.
- **Access tokens cannot be revoked before they expire** (up to 15 minutes). This is inherent to constraint 1.
- **Conversation memory** belongs to Person B's checkpointer. `MockGraph`'s history is in-memory and lost on restart.
- **No `/preferences` endpoint.** The plan's optional §4.3 was not built.
- **No SSE keep-alive comments.** Long gaps between tokens behind an idle-timeout proxy could drop the stream.
- **Password policy is length-only.** There is no breached-password check and no Unicode normalization.
- **Signup's 409 enumerates registered emails.** This is an accepted tradeoff, rate-limited.
- **Atlas Local runs without authentication.** It is mitigated only by binding to `127.0.0.1`. Production would need auth and TLS.
- **Images are pinned by tag, not digest.**
- **Dependencies:** python-jose is thinly maintained; PyJWT would be the replacement if needed.
- **A fresh clone needs one manual step to run the stack** (`.env` + `JWT_SECRET`). The tests themselves no longer need `.env`.
- **Per-IP auth limits are shared behind NAT or a proxy** (reviewer #5), because X-Forwarded-For is deliberately not trusted. Behind a future reverse proxy, a trusted-proxy configuration would be needed.
- **A real LangGraph stream is untested across threads** (reviewer #9). `iterate_in_threadpool` advances the generator on different worker threads; add one test with a real compiled graph when Person B's code lands.
- **Validation messages may quote fragments of an invalid email** (reviewer #10). Passwords are never echoed.
- **Windows dev note:** time-to-first-byte measured from the Windows host through Docker Desktop's port forwarding is about 1 s later than inside the network (measured in-network: about 20–130 ms depending on the mock delay). Measure latency from inside the compose network.
- **Problems in the plan docs and diagram (flagged for the team):**
  - `techinfo.md` §4.2: the `async def chat(message: str, thread_id: str)` signature makes query params, so a JSON body gets 422 (constraint 4).
  - `techinfo.md` §4.1: puts the graph at `backend/agent.py`; we use a separate `agent/` package.
  - `techinfo.md` §6.1–6.2:
    - The Dockerfile's `COPY . .` with `main:app` inside `build: ./backend` does not match a package layout.
    - The Dockerfile has no non-root user and no deps-layer caching.
    - Compose uses bare `depends_on`, which does not wait for readiness.
    - The `ollama` and `frontend` services are unconditional (the frontend doesn't exist yet).
  - `techinfo.md`:
    - It never mentions CORS.
    - Its `thread_id` is not scoped per user, so threads can be read across users.
    - It calls sync `graph.invoke()` directly inside `async def`, which blocks the event loop.
  - `techinfo.md` §3.1: `qwen2.5:8b` is not a published Qwen2.5 size (7b / 14b are). Person B should verify the tag.
  - `techinfo.md` §5: the Streamlit snippet uses `uuid` without importing it.
  - The diagram:
    - The "Low latency <500ms" NFR states a full-response target (constraint 6).
    - "validate token → Login storage" depicts a per-request DB validation (constraint 1).
    - "Structure the response grammatically" sits at the gateway, but the API layer is a thin boundary and response shaping is the agent's job.
    - "Load balancing" is at the gateway, but it is not implemented (see above).
  - `README.md` puts the `users` schema under Person A, contradicting this team's scope.
  - The prompt refers to `plan.md`, which does not exist; the plan is `techinfo.md`.
- **Nothing is committed.** The human owner should review and commit.
