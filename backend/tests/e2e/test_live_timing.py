"""Timing against real uvicorn (Task C b, Task D live). Needs the backend started with a mock
delay, announced via E2E_EXPECT_DELAY (= MOCK_GRAPH_DELAY_SECONDS), and generous chat limits.

    E2E_EXPECT_DELAY=2   -> SSE incremental-delivery test
    E2E_EXPECT_DELAY=0.5 -> concurrency table (/chat N=1..50, /chat/stream N=10)
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time

import httpx
import pytest

from backend.tests.e2e.live import BASE_URL, EXPECT_DELAY, bearer, new_user

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def token(http) -> str:
    return new_user(http)["access_token"]


async def _timed_stream(c: httpx.AsyncClient, token: str, thread_id: str) -> dict:
    t0 = time.perf_counter()
    arrivals: list[tuple[float, str, object]] = []
    event = None
    async with c.stream("POST", "/chat/stream", json={"message": "3 days in Kyoto", "thread_id": thread_id}, headers=bearer(token)) as r:
        assert r.status_code == 200
        async for line in r.aiter_lines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                arrivals.append((time.perf_counter() - t0, event, json.loads(line[6:])))
    total = time.perf_counter() - t0
    tokens = [a for a in arrivals if a[1] == "token"]
    return {"total": total, "ttft": tokens[0][0] if tokens else None, "tokens": tokens, "last": arrivals[-1]}


@pytest.mark.skipif(EXPECT_DELAY < 1, reason="needs backend with MOCK_GRAPH_DELAY_SECONDS>=1 (E2E_EXPECT_DELAY)")
async def test_sse_is_delivered_incrementally(token, record_property) -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=60) as c:
        s = await _timed_stream(c, token, "inc-1")
    tokens, total, ttft = s["tokens"], s["total"], s["ttft"]
    times = [t for t, _, _ in tokens]
    gaps = [b - a for a, b in zip(times, times[1:])]
    distinct = sum(1 for g in gaps if g > 0.010)
    print(
        f"\n[SSE] delay={EXPECT_DELAY}s events={len(tokens) + 1} tokens={len(tokens)} TTFT={ttft * 1000:.0f}ms "
        f"total={total * 1000:.0f}ms gaps min/median/max={min(gaps) * 1000:.0f}/{statistics.median(gaps) * 1000:.0f}/"
        f"{max(gaps) * 1000:.0f}ms gaps>10ms={distinct}/{len(gaps)}"
    )
    record_property("ttft_ms", round(ttft * 1000))
    record_property("total_ms", round(total * 1000))
    assert s["last"][1] == "done"
    assert len(tokens) >= 10
    assert ttft < 0.5 * total
    assert total >= EXPECT_DELAY * 0.9
    assert distinct >= 0.8 * len(gaps), "tokens arrived in a blob, not incrementally"
    assert max(gaps) < 0.4 * total


def _is_half_second() -> bool:
    return abs(EXPECT_DELAY - 0.5) < 1e-9


@pytest.mark.skipif(not _is_half_second(), reason="needs backend with MOCK_GRAPH_DELAY_SECONDS=0.5 (E2E_EXPECT_DELAY)")
@pytest.mark.parametrize("n", [1, 5, 10, 20, 50])
async def test_live_concurrent_chat(token, n, record_property) -> None:
    limits = httpx.Limits(max_connections=200, max_keepalive_connections=200)
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=60, limits=limits) as c:
        t0 = time.perf_counter()
        rs = await asyncio.gather(
            *(c.post("/chat", json={"message": f"m{i}", "thread_id": f"live-{n}-{i}"}, headers=bearer(token)) for i in range(n))
        )
        wall = time.perf_counter() - t0
    assert [r.status_code for r in rs] == [200] * n, {r.status_code for r in rs}
    record_property("wall_seconds", round(wall, 3))
    print(f"\n[TaskD-live] /chat N={n:>2} wall={wall:.3f}s serial={0.5 * n:.1f}s speedup x{0.5 * n / wall:.1f}")
    assert wall >= 0.5 * 0.95
    if 5 <= n <= 40:
        assert wall < 0.5 * n * 0.6
    if n == 50:
        assert 0.5 * 1.9 <= wall < 0.5 * 3.5  # 40-thread pool => two waves


@pytest.mark.skipif(not _is_half_second(), reason="needs backend with MOCK_GRAPH_DELAY_SECONDS=0.5 (E2E_EXPECT_DELAY)")
async def test_live_concurrent_streams_ttft(token, record_property) -> None:
    n = 10
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=60, limits=httpx.Limits(max_connections=50)) as c:
        t0 = time.perf_counter()
        results = await asyncio.gather(*(_timed_stream(c, token, f"live-s-{i}") for i in range(n)))
        wall = time.perf_counter() - t0
    ttfts = sorted(r["ttft"] * 1000 for r in results)
    totals = sorted(r["total"] for r in results)
    print(
        f"\n[TaskD-live] /chat/stream N={n} wall={wall:.3f}s TTFT ms min/median/p90/max="
        f"{ttfts[0]:.0f}/{statistics.median(ttfts):.0f}/{ttfts[int(0.9 * (n - 1))]:.0f}/{ttfts[-1]:.0f} "
        f"stream totals min/max={totals[0]:.3f}/{totals[-1]:.3f}s"
    )
    record_property("ttft_ms", ttfts)
    assert all(r["last"][1] == "done" for r in results)
    assert ttfts[-1] < 500  # latency target: first token < 500 ms
    assert wall < 0.5 * n * 0.6
