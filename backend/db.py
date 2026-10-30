"""MongoDB client construction and liveness checks (PyMongo native async API)."""

from __future__ import annotations

import asyncio
import logging
import time

from pymongo import AsyncMongoClient

from backend.config import Settings

log = logging.getLogger(__name__)

HEALTH_PING_TIMEOUT_SECONDS = 2.0
_MAX_BACKOFF_SECONDS = 5.0


def create_client(settings: Settings) -> AsyncMongoClient:
    # Short server selection so a single ping fails fast; the startup loop retries.
    return AsyncMongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True)


async def ping(client: AsyncMongoClient, timeout: float = HEALTH_PING_TIMEOUT_SECONDS) -> bool:
    """True if the server answers ``ping`` within ``timeout`` seconds."""
    try:
        await asyncio.wait_for(client.admin.command("ping"), timeout=timeout)
        return True
    except Exception:  # noqa: BLE001 - any failure means "down" for a health probe
        return False


async def wait_for_mongo(client: AsyncMongoClient, timeout_seconds: float) -> None:
    """Ping with exponential backoff until success or ``timeout_seconds`` elapse, then raise."""
    deadline = time.monotonic() + timeout_seconds
    delay = 0.5
    attempt = 0
    while True:
        attempt += 1
        remaining = deadline - time.monotonic()
        try:
            await asyncio.wait_for(client.admin.command("ping"), timeout=max(0.1, min(remaining, _MAX_BACKOFF_SECONDS)))
            log.info("MongoDB reachable (attempt %d)", attempt)
            return
        except Exception as exc:  # noqa: BLE001
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    f"MongoDB unreachable after {timeout_seconds:.0f}s ({attempt} attempts): {type(exc).__name__}"
                ) from exc
            log.warning("MongoDB not reachable yet (attempt %d, %s); retrying", attempt, type(exc).__name__)
            await asyncio.sleep(min(delay, remaining))
            delay = min(delay * 2, _MAX_BACKOFF_SECONDS)
