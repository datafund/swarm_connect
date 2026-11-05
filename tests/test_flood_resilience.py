# tests/test_flood_resilience.py
"""
Replay of a flood like the 2026-11-03 one, against the gateway app and a stub
Bee over real TCP (#588).

Checks the issue's acceptance locally: /health stays fast, the Bee calls made
for /health are bounded by time rather than by the number of requests, Bee
never has more calls in flight than the gate allows, and requests over the cap
get 503 BEE_BUSY with Retry-After while the background snapshot refresh keeps
working. The same replay against a running gateway is scripts/flood_replay.py.
"""
import asyncio
import math
import random
import time
from collections import Counter, defaultdict
from unittest.mock import patch

import httpx2
import pytest
import pytest_asyncio

from app.core.config import settings
from app.services import http_client as hc
from app.services.bee_gate import bee_call_gate
from tests.tools.bee_stub import BeeStub

REF = "ab" * 32
MIX = [  # (weight, method, path), mostly / and /health as on 2026-11-03
    (45, "GET", "/"),
    (25, "GET", "/health"),
    (10, "GET", "/api/v1/stamps/"),
    (10, "GET", f"/api/v1/data/{REF}"),
    (10, "POST", "/api/v1/data/"),
]
TOTAL = 3000
CONCURRENCY = 200
CAP, RESERVED, TRANSFERS = 16, 4, 6
TTL = 0.5
# Bee calls one /health snapshot makes: 5 for the node summary, 2 /wallet,
# chequebook address and balance.
HEALTH_PATHS = ("/topology", "/status", "/health", "/addresses", "/chainstate",
                "/wallet", "/chequebook/address", "/chequebook/balance")


def pct(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))]


@pytest_asyncio.fixture
async def gateway():
    stub = await BeeStub(latency=0.02).start()
    # Stamp list and downloads are slow, so they hold their slots.
    stub.latency_by_prefix = {"/batches": 0.2, "/stamps": 0.2, "/bzz/": 0.3}
    saved = {k: getattr(hc, k) for k in (
        "_client", "_transport", "_replace_lock", "_recovery_task", "_last_recovery_attempt")}
    patches = [
        patch.object(settings, "SWARM_BEE_API_URL", stub.base_url),
        patch.object(settings, "BASE_RPC_URL", stub.base_url + "rpc"),
        patch.object(settings, "X402_ENABLED", True),
        patch.object(settings, "X402_PAY_TO_ADDRESS", "0x" + "33" * 20),
        patch.object(settings, "BEE_CALL_GATE_ENABLED", True),
        patch.object(settings, "BEE_MAX_CONCURRENT_CALLS", CAP),
        patch.object(settings, "BEE_RESERVED_BACKGROUND_CALLS", RESERVED),
        patch.object(settings, "BEE_MAX_CONCURRENT_TRANSFERS", TRANSFERS),
        patch.object(settings, "BEE_CALL_SLOT_WAIT_SECONDS", 0.2),
        patch.object(settings, "HEALTH_SNAPSHOT_TTL_SECONDS", TTL),
        # get_node_status_summary's own 15 s cache would hide every snapshot
        # refresh after the first from the /topology count. Off, each refresh
        # reads Bee, so the count measures the snapshot alone.
        patch("app.services.swarm_api.NODE_STATUS_TTL_SECONDS", 0),
    ]
    for p in patches:
        p.start()
    from app.x402.base_balance import clear_balance_cache
    clear_balance_cache()
    from app.services.swarm_api import _node_status_cache
    _node_status_cache.update({"data": None, "ts": 0.0})
    bee_call_gate.reset()
    await hc.init_client()
    from app.main import app
    transport = httpx2.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx2.AsyncClient(transport=transport, base_url="http://gw", timeout=30) as client:
            yield client, stub
    finally:
        await hc.close_client()
        await stub.stop()
        for p in reversed(patches):
            p.stop()
        for k, v in saved.items():
            setattr(hc, k, v)
        clear_balance_cache()
        _node_status_cache.update({"data": None, "ts": 0.0})


