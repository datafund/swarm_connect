# tests/test_health_snapshot.py
"""
/ and /health are served from a snapshot of the upstream reads (#588).

Before, each request read the Bee node summary, the Bee wallet and chequebook
and the Base RPC itself: about five upstream calls per anonymous request.
"""
import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio

from app.core.config import settings
from app.services import health_snapshot

NODE = {"healthy": True, "network_availability": "Available", "connected_peers": 120, "warnings": []}
GNOSIS = {
    "can_accept": True, "wallet_address": "0xBee", "chequebook_address": "0xCheque",
    "xbzz_ok": True, "xdai_ok": True, "chequebook_ok": True,
    "balances": {"xbzz": {}, "xdai": {}, "chequebook": {}}, "warnings": [], "errors": [],
}
BASE = {"ok": True, "is_critical": False, "balance_eth": 0.01, "threshold_eth": 0.005,
        "critical_eth": 0.001, "address": "0xPayTo", "warning": None}


class Upstreams:
    """Counting fakes for the three upstream reads, with an optional delay."""

    def __init__(self, delay=0.0):
        self.delay = delay
        self.calls = {"node": 0, "gnosis": 0, "base": 0}
        self.node = dict(NODE)
        self.fail_node = False

    async def _node(self, *a, **k):
        self.calls["node"] += 1
        await asyncio.sleep(self.delay)
        if self.fail_node:
            raise RuntimeError("boom")
        return dict(self.node)

    async def _gnosis(self):
        self.calls["gnosis"] += 1
        await asyncio.sleep(self.delay)
        return dict(GNOSIS)

    async def _base(self):
        self.calls["base"] += 1
        await asyncio.sleep(self.delay)
        return dict(BASE)

    def patches(self):
        return [
            patch("app.services.swarm_api.get_node_status_summary", new=self._node),
            patch("app.x402.preflight.check_preflight_balances", new=self._gnosis),
            patch("app.x402.base_balance.check_base_eth_balance", new=self._base),
        ]


@pytest.fixture
def upstreams(monkeypatch):
    up = Upstreams()
    ps = up.patches()
    for p in ps:
        p.start()
    monkeypatch.setattr(settings, "X402_ENABLED", True)
    monkeypatch.setattr(settings, "HEALTH_SNAPSHOT_TTL_SECONDS", 15.0)
    yield up
    for p in reversed(ps):
        p.stop()


@pytest_asyncio.fixture
async def gw():
    from app.main import app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as c:
        yield c


class TestSingleFlight:

    @pytest.mark.asyncio
    async def test_concurrent_requests_cause_one_refresh(self, upstreams, gw):
        upstreams.delay = 0.1
        responses = await asyncio.gather(*[gw.get("/health") for _ in range(50)])
        assert all(r.status_code == 200 for r in responses)
        assert upstreams.calls == {"node": 1, "gnosis": 1, "base": 1}

    @pytest.mark.asyncio
    async def test_no_upstream_call_per_request_within_the_ttl(self, upstreams, gw):
        for path in ("/health", "/", "/health", "/"):
            assert (await gw.get(path)).status_code == 200
        for _ in range(100):
            await gw.get("/")
            await gw.get("/health")
        assert upstreams.calls == {"node": 1, "gnosis": 1, "base": 1}

    @pytest.mark.asyncio
    async def test_root_and_health_return_the_same_body(self, upstreams, gw):
        a = (await gw.get("/")).json()
        b = (await gw.get("/health")).json()
        for body in (a, b):
            body.pop("uptime_seconds")
            body["snapshot"].pop("age_seconds")
        assert a == b


class TestStaleWhileRevalidate:

    @pytest.mark.asyncio
    async def test_stale_snapshot_is_served_while_one_refresh_runs(self, upstreams, gw, monkeypatch):
        await gw.get("/health")
        upstreams.node["connected_peers"] = 7
        monkeypatch.setattr(settings, "HEALTH_SNAPSHOT_TTL_SECONDS", 0.05)
        await asyncio.sleep(0.06)
        upstreams.delay = 0.3
        loop = asyncio.get_running_loop()
        started = loop.time()
        stale = await asyncio.gather(*[gw.get("/health") for _ in range(20)])
        assert loop.time() - started < 0.25  # nobody waited for the refresh
        assert all(r.json()["bee_node"]["connected_peers"] == 120 for r in stale)
        await asyncio.sleep(0.4)
        assert upstreams.calls["node"] == 2  # one refresh for all twenty
        fresh = (await gw.get("/health")).json()
        assert fresh["bee_node"]["connected_peers"] == 7

    @pytest.mark.asyncio
    async def test_age_is_reported(self, upstreams, gw):
        body = (await gw.get("/health")).json()
        assert body["snapshot"]["max_age_seconds"] == 15.0
        assert 0 <= body["snapshot"]["age_seconds"] < 1
        assert body["snapshot"]["warnings"] == []
        await asyncio.sleep(0.3)
        later = (await gw.get("/health")).json()
        assert later["snapshot"]["age_seconds"] >= 0.2

    @pytest.mark.asyncio
    async def test_failed_refresh_keeps_last_snapshot_and_says_so(self, upstreams, gw, monkeypatch):
        await gw.get("/health")
        monkeypatch.setattr(settings, "HEALTH_SNAPSHOT_TTL_SECONDS", 0.05)
        monkeypatch.setattr(settings, "HEALTH_SNAPSHOT_REFRESH_TIMEOUT_SECONDS", 0.1)
        await asyncio.sleep(0.06)
        upstreams.delay = 1.0  # the refresh times out
        await gw.get("/health")
        await asyncio.sleep(0.2)
        body = (await gw.get("/health")).json()
        assert body["bee_node"]["healthy"] is True  # the last good snapshot
        assert body["snapshot"]["age_seconds"] >= 0.2
        assert any("timed out" in w for w in body["snapshot"]["warnings"])
        assert body["status"] == "ok"  # advisory only

    @pytest.mark.asyncio
    async def test_failed_refresh_is_not_retried_back_to_back(self, upstreams, gw, monkeypatch):
        await gw.get("/health")
        monkeypatch.setattr(settings, "HEALTH_SNAPSHOT_TTL_SECONDS", 0.2)
        monkeypatch.setattr(settings, "HEALTH_SNAPSHOT_REFRESH_TIMEOUT_SECONDS", 0.05)
        await asyncio.sleep(0.21)
        upstreams.delay = 1.0
        await gw.get("/health")
        await asyncio.sleep(0.08)  # the refresh has timed out
        calls = upstreams.calls["node"]
        for _ in range(20):
            await gw.get("/health")
        assert upstreams.calls["node"] == calls


