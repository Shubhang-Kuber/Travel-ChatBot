"""Helpers for e2e tests against a live stack."""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

import httpx

BASE_URL = os.environ.get("E2E_BASE_URL", "").rstrip("/")
# Delay the live backend was started with (MOCK_GRAPH_DELAY_SECONDS), for timing tests.
EXPECT_DELAY = float(os.environ.get("E2E_EXPECT_DELAY", "0") or 0)
PASSWORD = "correct horse battery"


def unique_email(prefix: str = "e2e") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}@example.com"


def post_patiently(c: httpx.Client, path: str, max_wait: float = 65, **kw: Any) -> httpx.Response:
    """POST, waiting out 429s (real limits are per IP and shared by every e2e run)."""
    deadline = time.monotonic() + max_wait
    while True:
        r = c.post(path, **kw)
        if r.status_code != 429 or time.monotonic() > deadline:
            return r
        time.sleep(min(float(r.headers.get("retry-after", "1")), 10))


def new_user(c: httpx.Client, email: str | None = None) -> dict[str, Any]:
    email = email or unique_email()
    r = post_patiently(c, "/auth/signup", json={"email": email, "password": PASSWORD})
    assert r.status_code == 201, r.text
    r = post_patiently(c, "/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"email": email, **r.json()}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
