# WanderChat backend image. Build context = repo root.
#
#   docker build --target runtime -t wanderchat-backend .   # production-style image
#   docker build --target test    -t wanderchat-test .      # runtime + dev deps + tests
#
# Stages:
#   base      pinned Python + env shared by every stage
#   deps      venv with runtime deps only (layer depends ONLY on requirements.txt)
#   test-deps deps + dev deps (layer depends ONLY on requirements*.txt)
#   src       app source with tests stripped (keeps tests out of the runtime layers)
#   runtime   non-root, venv + source, uvicorn with exactly 1 worker   <- compose `backend`
#   test      non-root, dev venv + full source incl. tests, runs pytest <- compose `test`

# Pinned to an exact Python patch + Debian release so rebuilds are reproducible.
ARG PYTHON_IMAGE=python:3.12.16-slim-trixie

# ---------------------------------------------------------------------------- base
FROM ${PYTHON_IMAGE} AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_ROOT_USER_ACTION=ignore \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app
# PYTHONPATH=/app: `backend` (and `agent`) importable regardless of cwd or entry point.
# The `pytest` console script, unlike `python -m pytest`, does not add cwd to sys.path,
# so without this a test dir lacking __init__.py fails with "No module named 'backend'".
# Fixed UID/GID so file ownership is predictable across machines.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin app

# ---------------------------------------------------------------------------- deps
FROM base AS deps
RUN python -m venv /opt/venv
COPY requirements.txt /tmp/requirements.txt
# --no-deps: requirements.txt is a complete lock (all transitives pinned). If a
# transitive dependency is ever missing from it, `pip check` fails the build instead
# of pip silently pulling whatever version is newest that day.
RUN pip install --no-deps -r /tmp/requirements.txt && pip check

# ---------------------------------------------------------------------------- test-deps
FROM deps AS test-deps
COPY requirements-dev.txt /tmp/requirements-dev.txt
RUN pip install --no-deps -r /tmp/requirements-dev.txt && pip check

# ---------------------------------------------------------------------------- src
# .dockerignore is an allowlist, so the context holds only backend/, agent/ (when Person
# B's package exists), pytest.ini and the requirements files. Copying the whole context
# lets agent/ be picked up when present without breaking the build when it is absent.
FROM base AS src
COPY . /src
RUN rm -rf /src/pytest.ini /src/requirements.txt /src/requirements-dev.txt \
 && find /src -type d \( -name tests -o -name __pycache__ \) -prune -exec rm -rf {} + \
 && find /src -type f -name 'test_*.py' -delete \
 && find /src -type f -name 'conftest.py' -delete

# ---------------------------------------------------------------------------- runtime
FROM base AS runtime
COPY --from=deps /opt/venv /opt/venv
WORKDIR /app
# Source stays root-owned and read-only for the app user: a compromised process cannot
# rewrite the code it is running.
COPY --from=src /src /app
USER app
EXPOSE 8000
# Stdlib only (no curl in slim). urlopen raises on 503, so "mongo down" => unhealthy.
HEALTHCHECK --interval=10s --timeout=5s --start-period=45s --start-interval=2s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"]
# Exactly ONE worker: the rate limiter is in-process state (see backend/ratelimit.py).
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

# ---------------------------------------------------------------------------- test
FROM base AS test
COPY --from=test-deps /opt/venv /opt/venv
WORKDIR /app
# Full source incl. backend/tests/ and pytest.ini (whichever exist). Owned by the app
# user so pytest can write .pytest_cache / .coverage next to pytest.ini.
COPY --chown=app:app . /app
RUN chown app:app /app
USER app
CMD ["pytest"]
