# WanderChat backend: running it locally

FastAPI in Docker. No login and no local database: anyone can chat. Travel tips are read
from MongoDB Atlas (`MONGODB_URI`, see `data/search.py`).
**You only need Docker** (Docker Desktop with Compose v2+). You do not need Python on the host.
Commands are PowerShell first. Where bash differs, it is listed underneath.

API contract (endpoints, errors, graph boundary): [`CONTRACT.md`](CONTRACT.md).

---

## 1. First-time setup

From the repo root:

```powershell
Copy-Item .env.example .env
```
```bash
cp .env.example .env
```

Fill in `GROQ_API_KEY` and `MONGODB_URI` for the real agent. While `USE_MOCK_GRAPH=true`,
both can stay empty. `.env` is gitignored, so never commit it. The tests (step 3) do not
need `.env`.

The settings you are most likely to touch:

| Variable | Default | Meaning |
|---|---|---|
| `LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL`, case-insensitive. An invalid value fails startup. |
| `USE_MOCK_GRAPH` | `true` | `true` = in-process mock agent; `false` = `agent.graph:graph`. |
| `MOCK_GRAPH_DELAY_SECONDS` | `0` | Mock only: **total** delay per call. `/chat` sleeps it once; `/chat/stream` spreads it evenly across chunks. |
| `CORS_ORIGINS` | `http://localhost:8501,http://localhost:3000` | Comma-separated browser origins. |
| `RATE_LIMIT_CHAT` | `30/60` | Chat requests per client IP, `N/seconds` sliding window. |
| `BACKEND_HOST_PORT` | `8000` | Host-side port (compose only). |

The full list with comments is in `.env.example`.

## 2. Run it

```powershell
docker compose up --build        # foreground, Ctrl+C to stop
docker compose up --build -d     # or detached
docker compose ps                # wait until backend shows (healthy)
```

This starts `backend` (http://localhost:8000), plus Ollama for query embeddings when
`COMPOSE_PROFILES=ollama` is set in `.env`.

Smoke test:

```powershell
Invoke-RestMethod http://localhost:8000/health
Invoke-RestMethod -Method Post http://localhost:8000/chat -ContentType "application/json" `
  -Body '{"message": "Plan 3 days in Lisbon", "thread_id": "trip-1"}'
```
```bash
curl -s http://localhost:8000/health
curl -s -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" -d '{"message": "Plan 3 days in Lisbon", "thread_id": "trip-1"}'
```

Streaming (`POST /chat/stream`, Server-Sent Events, same body as `/chat`). Use `curl.exe`,
not PowerShell's `curl` alias. The body goes in a file, because Windows PowerShell 5.1 strips
inner double quotes from JSON passed to native programs. `-N` disables curl's buffering.

```powershell
'{"message": "Plan 3 days in Lisbon", "thread_id": "trip-1"}' | Set-Content -Encoding ascii body.json
curl.exe -N http://localhost:8000/chat/stream -H "Content-Type: application/json" --data-binary "@body.json"
```
```bash
curl -N http://localhost:8000/chat/stream \
  -H "Content-Type: application/json" -d '{"message": "Plan 3 days in Lisbon", "thread_id": "trip-1"}'
```

You should see `event: token` lines and then one `event: done`. Event format: see
[`CONTRACT.md`](CONTRACT.md#post-chatstream-server-sent-events).

> **Measuring latency: do it from inside the compose network.** From the Windows host,
> Docker Desktop's port forwarding adds about 1 s before the first bytes of a stream
> arrive. The target is **< 500 ms to the first streamed token**. To measure in-network
> (stdlib only, runs in the backend container):
>
> ```powershell
> @'
> import http.client, json, time
> c = http.client.HTTPConnection("127.0.0.1", 8000)
> t0 = time.perf_counter()
> c.request("POST", "/chat/stream", json.dumps({"message": "Plan 3 days in Lisbon"}),
>           {"Content-Type": "application/json"})
> r = c.getresponse()
> while not r.readline().startswith(b"event: token"): pass
> print(f"status {r.status}, first token after {(time.perf_counter() - t0) * 1000:.0f} ms")
> '@ | docker compose exec -T backend python -
> ```

Interactive docs are at http://localhost:8000/docs.

Logs: `docker compose logs -f backend`. Stop: `docker compose down`.

Conversation history is kept in memory per `thread_id` and is lost when the backend restarts.

## 3. Tests

```powershell
docker compose --profile test run --rm test
```

This builds the `test` image target (the runtime image plus dev dependencies plus `backend/tests/`)
and runs `pytest` with the mock graph. No external services are needed. Coverage is reported
on every run (`pytest.ini` addopts). Pass pytest arguments after the service name:

```powershell
docker compose --profile test run --rm test pytest -q -k stream
docker compose --profile test run --rm test pytest --no-cov     # skip coverage
```

The production image (`target: runtime`) contains no pytest, dev dependencies or tests.

## 4. Frontend

```powershell
docker compose --profile frontend up --build
```

Open http://localhost:8501.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ports are not available: ... 127.0.0.1:8000 ... bind` | Something already uses 8000. Set `BACKEND_HOST_PORT` in `.env`. |
| PyPI timeouts during `--build` | Pip already uses a 120 s timeout. Retry. Only the deps layer downloads, and it is cached afterwards. |
| Changes to code don't show up | Run `docker compose up --build`. The source is copied into the image, not bind-mounted. |
| `.env` edited in PowerShell looks right but values are ignored | Save it as UTF-8 without BOM, or ASCII (`Set-Content -Encoding ascii`). Each line must be `KEY=value`. |

## How the image is built (for reviewers)

- `Dockerfile` stages: `deps` (venv from `requirements.txt`, installed with `--no-deps` and then
  `pip check`, so an incomplete lock fails the build) → `runtime` (non-root uid 10001, read-only
  root-owned source, `HEALTHCHECK` on `/health`, `uvicorn --workers 1`) and `test`.
- The dependency layer depends only on the requirements files. A code change rebuilds only the
  source layer, which takes seconds.
- Exactly one uvicorn worker and one backend replica: the rate limiter and chat history keep
  their state in process memory.
- Chat rate limits are keyed per client IP, and `X-Forwarded-For` is deliberately **not**
  trusted, because it is spoofable without a trusted proxy. So clients behind one NAT or
  proxy IP share a single bucket.
- To upgrade dependencies, edit the direct pin in `requirements.txt`, re-freeze inside
  `python:3.12.16-slim-trixie` (instructions at the top of the file), then rebuild and run the tests.
