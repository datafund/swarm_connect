#!/usr/bin/env python3
"""
Replay a request flood like the 2026-11-03 one against a gateway (#588).

Sends a mix of cheap anonymous requests at high concurrency, most of them to
/ and /health, the rest to routes that call Bee (stamp list, download) or are
refused before Bee (an invalid upload, 422). Reports latency percentiles per
route and the status mix, as JSON. A separate client probes /health once
every --probe-interval seconds during the flood, as a monitor would; its
latency is reported apart from the flood's own.

With --bee-stats pointing at a stub Bee's /__stats (tests/tools/bee_stub.py),
it also reports how many calls reached Bee and the most it had in flight at
once. The stub's counters are reset first.

Local example:

    python -m tests.tools.bee_stub --port 18633 --latency 0.05 &
    SWARM_BEE_API_URL=http://127.0.0.1:18633/ uvicorn app.main:app --port 8000 &
    python scripts/flood_replay.py --url http://localhost:8000 \
        --bee-stats http://127.0.0.1:18633/__stats

Run it only against a gateway you operate.
"""
import argparse
import asyncio
import json
import random
import time
from collections import Counter, defaultdict

import httpx

REF = "ab" * 32
# (weight, method, path). Weights follow the 2026-11-03 traffic: mostly / and
# /health, then API routes.
MIX = [
    (45, "GET", "/"),
    (25, "GET", "/health"),
    (10, "GET", "/api/v1/stamps/"),
    (10, "GET", f"/api/v1/data/{REF}"),
    (10, "POST", "/api/v1/data/"),
]


def percentile(values, p):
    if not values:
        return None
    values = sorted(values)
    k = min(len(values) - 1, max(0, int(round(p / 100 * (len(values) - 1)))))
    return round(values[k], 4)


async def replay(url, total, concurrency, timeout, probe_interval=0.5, seed=588):
    rng = random.Random(seed)
    weights = [w for w, _, _ in MIX]
    plan = [rng.choices(MIX, weights)[0] for _ in range(total)]
    latencies = defaultdict(list)
    statuses = defaultdict(Counter)
    retry_after_missing = 0
    queue = asyncio.Queue()
    for item in plan:
        queue.put_nowait(item)

    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(base_url=url, timeout=timeout, limits=limits) as client:
        async def worker():
            nonlocal retry_after_missing
            while True:
                try:
                    _, method, path = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                started = time.monotonic()
                try:
                    if method == "POST":
                        r = await client.post(path, content=b"not a form", headers={"Content-Type": "text/plain"})
                    else:
                        r = await client.get(path)
                    code = str(r.status_code)
                    if r.status_code == 503 and "retry-after" not in r.headers:
                        retry_after_missing += 1
                except httpx.HTTPError as e:
                    code = type(e).__name__
                latencies[f"{method} {path}"].append(time.monotonic() - started)
                statuses[f"{method} {path}"][code] += 1

        probe_latencies = []
        probe_failures = Counter()
        done = asyncio.Event()

        async def probe():
            # What a monitor sees: one /health at a time, on its own connection.
            async with httpx.AsyncClient(base_url=url, timeout=timeout) as monitor:
                while not done.is_set():
                    t0 = time.monotonic()
                    try:
                        r = await monitor.get("/health")
                        if r.status_code == 200:
                            probe_latencies.append(time.monotonic() - t0)
                        else:
                            probe_failures[str(r.status_code)] += 1
                    except httpx.HTTPError as e:
                        probe_failures[type(e).__name__] += 1
                    try:
                        await asyncio.wait_for(done.wait(), probe_interval)
                    except asyncio.TimeoutError:
                        pass

        started = time.monotonic()
        prober = asyncio.create_task(probe())
        await asyncio.gather(*[worker() for _ in range(concurrency)])
        duration = time.monotonic() - started
        done.set()
        await prober

    routes = {}
    for route, values in latencies.items():
        routes[route] = {
            "requests": len(values),
            "p50_s": percentile(values, 50),
            "p95_s": percentile(values, 95),
            "p99_s": percentile(values, 99),
            "max_s": round(max(values), 4),
            "statuses": dict(statuses[route]),
        }
    return {
        "requests": total,
        "concurrency": concurrency,
        "duration_s": round(duration, 2),
        "requests_per_s": round(total / duration, 1) if duration else None,
        "routes": routes,
        "503_without_retry_after": retry_after_missing,
        "health_probe": {
            "answers": len(probe_latencies),
            "p50_s": percentile(probe_latencies, 50),
            "p99_s": percentile(probe_latencies, 99),
            "max_s": round(max(probe_latencies), 4) if probe_latencies else None,
            "failures": dict(probe_failures),
        },
    }


async def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--requests", type=int, default=3000)
    parser.add_argument("--concurrency", type=int, default=200)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--probe-interval", type=float, default=0.5,
                        help="seconds between the separate /health probe's requests")
    parser.add_argument("--bee-stats", default=None, help="a stub Bee's /__stats URL")
    args = parser.parse_args()

    if args.bee_stats:
        async with httpx.AsyncClient() as c:
            await c.post(args.bee_stats.replace("/__stats", "/__reset"))
    report = await replay(args.url, args.requests, args.concurrency, args.timeout, args.probe_interval)
    if args.bee_stats:
        async with httpx.AsyncClient() as c:
            report["bee"] = (await c.get(args.bee_stats)).json()
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
