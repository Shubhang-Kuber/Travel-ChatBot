"""/health and the app lifespan against real (and deliberately dead) MongoDB clients."""

from __future__ import annotations

import time

import pytest
from asgi_lifespan import LifespanManager
from pymongo import AsyncMongoClient

from backend.dependencies import get_mongo_client
from backend.main import create_app
from backend.tests.helpers import make_settings

pytestmark = pytest.mark.integration

# Port 1 on loopback: connection refused immediately. A long server-selection timeout
# proves /health's own ~2 s cap is what bounds the probe, not the driver's timeout.
DEAD_URI = "mongodb://127.0.0.1:1/?directConnection=true"


@pytest.fixture
async def dead_client():
    c = AsyncMongoClient(DEAD_URI, serverSelectionTimeoutMS=30_000, connectTimeoutMS=500)
    yield c
    await c.close()


async def test_health_up(api) -> None:
    r = await api.client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok", "mongo": "up"}


async def test_health_down_is_503_status_report_within_timeout(api, dead_client) -> None:
    api.app.dependency_overrides[get_mongo_client] = lambda: dead_client
    t0 = time.perf_counter()
    r = await api.client.get("/health")
    elapsed = time.perf_counter() - t0
    assert r.status_code == 503
    assert r.json() == {"status": "degraded", "mongo": "down"}  # NOT the error envelope
    assert elapsed < 4.0, f"/health took {elapsed:.2f}s with mongo down"


async def test_health_recovers(api, dead_client) -> None:
    api.app.dependency_overrides[get_mongo_client] = lambda: dead_client
    assert (await api.client.get("/health")).status_code == 503
    api.app.dependency_overrides.clear()
    assert (await api.client.get("/health")).status_code == 200


async def test_startup_fails_loudly_when_mongo_unreachable(dead_client) -> None:
    app = create_app(make_settings(mongo_startup_timeout_seconds=1.0), mongo_client=dead_client)
    t0 = time.perf_counter()
    with pytest.raises(Exception) as exc:
        async with LifespanManager(app, startup_timeout=30):
            pass
    assert "MongoDB unreachable" in str(exc.value)
    assert time.perf_counter() - t0 < 10


async def test_lifespan_creates_unique_email_index(api, db) -> None:
    info = await db["users"].index_information()
    assert any(spec["key"] == [("email", 1)] and spec.get("unique") for spec in info.values()), info


async def test_lifespan_touches_only_the_users_collection(api, db) -> None:
    assert set(await db.list_collection_names()) <= {"users"}


async def test_injected_client_is_not_closed_on_shutdown(mongo_client) -> None:
    app = create_app(make_settings(), mongo_client=mongo_client)
    async with LifespanManager(app, startup_timeout=30):
        pass
    assert (await mongo_client.admin.command("ping"))["ok"] == 1


async def test_owned_client_lifecycle_from_settings() -> None:
    # No injected client: the app builds its own from settings.mongo_uri and closes it.
    from backend.tests.helpers import client_for

    app = create_app(make_settings())
    async with LifespanManager(app, startup_timeout=30) as m:
        async with client_for(m.app) as c:
            assert (await c.get("/health")).status_code == 200