async def replay(client):
    rng = random.Random(588)
    plan = [rng.choices(MIX, [w for w, _, _ in MIX])[0] for _ in range(TOTAL)]
    queue = asyncio.Queue()
    for item in plan:
        queue.put_nowait(item)
    latencies = defaultdict(list)
    statuses = defaultdict(Counter)
    busy_without_retry_after = 0
    busy_wrong_body = 0

    async def worker():
        nonlocal busy_without_retry_after, busy_wrong_body
        while True:
            try:
                _, method, path = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            started = time.monotonic()
            if method == "POST":
                r = await client.post(path, content=b"not a form", headers={"Content-Type": "text/plain"})
            else:
                r = await client.get(path)
            latencies[path].append(time.monotonic() - started)
            statuses[path][r.status_code] += 1
            if r.status_code == 503:
                if "retry-after" not in r.headers:
                    busy_without_retry_after += 1
                if r.json().get("code") != "BEE_BUSY":
                    busy_wrong_body += 1

    started = time.monotonic()
    await asyncio.gather(*[worker() for _ in range(CONCURRENCY)])
    return time.monotonic() - started, latencies, statuses, busy_without_retry_after, busy_wrong_body


@pytest.mark.asyncio
async def test_flood_leaves_health_fast_and_bee_bounded(gateway, record_property, capsys):
    from app.services.metrics import health_snapshot_refreshes_total as refreshes
    client, stub = gateway
    failed_before = refreshes.labels(result="error")._value.get() + refreshes.labels(result="timeout")._value.get()

    # Unloaded baseline on this machine, so the bound below scales with it.
    unloaded = []
    for _ in range(20):
        t0 = time.monotonic()
        assert (await client.get("/health")).status_code == 200
        unloaded.append(time.monotonic() - t0)
    baseline = pct(unloaded, 50)
    # Without the snapshot the flood's /health p99 was about 27 s here; with it
    # about 0.2 s. The bound leaves a wide margin for a slow CI machine on both
    # sides: at least 2.5 s, or 50 times the unloaded median if that is larger.
    p99_bound = max(2.5, 50 * baseline)
    ok_before = refreshes.labels(result="ok")._value.get()
    from app.services.swarm_api import _node_status_cache
    _node_status_cache.update({"data": None, "ts": 0.0})
    stub.reset()

    duration, latencies, statuses, no_retry_after, wrong_body = await replay(client)

    health = latencies["/health"] + latencies["/"]
    health_p99 = pct(health, 99)
    stats = stub.stats()
    by_path = stats["by_path"]
    max_refreshes = math.ceil(duration / TTL) + 2
    summary = {
        "requests": TOTAL, "duration_s": round(duration, 2),
        "unloaded_health_p50_s": round(baseline, 4), "p99_bound_s": round(p99_bound, 2),
        "health_requests": len(health), "health_p50_s": round(pct(health, 50), 4),
        "health_p99_s": round(health_p99, 4), "health_max_s": round(max(health), 4),
        "bee_calls_total": stats["total"], "bee_peak_in_flight": stats["peak_in_flight"],
        "bee_topology_calls": by_path.get("/topology", 0), "bee_wallet_calls": by_path.get("/wallet", 0),
        "max_refreshes_allowed": max_refreshes,
        "statuses": {p: dict(c) for p, c in statuses.items()},
    }
    for k, v in summary.items():
        record_property(k, v)
    with capsys.disabled():
        print(f"\nflood replay (#588): {summary}")

    # /health stays fast and always answers.
    assert health_p99 < p99_bound
    assert set(statuses["/health"]) == {200} and set(statuses["/"]) == {200}
    # Bee calls for /health are bounded by time, not by request count: at most
    # one snapshot per TTL (plus the first and one in progress at the end).
    # Every refresh reads Bee (node cache off), so this counts refreshes: at
    # least one ran, and no more than one per TTL.
    assert 1 <= by_path.get("/topology", 0) <= max_refreshes
    assert by_path.get("/wallet", 0) <= 2 * max_refreshes
    assert sum(by_path.get(p, 0) for p in HEALTH_PATHS) <= 9 * max_refreshes
    # Bee never has more calls in flight than the gate allows.
    assert stats["peak_in_flight"] <= CAP
    # Requests over the cap get 503 BEE_BUSY with Retry-After, not a 5xx from the pool.
    bee_routes = ("/api/v1/stamps/", f"/api/v1/data/{REF}")
    assert sum(statuses[p][503] for p in bee_routes) > 0
    assert no_retry_after == 0 and wrong_body == 0
    for p in bee_routes:
        assert set(statuses[p]) <= {200, 404, 503}, statuses[p]
    # The background refresh kept working under the flood.
    assert refreshes.labels(result="ok")._value.get() - ok_before >= 1
    assert refreshes.labels(result="error")._value.get() + refreshes.labels(result="timeout")._value.get() \
        == failed_before
    assert sum(bee_call_gate.held.values()) == 0
