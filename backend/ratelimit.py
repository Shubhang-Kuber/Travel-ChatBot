"""In-process sliding-window rate limiter, exposed as FastAPI dependencies.

Per-process state: lost on restart and NOT shared across workers/replicas, so the
backend runs with exactly one uvicorn worker. Scaling out requires moving this to Redis.

Concurrency: the dependency below is ``async def``, so every ``hit`` runs on the single
event-loop thread and cannot interleave (there is no ``await`` inside ``hit``). The
``threading.Lock`` is belt-and-braces for any future caller from a worker thread
(e.g. a sync dependency run in the threadpool); uncontended, it costs ~50 ns.
"""

from __future__ import annotations

import inspect
import math
import threading
import time
from collections import deque
from typing import Any, Callable

from fastapi import Depends, Request

from backend.errors import AppError

_SWEEP_EVERY = 1024  # hits between sweeps of idle keys (bounds memory under many IPs)


class RateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: dict[tuple[str, str], deque[float]] = {}
        self._windows: dict[str, float] = {}
        self._calls = 0

    def hit(self, bucket: str, key: str, max_requests: int, window_seconds: float) -> float | None:
        """Record a request. Returns None if allowed, else seconds until a slot frees up."""
        now = self._clock()
        with self._lock:
            self._windows[bucket] = window_seconds
            self._calls += 1
            if self._calls % _SWEEP_EVERY == 0:
                self._sweep(now)
            q = self._hits.setdefault((bucket, key), deque())
            while q and q[0] <= now - window_seconds:
                q.popleft()
            if len(q) >= max_requests:
                return q[0] + window_seconds - now
            q.append(now)
            return None

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
            self._calls = 0

    def _sweep(self, now: float) -> None:
        stale = [k for k, q in self._hits.items() if not q or q[-1] <= now - self._windows.get(k[0], 0.0)]
        for k in stale:
            del self._hits[k]


def client_ip(request: Request) -> str:
    """Key by the direct peer address. X-Forwarded-For is NOT trusted (spoofable)."""
    return request.client.host if request.client else "unknown"


def _on_event_loop(func: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a trivial sync dependency as ``async def`` with the same (resolved) signature.

    FastAPI runs a plain ``def`` dependency through the threadpool, so it would queue for
    one of AnyIO's 40 worker tokens behind long graph/bcrypt calls. Key functions only read
    the request, so running them inline on the loop is both safe and faster.
    """

    async def call(*args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    call.__signature__ = inspect.signature(func, eval_str=True)  # type: ignore[attr-defined]
    call.__name__ = getattr(func, "__name__", "key_func")
    return call


def rate_limit(bucket: str, key_func: Callable[..., Any]) -> Callable[..., Any]:
    """Build a dependency enforcing the ``bucket`` limit from settings.

    ``key_func`` is itself resolved as a FastAPI dependency, so it can take a
    ``Request`` (see ``client_ip``) or depend on the authenticated user. A sync
    ``key_func`` is run on the event loop (see ``_on_event_loop``), so it must not block.
    """
    key_dep = key_func if inspect.iscoroutinefunction(key_func) else _on_event_loop(key_func)

    async def _enforce(request: Request, key: str = Depends(key_dep)) -> None:
        limit = request.app.state.settings.rate_limit(bucket)
        limiter: RateLimiter = request.app.state.rate_limiter
        retry_after = limiter.hit(bucket, key, limit.max_requests, limit.window_seconds)
        if retry_after is not None:
            secs = max(1, math.ceil(retry_after))
            raise AppError(
                429,
                "rate_limited",
                f"Too many requests. Retry in {secs} seconds.",
                headers={"Retry-After": str(secs)},
            )

    _enforce.__name__ = f"rate_limit_{bucket}"
    return _enforce
