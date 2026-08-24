# tests/test_stamps_for_owner_limits.py
"""
Tests for the for-owner authorization allow-list + alpha caps (Flow B #230).
All guards must be enforced BEFORE any on-chain spend.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app

OWNER = "0x571dEAC541E65312Bdb027E1C570e2751f8A6795"
OTHER = "0x" + "99" * 20


@pytest.fixture
def client():
    return TestClient(app)


def _settings(enabled=True, require_wl=False, whitelist=None,
              max_depth=22, max_bzz=1.0, max_dur=168, free_tier=False):
    ms = MagicMock()
    ms.STAMP_PURCHASE_FOR_OTHERS_ENABLED = enabled
    ms.STAMP_FOR_OTHERS_REQUIRE_WHITELIST = require_wl
    ms.get_stamp_for_others_whitelist.return_value = [a.lower() for a in (whitelist or [])]
    ms.STAMP_FOR_OTHERS_MAX_DEPTH = max_depth
    ms.STAMP_FOR_OTHERS_MAX_BZZ = max_bzz
    ms.STAMP_FOR_OTHERS_MAX_DURATION_HOURS = max_dur
    ms.STAMP_FOR_OTHERS_FREE_TIER_ENABLED = free_tier
    return ms


@pytest.fixture
def env():
    gc = MagicMock()
    gc.is_configured = True

    async def _cb(owner, amount, depth, immutable=False):
        return {"batch_id": "0x" + "ab" * 32, "tx_hash": "0xdead", "owner": owner}

    gc.create_batch = AsyncMock(side_effect=_cb)
    gc.preflight = AsyncMock(return_value={"is_critical": False, "warnings": []})
    with patch("app.services.swarm_api.get_chainstate", AsyncMock(return_value={"currentPrice": "100000"})), \
         patch("app.api.endpoints.stamps_for_owner.gnosis_chain_client", gc), \
         patch("app.api.endpoints.stamps_for_owner.record_purchase"), \
         patch("app.api.endpoints.stamps_for_owner.stamp_ownership_manager"):
        yield gc


def _post(client, **body):
    body.setdefault("owner", OWNER)
    body.setdefault("size", "small")
    return client.post("/api/v1/stamps/for-owner", json=body)


# --- allow-list ---
def test_non_allowlisted_owner_403_and_no_spend(client, env):
    with patch("app.api.endpoints.stamps_for_owner.settings", _settings(require_wl=True, whitelist=[OTHER])):
        r = _post(client)
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "OWNER_NOT_ALLOWLISTED"
    env.create_batch.assert_not_called()  # no spend


def test_allowlisted_owner_passes(client, env):
    with patch("app.api.endpoints.stamps_for_owner.settings", _settings(require_wl=True, whitelist=[OWNER])):
        r = _post(client)
    assert r.status_code == 201


def test_require_whitelist_false_disables_check(client, env):
    with patch("app.api.endpoints.stamps_for_owner.settings", _settings(require_wl=False, whitelist=[])):
        r = _post(client)
    assert r.status_code == 201


# --- caps (all rejected before spend) ---
def test_depth_cap(client, env):
    with patch("app.api.endpoints.stamps_for_owner.settings", _settings(max_depth=17)):
        r = _post(client, size="medium")  # depth 20 > 17
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "DEPTH_TOO_HIGH"
    env.create_batch.assert_not_called()


def test_duration_cap(client, env):
    with patch("app.api.endpoints.stamps_for_owner.settings", _settings(max_dur=24)):
        r = _post(client, duration_hours=48)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "DURATION_TOO_LONG"
    env.create_batch.assert_not_called()


def test_cost_cap(client, env):
    with patch("app.api.endpoints.stamps_for_owner.settings", _settings(max_bzz=0.001)):
        r = _post(client)  # depth-17/24h ~0.02 BZZ > 0.001
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "COST_TOO_HIGH"
    env.create_batch.assert_not_called()


def test_at_limit_succeeds(client, env):
    # depth exactly at the cap is allowed
    with patch("app.api.endpoints.stamps_for_owner.settings", _settings(max_depth=17)):
        r = _post(client, size="small")  # depth 17 == max
    assert r.status_code == 201


# --- signer preflight (#231): never spend if the gateway wallet can't fund it ---
def test_insufficient_signer_funds_503_no_spend(client, env):
    env.preflight = AsyncMock(return_value={
        "is_critical": True, "warnings": ["signer xDAI 0.0 below critical"]})
    with patch("app.api.endpoints.stamps_for_owner.settings", _settings()):
        r = _post(client)
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "SIGNER_INSUFFICIENT_FUNDS"
    env.create_batch.assert_not_called()  # no spend attempted


def test_preflight_called_with_batch_cost(client, env):
    with patch("app.api.endpoints.stamps_for_owner.settings", _settings()):
        r = _post(client)
    assert r.status_code == 201
    env.preflight.assert_awaited_once()
    # required_plur passed so preflight can compare against xBZZ
    assert env.preflight.call_args.kwargs.get("required_plur", 0) > 0
