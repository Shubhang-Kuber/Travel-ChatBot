"""E2E fixtures: a real HTTP client against the running stack, from inside the compose network.

Run (stack must be up):
    docker compose --profile test run --rm -e E2E_BASE_URL=http://backend:8000 test pytest -m e2e --no-cov -s

The live backend enforces its real (.env) rate limits per client IP, so helpers wait out
a 429 using its Retry-After instead of failing: re-running the suite within a minute is OK.
Users are created in the stack's own database (random emails).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from backend.tests.e2e.live import BASE_URL, new_user


def pytest_collection_modifyitems(config, items):
    skip = pytest.mark.skip(reason="E2E_BASE_URL not set (e2e runs only against a live stack)")
    for item in items:
        if "e2e" in item.keywords and not BASE_URL:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def http() -> httpx.Client:
    with httpx.Client(base_url=BASE_URL, timeout=30) as c:
        yield c


@pytest.fixture(scope="session")
def alice(http) -> dict[str, Any]:
    return new_user(http)


@pytest.fixture(scope="session")
def bob(http) -> dict[str, Any]:
    return new_user(http)
