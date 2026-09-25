"""PATCH /stamps/{id}/extend is paid, owner-restricted and floored (#350).

Extend spends the gateway's BZZ exactly like a purchase. It used to sit outside
the x402 gate (only POST routes were protected), topped up any batch on the
node, and accepted any positive legacy amount.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from x402.types import SettleResponse, VerifyResponse

from fastapi import Depends, FastAPI

from app.api.endpoints import stamps
from app.core.config import settings
from app.x402.dependency import require_x402_payment
from app.services.stamp_ownership import stamp_ownership_manager
from app.x402.ratelimit import reset_rate_limiter
from tests.test_x402_integration import OK_BALANCE, create_valid_payment_header

# The production app attaches the x402 dependency only when X402_ENABLED is set
# at import time, which it is not in the test process. Mount the real stamps
# router with the real dependency instead.
app = FastAPI()
app.include_router(stamps.router, prefix="/api/v1/stamps", dependencies=[Depends(require_x402_payment)])

BATCH = "e" * 64
OWNER = "0xaaaa000000000000000000000000000000000001"
OTHER = "0xbbbb000000000000000000000000000000000002"
CHAINSTATE = {"currentPrice": "24000", "minimumValidityBlocks": 17280}
FUNDS_OK = {"sufficient": True, "wallet_balance_bzz": 100.0, "required_bzz": 0.01, "shortfall_bzz": 0.0}


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setattr(settings, "X402_ENABLED", True)
    monkeypatch.setattr(settings, "X402_FREE_TIER_ENABLED", True)
    monkeypatch.setattr(settings, "X402_PAY_TO_ADDRESS", "0xpayee")
    reset_rate_limiter()
    saved = dict(stamp_ownership_manager._registry)
    stamp_ownership_manager._registry = {}
    extend = AsyncMock(return_value=BATCH)
    with patch("app.x402.dependency.check_base_eth_balance", new=AsyncMock(return_value=OK_BALANCE)), \
         patch("app.services.swarm_api.get_all_stamps_processed",
               new=AsyncMock(return_value=[{"batchID": BATCH, "depth": 20}])), \
         patch("app.services.swarm_api.get_chainstate", new=AsyncMock(return_value=CHAINSTATE)), \
         patch("app.x402.pricing.get_chainstate", new=AsyncMock(return_value=CHAINSTATE)), \
         patch("app.services.swarm_api.check_sufficient_funds", new=AsyncMock(return_value=FUNDS_OK)), \
         patch("app.services.swarm_api.extend_postage_stamp", new=extend):
        yield extend
    stamp_ownership_manager._registry = saved
    reset_rate_limiter()


def _patch(headers=None, body=None):
    return TestClient(app).patch(f"/api/v1/stamps/{BATCH}/extend",
                                 json=body or {"duration_hours": 24}, headers=headers or {})


def _paying(payer):
    fac = MagicMock()
    fac.verify = AsyncMock(return_value=VerifyResponse(isValid=True, payer=payer))
    # settle must be awaitable too. The handler settles immediately before the
    # Bee call (#398) and awaits this; with settle left as a bare MagicMock the
    # await raised TypeError inside settle_payment, which surfaced as a 502
    # PAYMENT_SETTLEMENT_UNAVAILABLE and looked like a payment problem rather
    # than a test double missing a method.
    fac.settle = AsyncMock(return_value=SettleResponse(
        success=True, transaction="0x" + "ab" * 32, network="base-sepolia"
    ))
    return patch("app.x402.dependency._get_facilitator_client", return_value=fac)


def test_unpaid_extend_gets_402_priced_at_the_batch_depth(gated):
    r = _patch()
    assert r.status_code == 402
    accepts = r.json()["detail"]["accepts"][0]
    assert "depth 20" in accepts["description"]
    gated.assert_not_called()


def test_extend_price_matches_what_is_topped_up(gated):
    import asyncio
    from app.services import swarm_api
    from app.x402.pricing import _price_from_cost_bzz
    r = _patch(body={"duration_hours": 48})
    # What the handler would top up: same amount calculation, the batch's depth.
    amount = swarm_api.calculate_stamp_amount(48, 24000, minimum_validity_blocks=17280)
    cost = swarm_api.plur_to_bzz(swarm_api.calculate_stamp_total_cost(amount, 20))
    expected = _price_from_cost_bzz(cost)["price_usd"]
    assert r.json()["detail"]["accepts"][0]["maxAmountRequired"] == str(int(expected * 1_000_000))


def test_untracked_batch_cannot_be_extended(gated):
    r = _patch(headers={"X-Payment-Mode": "free"})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "STAMP_OWNERSHIP_DENIED"
    gated.assert_not_called()


def test_pool_inventory_cannot_be_extended(gated):
    stamp_ownership_manager._registry[BATCH] = {"owner": "pool", "mode": "pool"}
    assert _patch(headers={"X-Payment-Mode": "free"}).status_code == 403
    gated.assert_not_called()


def test_shared_batch_can_be_extended_on_the_free_tier(gated):
    stamp_ownership_manager._registry[BATCH] = {"owner": "shared", "mode": "free"}
    assert _patch(headers={"X-Payment-Mode": "free"}).status_code == 200
    gated.assert_awaited_once()


def test_only_the_owner_may_extend_an_owned_batch(gated):
    stamp_ownership_manager._registry[BATCH] = {"owner": OWNER, "mode": "paid"}
    with _paying(OTHER):
        r = _patch(headers={"X-PAYMENT": create_valid_payment_header(payer=OTHER)})
    assert r.status_code == 403
    gated.assert_not_called()
    with _paying(OWNER):
        r = _patch(headers={"X-PAYMENT": create_valid_payment_header(payer=OWNER)})
    assert r.status_code == 200, r.text


def test_dust_amounts_are_refused_before_any_spend(gated):
    stamp_ownership_manager._registry[BATCH] = {"owner": "shared", "mode": "free"}
    r = _patch(headers={"X-Payment-Mode": "free"}, body={"amount": 1})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "EXTENSION_TOO_SMALL"
    gated.assert_not_called()


def test_legacy_amount_is_priced_as_topped_up(gated):
    from app.services import swarm_api
    from app.x402.pricing import _price_from_cost_bzz
    amount = 24000 * 17280 * 3
    r = _patch(body={"amount": amount})
    cost = swarm_api.plur_to_bzz(swarm_api.calculate_stamp_total_cost(amount, 20))
    expected = _price_from_cost_bzz(cost)["price_usd"]
    assert r.json()["detail"]["accepts"][0]["maxAmountRequired"] == str(int(expected * 1_000_000))


def test_free_tier_rate_limit_applies_to_extend(gated, monkeypatch):
    monkeypatch.setattr(settings, "X402_FREE_TIER_RATE_LIMIT", 2)
    stamp_ownership_manager._registry[BATCH] = {"owner": "shared", "mode": "free"}
    codes = [_patch(headers={"X-Payment-Mode": "free"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_unknown_current_price_is_refused(gated):
    stamp_ownership_manager._registry[BATCH] = {"owner": "shared", "mode": "free"}
    with patch("app.services.swarm_api.get_chainstate",
               new=AsyncMock(return_value={"currentPrice": "0"})):
        r = _patch(headers={"X-Payment-Mode": "free"}, body={"amount": 1_000_000_000})
    assert r.status_code == 503
    gated.assert_not_called()
