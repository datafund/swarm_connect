# tests/test_stamps_for_owner.py
"""
Tests for POST /api/v1/stamps/for-owner (Flow B #228).
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app

OWNER = "0x571dEAC541E65312Bdb027E1C570e2751f8A6795"


@pytest.fixture
def client():
    return TestClient(app)


def _settings(enabled=True, require_wl=False, whitelist=None,
              max_depth=22, max_bzz=1.0, max_dur=168):
    ms = MagicMock()
    ms.STAMP_PURCHASE_FOR_OTHERS_ENABLED = enabled
    ms.STAMP_FOR_OTHERS_REQUIRE_WHITELIST = require_wl
    ms.get_stamp_for_others_whitelist.return_value = [a.lower() for a in (whitelist or [])]
    ms.STAMP_FOR_OTHERS_MAX_DEPTH = max_depth
    ms.STAMP_FOR_OTHERS_MAX_BZZ = max_bzz
    ms.STAMP_FOR_OTHERS_MAX_DURATION_HOURS = max_dur
    return ms


@pytest.fixture
def env():
    """Patch the chain client, chainstate, registry, and tracker (no real spend/IO)."""
    gc = MagicMock()
    gc.is_configured = True

    async def _cb(owner, amount, depth, immutable=False):
        return {"batch_id": "0x" + "ab" * 32, "tx_hash": "0xdead", "owner": owner}

    gc.create_batch = AsyncMock(side_effect=_cb)
    gc.preflight = AsyncMock(return_value={"is_critical": False, "warnings": []})
    with patch("app.services.swarm_api.get_chainstate", AsyncMock(return_value={"currentPrice": "100000"})), \
         patch("app.api.endpoints.stamps_for_owner.gnosis_chain_client", gc), \
         patch("app.api.endpoints.stamps_for_owner.record_purchase") as rp, \
         patch("app.api.endpoints.stamps_for_owner.stamp_ownership_manager") as own:
        yield {"gc": gc, "rp": rp, "own": own}


def test_toggle_off_returns_404(client, env):
    with patch("app.api.endpoints.stamps_for_owner.settings", _settings(enabled=False)):
        r = client.post("/api/v1/stamps/for-owner", json={"owner": OWNER, "size": "small"})
    assert r.status_code == 404


def test_happy_path_creates_batch(client, env):
    with patch("app.api.endpoints.stamps_for_owner.settings", _settings()):
        r = client.post("/api/v1/stamps/for-owner", json={"owner": OWNER, "size": "small"})
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["batchID"] == "ab" * 32          # 0x stripped
    assert b["owner"] == OWNER
    assert b["txHash"] == "0xdead"
    assert b["depth"] == 17                    # small preset
    # chain client called with owner + depth; registry + tracker recorded
    env["gc"].create_batch.assert_awaited_once()
    assert env["gc"].create_batch.call_args.args[0] == OWNER
    env["rp"].assert_called_once_with("ab" * 32)
    own_call = env["own"].register_stamp.call_args.kwargs
    assert own_call["owner"] == OWNER and own_call["source"] == "created_for_owner"


def test_invalid_owner_422(client, env):
    with patch("app.api.endpoints.stamps_for_owner.settings", _settings()):
        r = client.post("/api/v1/stamps/for-owner", json={"owner": "not-an-address", "size": "small"})
    assert r.status_code == 422


def test_depth_from_size_medium(client, env):
    with patch("app.api.endpoints.stamps_for_owner.settings", _settings()):
        r = client.post("/api/v1/stamps/for-owner", json={"owner": OWNER, "size": "medium"})
    assert r.status_code == 201
    assert r.json()["depth"] == 20
    assert env["gc"].create_batch.call_args.args[2] == 20  # depth passed to chain client


def test_chain_client_not_configured_503(client):
    gc = MagicMock(); gc.is_configured = False
    with patch("app.api.endpoints.stamps_for_owner.settings", _settings()), \
         patch("app.api.endpoints.stamps_for_owner.gnosis_chain_client", gc), \
         patch("app.services.swarm_api.get_chainstate", AsyncMock(return_value={"currentPrice": "100000"})):
        r = client.post("/api/v1/stamps/for-owner", json={"owner": OWNER, "size": "small"})
    assert r.status_code == 503
