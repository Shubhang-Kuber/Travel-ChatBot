# WanderChat backend: running it locally

FastAPI + MongoDB (Atlas Local, which includes Vector Search), all in Docker.
**You only need Docker** (Docker Desktop with Compose v2+). You do not need Python on the host.
Commands are PowerShell first. Where bash differs, it is listed underneath.

API contract (endpoints, auth, errors, graph boundary): [`CONTRACT.md`](CONTRACT.md).

---

## 1. First-time setup (the one manual step)

From the repo root:

```powershell
Copy-Item .env.example .env
```
```bash
cp .env.example .env
```

Then set `JWT_SECRET` in `.env` (required, at least 32 characters). Generate a value with:

```powershell
$b = New-Object byte[] 48; [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); [Convert]::ToBase64String($b).TrimEnd('=').Replace('+','-').Replace('/','_')
```
```bash
# Any shell with Docker (no local Python needed):
docker run --rm python:3.12.16-slim-trixie python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Paste the output after `JWT_SECRET=`. `.env` is gitignored, so never commit it. No default
secret ships with the repo on purpose. If `JWT_SECRET` is missing or too short, the backend
container exits at startup with `ConfigError: JWT_SECRET is required and must be at least 32
characters` and never turns healthy. Check with `docker compose logs backend`. The tests
(step 3) do not need `.env`.

Every other setting in `.env.example` has a working default. While `USE_MOCK_GRAPH=true`,
`GEMINI_API_KEY` can stay empty. The ones you are most likely to touch:

| Variable | Default | Meaning |
|---|---|---|
| `JWT_SECRET` | none (required) | HS256 signing key, at least 32 characters. |
| `LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL`, case-insensitive. An invalid value fails startup. |
| `USE_MOCK_GRAPH` | `true` | `true` = in-process mock agent; `false` = Person B's `agent.graph:graph`. |
| `MOCK_GRAPH_DELAY_SECONDS` | `0` | Mock only: **total** delay per call. `/chat` sleeps it once; `/chat/stream` spreads it evenly across chunks. |
| `CORS_ORIGINS` | `http://localhost:8501,http://localhost:3000` | Comma-separated browser origins. |
| `RATE_LIMIT_*` | `10/60` login, `5/60` signup, `30/60` refresh, `30/60` chat | `N/seconds` sliding window. |
| `MONGO_HOST_PORT` / `BACKEND_HOST_PORT` | `27017` / `8000` | Host-side ports (compose only). See Troubleshooting. |

The full list with comments is in `.env.example`.

## 2. Run it

```powershell
docker compose up --build        # foreground, Ctrl+C to stop
docker compose up --build -d     # or detached
docker compose ps                # wait until backend shows (healthy)
```

