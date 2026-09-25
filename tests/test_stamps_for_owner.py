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


def test_receipt_timeout_returns_202_with_tx_hash(client, env):
    """Broadcast but unconfirmed: 202 + txHash, not an error that implies no charge (#368)."""
    from app.services.gnosis_chain import TransactionPending
    env["gc"].create_batch = AsyncMock(side_effect=TransactionPending("0xfeed", "0x" + "cd" * 32, OWNER))
    with patch("app.api.endpoints.stamps_for_owner.settings", _settings()):
        r = client.post("/api/v1/stamps/for-owner", json={"owner": OWNER, "size": "small"})
    assert r.status_code == 202, r.text
    b = r.json()
    assert b["txHash"] == "0xfeed"
    assert b["batchID"] == "cd" * 32
    assert b["confirmed"] is False
    assert "may still mine" in b["message"]
    # Recorded like a created batch, so the record exists if it mines.
    env["rp"].assert_called_once_with("cd" * 32)
    assert env["own"].register_stamp.call_args.kwargs["batch_id"] == "cd" * 32


def test_confirmed_batch_reports_confirmed(client, env):
    with patch("app.api.endpoints.stamps_for_owner.settings", _settings()):
        r = client.post("/api/v1/stamps/for-owner", json={"owner": OWNER, "size": "small"})
    assert r.status_code == 201 and r.json()["confirmed"] is True


def test_signer_busy_returns_503_and_is_not_a_success(client, env):
    """Nothing was sent; a 5xx is not settled by the x402 middleware."""
    from app.services.gnosis_chain import SignerBusy
    env["gc"].create_batch = AsyncMock(side_effect=SignerBusy("signer has an unconfirmed transaction (nonce 7)"))
    with patch("app.api.endpoints.stamps_for_owner.settings", _settings()):
        r = client.post("/api/v1/stamps/for-owner", json={"owner": OWNER, "size": "small"})
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "SIGNER_BUSY"
    env["own"].register_stamp.assert_not_called()
