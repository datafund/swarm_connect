# tests/test_health_node.py
"""
Tests for Bee node connectivity/health surfaced on the gateway /health endpoint.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import swarm_api


@pytest.fixture
def client():
    return TestClient(app)


def _resp(data):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value=data)
    return r


def _mock_client(topo, status, health=None, addresses=None, chainstate=None):
    """Mock Bee that dispatches per diagnostic endpoint.

    Endpoints not supplied answer {}, mirroring a Bee that doesn't serve them.
    """
    by_endpoint = {
        "topology": topo,
        "status": status,
        "health": health or {},
        "addresses": addresses or {},
        "chainstate": chainstate or {},
    }

    def fake_get(url, timeout=8):
        endpoint = url.rstrip("/").rsplit("/", 1)[-1]
        return _resp(by_endpoint.get(endpoint, {}))

    c = MagicMock()
    c.get = AsyncMock(side_effect=fake_get)
    return c


# --------------------------------------------------------------------------- #
# summary derivation
# --------------------------------------------------------------------------- #
class TestNodeStatusSummary:
    @pytest.mark.asyncio
    async def test_unavailable_is_unhealthy_with_warning(self):
        topo = {"networkAvailability": "Unavailable", "connected": 15, "population": 1317,
                "depth": 9, "reachability": "Private"}
        status = {"beeMode": "light", "connectedPeers": 15, "neighborhoodSize": 16,
                  "storageRadius": 0, "reserveSize": 0, "isWarmingUp": False}
        swarm_api._node_status_cache["data"] = None
        with patch("app.services.swarm_api.get_client", return_value=_mock_client(topo, status)):
            s = await swarm_api.get_node_status_summary(use_cache=False)
        assert s["network_availability"] == "Unavailable"
        assert s["connected_peers"] == 15
        assert s["mode"] == "light"
        assert s["healthy"] is False
        assert any("network availability" in w for w in s["warnings"])

    @pytest.mark.asyncio
    async def test_available_is_healthy(self):
        topo = {"networkAvailability": "Available", "connected": 137, "population": 3794,
                "depth": 9, "reachability": "Private"}
        status = {"beeMode": "light", "connectedPeers": 137, "isWarmingUp": False,
                  "storageRadius": 0, "reserveSize": 0}
        swarm_api._node_status_cache["data"] = None
        with patch("app.services.swarm_api.get_client", return_value=_mock_client(topo, status)):
            s = await swarm_api.get_node_status_summary(use_cache=False)
        assert s["healthy"] is True
        assert s["warnings"] == []

    @pytest.mark.asyncio
    async def test_unreachable_node_is_unhealthy(self):
        # both calls raise -> empty topo/status
        c = MagicMock()
        c.get = AsyncMock(side_effect=Exception("bee down"))
        swarm_api._node_status_cache["data"] = None
        with patch("app.services.swarm_api.get_client", return_value=c):
            s = await swarm_api.get_node_status_summary(use_cache=False)
        assert s["healthy"] is False
        assert any("could not query" in w for w in s["warnings"])


# --------------------------------------------------------------------------- #
# node identity, build and chain-sync detail
# --------------------------------------------------------------------------- #
class TestNodeDetail:
    TOPO = {"networkAvailability": "Available", "connected": 137, "population": 3794,
            "depth": 9, "reachability": "Private"}
    STATUS = {"beeMode": "light", "connectedPeers": 137, "isWarmingUp": False,
              "storageRadius": 0, "reserveSize": 0, "reserveSizeWithinRadius": 0,
              "pullsyncRate": 12.5, "committedDepth": 17, "batchCommitment": 4096,
              "lastSyncedBlock": 41_000_000}
    HEALTH = {"status": "ok", "version": "2.8.0-6ce78a76", "apiVersion": "7.4.0"}
    ADDRESSES = {"overlay": "47f1994c" + "0" * 56, "ethereum": "0x7f73"}
    CHAINSTATE = {"chainTip": 41_000_050, "block": 41_000_050}

    async def _summary(self, **overrides):
        kwargs = {"topo": self.TOPO, "status": self.STATUS, "health": self.HEALTH,
                  "addresses": self.ADDRESSES, "chainstate": self.CHAINSTATE}
        kwargs.update(overrides)
        swarm_api._node_status_cache["data"] = None
        client = _mock_client(kwargs["topo"], kwargs["status"], kwargs["health"],
                              kwargs["addresses"], kwargs["chainstate"])
        with patch("app.services.swarm_api.get_client", return_value=client):
            return await swarm_api.get_node_status_summary(use_cache=False)

    @pytest.mark.asyncio
    async def test_surfaces_version_and_identity(self):
        s = await self._summary()
        assert s["version"] == "2.8.0-6ce78a76"
        assert s["api_version"] == "7.4.0"
        assert s["bee_status"] == "ok"
        assert s["overlay"] == self.ADDRESSES["overlay"]

    @pytest.mark.asyncio
    async def test_surfaces_previously_discarded_status_fields(self):
        s = await self._summary()
        assert s["reserve_size_within_radius"] == 0
        assert s["pullsync_rate"] == 12.5
        assert s["committed_depth"] == 17
        assert s["batch_commitment"] == 4096

    @pytest.mark.asyncio
    async def test_chain_lag_computed_and_not_warned_when_small(self):
        s = await self._summary()
        assert s["last_synced_block"] == 41_000_000
        assert s["chain_tip"] == 41_000_050
        assert s["chain_sync_lag_blocks"] == 50
        assert not any("behind the chain tip" in w for w in s["warnings"])

    @pytest.mark.asyncio
    async def test_chain_lag_warns_when_beyond_threshold(self):
        tip = self.STATUS["lastSyncedBlock"] + swarm_api.CHAIN_LAG_WARN_BLOCKS + 1
        s = await self._summary(chainstate={"chainTip": tip})
        assert s["chain_sync_lag_blocks"] == swarm_api.CHAIN_LAG_WARN_BLOCKS + 1
        assert any("behind the chain tip" in w for w in s["warnings"])

    @pytest.mark.asyncio
    async def test_chain_lag_none_when_chainstate_unavailable(self):
        s = await self._summary(chainstate={})
        assert s["chain_tip"] is None
        assert s["chain_sync_lag_blocks"] is None
        assert not any("behind the chain tip" in w for w in s["warnings"])

    @pytest.mark.asyncio
    async def test_low_peer_count_warns_without_flipping_healthy(self):
        # Available network but a collapsing peer set: advisory only.
        low = dict(self.STATUS, connectedPeers=2)
        s = await self._summary(status=low)
        assert any("peer count is low" in w for w in s["warnings"])
        assert s["healthy"] is True

    @pytest.mark.asyncio
    async def test_no_low_peer_warning_at_threshold(self):
        at_threshold = dict(self.STATUS, connectedPeers=swarm_api.LOW_PEER_WARN_THRESHOLD)
        s = await self._summary(status=at_threshold)
        assert not any("peer count is low" in w for w in s["warnings"])

    @pytest.mark.asyncio
    async def test_non_ok_bee_status_warns(self):
        s = await self._summary(health={"status": "degraded", "version": "2.8.0"})
        assert any("Bee reports status 'degraded'" in w for w in s["warnings"])

    @pytest.mark.asyncio
    async def test_missing_optional_endpoints_leave_fields_none(self):
        # Bee answers topology/status only — summary degrades field-wise, not wholesale.
        s = await self._summary(health={}, addresses={}, chainstate={})
        assert s["version"] is None
        assert s["overlay"] is None
        assert s["bee_status"] is None
        assert s["connected_peers"] == 137
        assert s["healthy"] is True


# --------------------------------------------------------------------------- #
# /health integration
# --------------------------------------------------------------------------- #
class TestHealthEndpoint:
    def test_health_includes_node_and_stays_ok_when_healthy(self, client):
        summary = {"mode": "light", "connected_peers": 137, "network_availability": "Available",
                   "healthy": True, "warnings": []}
        with patch("app.services.swarm_api.get_node_status_summary", new=AsyncMock(return_value=summary)):
            r = client.get("/health")
        assert r.status_code == 200
        b = r.json()
        assert b["bee_node"]["network_availability"] == "Available"
        assert b["status"] == "ok"

    def test_health_degraded_when_node_unavailable(self, client):
        summary = {"mode": "light", "connected_peers": 15, "network_availability": "Unavailable",
                   "healthy": False, "warnings": ["network availability is 'Unavailable' — ..."]}
        with patch("app.services.swarm_api.get_node_status_summary", new=AsyncMock(return_value=summary)):
            r = client.get("/health")
        assert r.status_code == 200
        b = r.json()
        assert b["bee_node"]["healthy"] is False
        assert b["status"] == "degraded"
        assert any("network availability" in w for w in b["bee_node"]["warnings"])


# --------------------------------------------------------------------------- #
# starting-up vs unreachable
# --------------------------------------------------------------------------- #
class TestStartingUpVsUnreachable:
    """A booting node must not be reported as an unreachable one.

    Bee gates /topology and /status behind startup, answering them with 503
    "Node is syncing" until the postage snapshot is replayed — minutes on a cold
    start — while /health and /addresses answer immediately. Both cases leave
    topo and status empty, so the summary previously reported "could not query
    the Bee node" for a node that had just told us its version.
    """

    HEALTH = {"status": "ok", "version": "2.8.1-7cf53193", "apiVersion": "8.1.0"}
    ADDRESSES = {"overlay": "7c74f416" + "0" * 56}

    @pytest.mark.asyncio
    async def test_starting_up_says_so(self):
        swarm_api._node_status_cache["data"] = None
        client = _mock_client({}, {}, self.HEALTH, self.ADDRESSES, {})
        with patch("app.services.swarm_api.get_client", return_value=client):
            s = await swarm_api.get_node_status_summary(use_cache=False)

        assert any("still starting up" in w for w in s["warnings"])
        assert not any("could not query" in w for w in s["warnings"])
        # It told us its version, so the fields we could read are still present.
        assert s["version"] == "2.8.1-7cf53193"
        assert s["healthy"] is False

    @pytest.mark.asyncio
    async def test_genuinely_unreachable_still_says_could_not_query(self):
        swarm_api._node_status_cache["data"] = None
        c = MagicMock()
        c.get = AsyncMock(side_effect=Exception("connection refused"))
        with patch("app.services.swarm_api.get_client", return_value=c):
            s = await swarm_api.get_node_status_summary(use_cache=False)

        assert any("could not query" in w for w in s["warnings"])
        assert not any("still starting up" in w for w in s["warnings"])
        assert s["healthy"] is False

    @pytest.mark.asyncio
    async def test_overlay_alone_is_enough_to_prove_reachability(self):
        """/health may fail while /addresses succeeds; either proves it answered."""
        swarm_api._node_status_cache["data"] = None
        client = _mock_client({}, {}, {}, self.ADDRESSES, {})
        with patch("app.services.swarm_api.get_client", return_value=client):
            s = await swarm_api.get_node_status_summary(use_cache=False)

        assert any("still starting up" in w for w in s["warnings"])

    @pytest.mark.asyncio
    async def test_gateway_status_stays_ok_while_node_starts(self):
        """Startup must not flip the gateway to degraded.

        network_availability is None during startup, not "Unavailable", so the
        degrade rule from #238 does not fire. Pinned so a future change to the
        warning text cannot quietly change health semantics too.
        """
        swarm_api._node_status_cache["data"] = None
        client = _mock_client({}, {}, self.HEALTH, self.ADDRESSES, {})
        with patch("app.services.swarm_api.get_client", return_value=client):
            s = await swarm_api.get_node_status_summary(use_cache=False)
        assert s["network_availability"] is None