This starts `mongo` (Atlas Local) and `backend` (http://localhost:8000). The backend waits
for mongo to be healthy before it starts. The first run pulls about 2.2 GB of images (Atlas Local alone is about 2 GB). After that,
the stack is healthy about 20-30 s after `up`.

Smoke test:

```powershell
Invoke-RestMethod http://localhost:8000/health
$body = '{"email": "alice@example.com", "password": "correct horse battery"}'
Invoke-RestMethod -Method Post http://localhost:8000/auth/signup -ContentType "application/json" -Body $body
$t = Invoke-RestMethod -Method Post http://localhost:8000/auth/login -ContentType "application/json" -Body $body
Invoke-RestMethod -Method Post http://localhost:8000/chat -ContentType "application/json" `
  -Headers @{ Authorization = "Bearer $($t.access_token)" } `
  -Body '{"message": "Plan 3 days in Lisbon", "thread_id": "trip-1"}'
```
```bash
curl -s http://localhost:8000/health
BODY='{"email": "alice@example.com", "password": "correct horse battery"}'
curl -s -X POST http://localhost:8000/auth/signup -H "Content-Type: application/json" -d "$BODY"
TOKEN=$(curl -s -X POST http://localhost:8000/auth/login -H "Content-Type: application/json" -d "$BODY" \
  | sed -E 's/.*"access_token":"([^"]+)".*/\1/')
curl -s -X POST http://localhost:8000/chat -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"message": "Plan 3 days in Lisbon", "thread_id": "trip-1"}'
```

Streaming (`POST /chat/stream`, Server-Sent Events, same body as `/chat`). Use `curl.exe`,
not PowerShell's `curl` alias. The body goes in a file, because Windows PowerShell 5.1 strips
inner double quotes from JSON passed to native programs. `-N` disables curl's buffering.

```powershell
'{"message": "Plan 3 days in Lisbon", "thread_id": "trip-1"}' | Set-Content -Encoding ascii body.json
curl.exe -N http://localhost:8000/chat/stream -H "Authorization: Bearer $($t.access_token)" `
  -H "Content-Type: application/json" --data-binary "@body.json"
```
```bash
curl -N http://localhost:8000/chat/stream -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"message": "Plan 3 days in Lisbon", "thread_id": "trip-1"}'
```

You should see `event: token` lines and then one `event: done`. Event format: see
[`CONTRACT.md`](CONTRACT.md#post-chatstream-protected-server-sent-events).

> **Measuring latency: do it from inside the compose network.** From the Windows host,
> Docker Desktop's port forwarding adds about 1 s before the first bytes of a stream
> arrive, and it groups the first events together. The extra delay comes from the host
> port forwarding, not the API. The target is **< 500 ms to the first streamed token**.
> For reference, these in-network measurements were all taken with the **mock graph**,
> not the real agent:
> about 110-127 ms and 129-135 ms with `MOCK_GRAPH_DELAY_SECONDS=2`, and about 20 ms with
> `MOCK_GRAPH_DELAY_SECONDS=0`. They show the backend's streaming overhead only. They say
> nothing about the real LLM.
> To measure in-network (stdlib only, runs in the backend container):
>
> ```powershell
> $env:ACCESS_TOKEN = $t.access_token
> @'
> import http.client, json, os, time
> c = http.client.HTTPConnection("127.0.0.1", 8000)
> t0 = time.perf_counter()
> c.request("POST", "/chat/stream", json.dumps({"message": "Plan 3 days in Lisbon"}),
>           {"Authorization": "Bearer " + os.environ["ACCESS_TOKEN"], "Content-Type": "application/json"})
> r = c.getresponse()
> while not r.readline().startswith(b"event: token"): pass
> print(f"status {r.status}, first token after {(time.perf_counter() - t0) * 1000:.0f} ms")
> '@ | docker compose exec -T -e ACCESS_TOKEN backend python -
> ```
>
> (bash: `ACCESS_TOKEN=$TOKEN docker compose exec -T -e ACCESS_TOKEN backend python - <<'EOF'` ... `EOF`.)

Interactive docs are at http://localhost:8000/docs.

Logs: `docker compose logs -f backend`. Stop: `docker compose down` (your data is kept).

## 3. Tests

```powershell
docker compose --profile test run --rm test
```

This builds the `test` image target (the runtime image plus dev dependencies plus `backend/tests/`)
and runs `pytest` against the compose mongo, using database `wanderchat_test` and a test-only
JWT secret. The tests do not read `.env` and run on a fresh clone without one. Coverage
is reported on every run (`pytest.ini` addopts). Pass pytest arguments after the service name:

```powershell
docker compose --profile test run --rm test pytest -q -k auth
docker compose --profile test run --rm test pytest --no-cov     # skip coverage
```

The production image (`target: runtime`) contains no pytest, dev dependencies or tests.

## 4. Offline mode (local LLM via Ollama)

```powershell
docker compose --profile ollama up -d
docker compose exec ollama ollama pull llama3.2    # one-time model download, kept in a volume
```

Ollama listens on http://localhost:11434 (and on `http://ollama:11434` from other containers).
The image is several GB, so it only starts with `--profile ollama`. Using it is up to the
graph (Person B's `agent/`). Keep `USE_MOCK_GRAPH=true` until that package exists.

The frontend (once `./frontend` exists) runs with `docker compose --profile frontend up --build`.

## 5. Clean reset

```powershell
docker compose down -v    # stops everything and DELETES users, travel_tips and search indexes
docker compose up --build
```

`down` without `-v` keeps the data volumes (`mongo-data`, `mongo-config`, `mongot-data`, `ollama-data`).

## 6. Connecting to MongoDB from your machine

Use `mongodb://localhost:27017/?directConnection=true` (Compass, mongosh, Person A's loaders).
Without `directConnection=true`, the driver tries to reach the replica-set member by its
in-network name `mongo`, which does not resolve on your host. The port is bound to
`127.0.0.1` only, because this dev instance has **no authentication**.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Backend keeps restarting / never healthy, logs say `ConfigError: JWT_SECRET is required and must be at least 32 characters` | There is no `.env`, or `JWT_SECRET` is empty or too short. Run `Copy-Item .env.example .env` (bash: `cp .env.example .env`) and set a secret as in step 1. Then run `docker compose up -d --force-recreate backend`. |
| `ports are not available: ... 127.0.0.1:27017 ... bind` | Something already uses 27017, usually a natively installed **MongoDB Windows service** (check: `Get-Service MongoDB`). Either stop it (`Stop-Service MongoDB`, admin) or set `MONGO_HOST_PORT=27018` in `.env` and use that port from the host. Containers are unaffected. Use `BACKEND_HOST_PORT` the same way for 8000. |
| `docker compose ps` shows backend `restarting`, logs say `MongoDB unreachable after 30s` | Mongo was not reachable at startup. The backend retries for `MONGO_STARTUP_TIMEOUT_SECONDS`, exits, and is restarted (`restart: unless-stopped`). It recovers on its own once mongo is up. Check `docker compose logs mongo`. |
| Backend `(unhealthy)` while running, `/health` returns 503 `{"mongo":"down"}` | Mongo is stopped or unresponsive: `docker compose start mongo`. Health recovers within about 10 s. |
| Mongo stays `starting` for a long time on first run | Atlas Local initialises its replica set and search process on first start. Give it up to about 2 min on slow disks. |
| `MONGO_URI` set to `localhost` and the backend can't connect | Inside a container, `localhost` is the container itself. Leave `MONGO_URI` unset (compose uses `mongo:27017`). |
| PyPI timeouts during `--build` | Pip already uses a 120 s timeout. Retry. Only the deps layer downloads, and it is cached afterwards. |
| Changes to code don't show up | Run `docker compose up --build`. The source is copied into the image, not bind-mounted. |
| `.env` edited in PowerShell looks right but values are ignored | Save it as UTF-8 without BOM, or ASCII (`Set-Content -Encoding ascii`). Each line must be `KEY=value`. |

## How the image is built (for reviewers)

- `Dockerfile` stages: `deps` (venv from `requirements.txt`, installed with `--no-deps` and then
  `pip check`, so an incomplete lock fails the build) → `runtime` (non-root uid 10001, read-only
  root-owned source, `HEALTHCHECK` on `/health`, `uvicorn --workers 1`) and `test`.
- The dependency layer depends only on the requirements files. A code change rebuilds only the
  source layer, which takes seconds.
- `.dockerignore` is an allowlist (`backend/`, `agent/`, `pytest.ini`, `requirements*.txt`).
  Person B's `agent/` is picked up automatically once it exists.
- Exactly one uvicorn worker and one backend replica: the rate limiter keeps its state in
  process memory. Scaling out first requires moving it to a shared store.
- Auth rate limits are keyed per client IP, and `X-Forwarded-For` is deliberately **not**
  trusted, because it is spoofable without a trusted proxy. So clients behind one NAT or
  proxy IP share a single bucket. Behind a reverse proxy, every client looks like the
  proxy's IP. Chat limits are per user and are not affected.
- To upgrade dependencies, edit the direct pin in `requirements.txt`, re-freeze inside
  `python:3.12.16-slim-trixie` (instructions at the top of the file), then rebuild and run the tests.