class TestFirstSnapshot:

    @pytest.mark.asyncio
    async def test_first_request_waits_at_most_the_first_wait(self, upstreams, gw, monkeypatch):
        monkeypatch.setattr(settings, "HEALTH_SNAPSHOT_FIRST_WAIT_SECONDS", 0.1)
        upstreams.delay = 1.0
        loop = asyncio.get_running_loop()
        started = loop.time()
        body = (await gw.get("/health")).json()
        assert loop.time() - started < 0.5
        assert body["bee_node"]["healthy"] is False
        assert body["snapshot"]["age_seconds"] is None
        assert body["snapshot"]["warnings"]
        assert body["x402"]["bee_gnosis_wallet"]["can_accept"] is None

    @pytest.mark.asyncio
    async def test_node_summary_failure_reports_bee_node_unhealthy(self, upstreams, gw):
        # Before #589 bee_node was left out here; #586's probe reads
        # bee_node.healthy, so it is now reported, and status is degraded.
        upstreams.fail_node = True
        body = (await gw.get("/health")).json()
        assert body["bee_node"]["healthy"] is False
        assert body["bee_node"]["api_reachable"] is False
        assert body["status"] == "degraded"
        assert body["x402"]["bee_gnosis_wallet"]["can_accept"] is True


class TestShape:

    @pytest.mark.asyncio
    async def test_fields_clients_read_are_present(self, upstreams, gw):
        body = (await gw.get("/health")).json()
        assert body["status"] == "ok"
        assert body["bee_node"]["healthy"] is True  # #586's Alloy probe
        for key in ("message", "version", "uptime_seconds", "terms_url", "state"):
            assert key in body
        x = body["x402"]
        assert x["free_tier"]["header"] == "X-Payment-Mode: free"
        assert x["base_wallet"]["address"] == "0xPayTo"
        assert x["bee_gnosis_wallet"]["wallet_address"] == "0xBee"

    @pytest.mark.asyncio
    async def test_status_rules_are_unchanged(self, upstreams, gw):
        upstreams.node["network_availability"] = "Unavailable"
        body = (await gw.get("/health")).json()
        assert body["status"] == "degraded"

    @pytest.mark.asyncio
    async def test_snapshot_built_without_x402_is_not_used_with_it(self, upstreams, gw, monkeypatch):
        monkeypatch.setattr(settings, "X402_ENABLED", False)
        assert "x402" not in (await gw.get("/health")).json()
        monkeypatch.setattr(settings, "X402_ENABLED", True)
        body = (await gw.get("/health")).json()
        assert body["x402"]["bee_gnosis_wallet"]["can_accept"] is True


class TestMetrics:

    @pytest.mark.asyncio
    async def test_age_and_refresh_counts(self, upstreams, gw):
        from app.services.metrics import health_snapshot_refreshes_total
        before = health_snapshot_refreshes_total.labels(result="ok")._value.get()
        await gw.get("/health")
        assert health_snapshot_refreshes_total.labels(result="ok")._value.get() == before + 1
        from app.services.metrics import _snapshot_age
        assert 0 <= _snapshot_age() < 1

    def test_age_is_minus_one_before_the_first_snapshot(self):
        from app.services.metrics import _snapshot_age
        health_snapshot.reset()
        assert _snapshot_age() == -1.0


class TestRefreshIsBackgroundWork:

    @pytest.mark.asyncio
    async def test_refresh_runs_outside_the_request_context(self, upstreams, gw):
        """So its Bee calls take background slots, which a flood cannot starve."""
        from app.services import bee_gate
        seen = []
        original = upstreams._node

        async def node(*a, **k):
            seen.append(bee_gate.current_request_state())
            return await original()

        with patch("app.services.swarm_api.get_node_status_summary", new=node):
            await gw.get("/health")
        assert seen == [None]
