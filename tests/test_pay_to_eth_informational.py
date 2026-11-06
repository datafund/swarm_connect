# tests/test_pay_to_eth_informational.py
"""The pay-to wallet's Base ETH balance is informational only (#663).

With x402 `exact` (EIP-3009) the facilitator submits and pays for the USDC
transfer; the pay-to wallet only receives. A pay-to wallet with 0 ETH, for
example a new Safe, is a normal state. Its balance is still reported in
/health and in the gateway_base_eth_balance gauge, but it never degrades
/health, never fires an alert and never blocks a request.
"""
import json
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, settings
from app.services import health_snapshot
from app.x402 import base_balance
from app.x402.models_v1 import SettleResponse, VerifyResponse
from app.x402.ratelimit import reset_rate_limiter
from tests.test_x402_integration import (
    _configure,
    _make_app,
    create_stamp,
    create_valid_payment_header,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALERT_RULES = os.path.join(REPO_ROOT, "monitoring", "alerting", "alert-rules.json")

PAY_TO = "0x" + "1" * 40
NODE = {"healthy": True, "network_availability": "Available", "warnings": []}
GNOSIS = {
    "can_accept": True, "wallet_address": "0xBee", "chequebook_address": "0xCheque",
    "xbzz_ok": True, "xdai_ok": True, "chequebook_ok": True,
    "balances": {"xbzz": {}, "xdai": {}, "chequebook": {}}, "warnings": [], "errors": [],
}


@pytest.fixture(autouse=True)
def _clean_cache():
    base_balance.clear_balance_cache()
    yield
    base_balance.clear_balance_cache()


async def _read_base(monkeypatch, rpc):
    """Run the real check_base_eth_balance against a fake RPC."""
    monkeypatch.setattr(settings, "X402_PAY_TO_ADDRESS", PAY_TO)
    monkeypatch.setattr(base_balance, "_get_eth_balance_from_rpc", rpc)
    return await base_balance.check_base_eth_balance()


def _health(monkeypatch, base_eth):
    from app.main import build_health_response
    monkeypatch.setattr(settings, "X402_ENABLED", True)
    snap = health_snapshot.HealthSnapshot(node=dict(NODE), gnosis=dict(GNOSIS), base_eth=base_eth,
                                          x402=True, taken_at=time.monotonic())
    return build_health_response(snap)


@pytest.mark.asyncio
async def test_zero_eth_pay_to_wallet_does_not_degrade_health(monkeypatch):
    base = await _read_base(monkeypatch, AsyncMock(return_value=0))
    body = _health(monkeypatch, base)

    assert body["status"] == "ok"
    wallet = body["x402"]["base_wallet"]
    assert wallet["ok"] is True
    assert wallet["is_critical"] is False
    assert wallet["informational"] is True
    assert wallet["fetched"] is True
    # Still reported, as information.
    assert wallet["address"] == PAY_TO
    assert wallet["balance_eth"] == 0.0
    assert body["x402"]["warnings"] == []
    assert body["x402"]["errors"] == []


@pytest.mark.asyncio
async def test_unreadable_pay_to_balance_does_not_degrade_health(monkeypatch):
    base = await _read_base(monkeypatch, AsyncMock(side_effect=RuntimeError("rpc down")))
    body = _health(monkeypatch, base)

    assert body["status"] == "ok"
    wallet = body["x402"]["base_wallet"]
    assert wallet["ok"] is True
    assert wallet["is_critical"] is False
    assert wallet["fetched"] is False
    assert body["x402"]["warnings"] == []
    assert body["x402"]["errors"] == []


@pytest.mark.asyncio
async def test_zero_eth_check_logs_no_warning(monkeypatch):
    with patch.object(base_balance, "logger") as log:
        result = await _read_base(monkeypatch, AsyncMock(return_value=0))
    assert result["warning"] is None
    assert result["ok"] is True and result["is_critical"] is False
    log.warning.assert_not_called()
    log.error.assert_not_called()


@patch("app.x402.dependency._get_facilitator_client")
@patch("app.x402.dependency.get_price_quote")
@patch("app.x402.middleware.settings")
@patch("app.x402.dependency.settings")
def test_zero_eth_pay_to_wallet_accepts_payments(mock_dep, mock_mw, mock_price, mock_get_fac, monkeypatch):
    reset_rate_limiter()
    try:
        _configure(mock_dep, mock_mw, free_tier=True)
        mock_dep.X402_FACILITATOR_URL = "https://x402.org/facilitator"
        mock_price.return_value = {"price_usd": 0.05, "description": "Test"}
        fac = MagicMock()
        fac.verify = AsyncMock(return_value=VerifyResponse(
            is_valid=True, invalid_reason=None, payer="0x1234567890abcdef1234567890abcdef12345678"))
        mock_get_fac.return_value = fac
        settle_fac = MagicMock()
        settle_fac.settle = AsyncMock(return_value=SettleResponse(
            success=True, transaction="0x" + "ab" * 32, network="base-sepolia"))
        monkeypatch.setattr(base_balance, "_get_eth_balance_from_rpc", AsyncMock(return_value=0))

        client = TestClient(_make_app(("POST", "/api/v1/stamps/", create_stamp),
                                      facilitator_client=settle_fac))
        assert client.post("/api/v1/stamps/").status_code == 402
        assert client.post("/api/v1/stamps/", headers={"X-Payment-Mode": "free"}).status_code == 200
        paid = client.post("/api/v1/stamps/", headers={"X-PAYMENT": create_valid_payment_header()})
        assert paid.status_code == 200
    finally:
        reset_rate_limiter()


def test_no_alert_rule_on_pay_to_eth():
    with open(ALERT_RULES) as f:
        rules = json.load(f)
    titles = {r["title"] for r in rules}
    assert "Low Base ETH" not in titles
    for rule in rules:
        for query in rule["data"]:
            assert "gateway_base_eth_balance" not in query["model"].get("expr", ""), rule["title"]


def test_removed_threshold_env_vars_are_ignored(monkeypatch):
    """An operator who still sets the removed variables is not refused at startup."""
    monkeypatch.setenv("X402_BASE_ETH_WARN_THRESHOLD", "0.005")
    monkeypatch.setenv("X402_BASE_ETH_CRITICAL_THRESHOLD", "0.001")
    s = Settings()
    assert not hasattr(s, "X402_BASE_ETH_WARN_THRESHOLD")
    assert not hasattr(s, "X402_BASE_ETH_CRITICAL_THRESHOLD")
