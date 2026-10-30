"""Task D: concurrent /chat with a blocking (sync-sleep) mock graph must not serialize.

The mock's ``time.sleep`` imitates the real synchronous graph. If /chat ran it on the
event loop, N requests would take N * delay. Run with ``-s`` to see the timing table.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from backend.tests.helpers import Api

pytestmark = pytest.mark.integration

DELAY = 0.5
RESULTS: dict[int, float] = {}


async def _burst(api: Api, token: str, n: int) -> float:
    t0 = time.perf_counter()
    rs = await asyncio.gather(*(api.chat(token, f"msg {i}", f"conc-{n}-{i}") for i in range(n)))
    wall = time.perf_counter() - t0
    assert [r.status_code for r in rs] == [200] * n, [r.text for r in rs if r.status_code != 200]
    return wall


@pytest.mark.parametrize("n", [1, 5, 10, 20])
async def test_concurrent_chat_is_not_serial(app_with, n: int, record_property) -> None:
    async with app_with(mock_graph_delay_seconds=DELAY) as api:
        _, t = await api.new_user()
        wall = await _burst(api, t["access_token"], n)
    RESULTS[n] = wall
    record_property("wall_seconds", round(wall, 3))
    serial = DELAY * n
    print(f"\n[TaskD] N={n:>2}  wall={wall:.3f}s  serial would be {serial:.1f}s  speedup x{serial / wall:.1f}")
    assert wall >= DELAY * 0.95  # every request really waited on the blocking graph
    if n >= 5:
        assert wall < DELAY * n * 0.6, f"N={n}: {wall:.2f}s looks serialized"


async def test_event_loop_stays_responsive_during_chat_burst(app_with, record_property) -> None:
    async with app_with(mock_graph_delay_seconds=DELAY) as api:
        _, t = await api.new_user()
        burst = asyncio.create_task(_burst(api, t["access_token"], 20))
        await asyncio.sleep(0.1)  # let the 20 graph calls occupy worker threads
        t0 = time.perf_counter()
        r = await api.client.get("/health")
        health_latency = time.perf_counter() - t0
        await burst
    record_property("health_latency_seconds", round(health_latency, 3))
    print(f"\n[TaskD] /health latency during 20 in-flight chats: {health_latency * 1000:.1f} ms")
    assert r.status_code == 200
    assert health_latency < DELAY / 2


async def test_beyond_threadpool_capacity_requests_queue(app_with, record_property) -> None:
    # AnyIO's default thread limiter has 40 tokens: 50 blocking calls run as 40 + 10,
    # so wall time is ~2 x DELAY rather than ~1 x DELAY. Documents the ceiling.
    n = 50
    async with app_with(mock_graph_delay_seconds=DELAY) as api:
        _, t = await api.new_user()
        wall = await _burst(api, t["access_token"], n)
    record_property("wall_seconds", round(wall, 3))
    print(f"\n[TaskD] N={n}  wall={wall:.3f}s  (threadpool limit 40 => 2 waves expected)")
    assert DELAY * 1.9 <= wall < DELAY * 3.5
