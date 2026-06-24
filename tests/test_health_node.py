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


def _mock_client(topo, status):
    def fake_get(url, timeout=8):
        return _resp(topo if url.endswith("topology") else status)
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
