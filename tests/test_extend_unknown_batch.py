"""Extend of a batch the Bee node does not list answers 404 before any offer (#572).

The x402 dependency looks the batch up to price the top-up at its depth. A batch
it did not find used to be quoted at depth 17: an unpaid request got a 402 with
`accepts`, a paid one had its payment verified, and a free-tier one used up a
rate-limit slot, all for a request the route then refused with 404.

These tests mount the real stamps router with the real dependency and the real
middleware, the way app/main.py does with x402 enabled, and mock only Bee and
the facilitator.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import httpx2
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from x402.types import SettleResponse, VerifyResponse

from app.api.endpoints import stamps
from app.core.config import settings
from app.services.stamp_ownership import stamp_ownership_manager
from app.x402.dependency import require_x402_payment
from app.x402.middleware import X402Middleware
from app.x402.ratelimit import reset_rate_limiter
from tests.test_x402_integration import create_valid_payment_header

KNOWN = "e" * 64
UNKNOWN = "d" * 64
PAYER = "0xaaaa000000000000000000000000000000000001"
CHAINSTATE = {"currentPrice": "24000", "minimumValidityBlocks": 17280}
FUNDS_OK = {"sufficient": True, "wallet_balance_bzz": 100.0, "required_bzz": 0.01, "shortfall_bzz": 0.0}
NOT_FOUND = {"detail": f"Stamp batch with ID '{UNKNOWN}' not found on the connected Swarm node."}


def _facilitator():
    fac = MagicMock()
    fac.verify = AsyncMock(return_value=VerifyResponse(isValid=True, payer=PAYER))
    fac.settle = AsyncMock(return_value=SettleResponse(
        success=True, transaction="0x" + "ab" * 32, network="base-sepolia"))
    return fac


def _app(fac):
    app = FastAPI()
    app.include_router(stamps.router, prefix="/api/v1/stamps",
                       dependencies=[Depends(require_x402_payment)])
    app.add_middleware(X402Middleware, facilitator_client=fac)
    return app


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(settings, "X402_ENABLED", True)
    monkeypatch.setattr(settings, "X402_FREE_TIER_ENABLED", True)
    monkeypatch.setattr(settings, "X402_PAY_TO_ADDRESS", "0x" + "11" * 20)
    reset_rate_limiter()
    saved = dict(stamp_ownership_manager._registry)
    stamp_ownership_manager._registry = {KNOWN: {"owner": "shared", "mode": "free"}}
    fac = _facilitator()
    listing = AsyncMock(return_value=[{"batchID": KNOWN, "depth": 20}])
    extend = AsyncMock(return_value=KNOWN)
    with patch("app.services.swarm_api.get_all_stamps_processed", new=listing), \
         patch("app.services.swarm_api.get_chainstate", new=AsyncMock(return_value=CHAINSTATE)), \
         patch("app.x402.pricing.get_chainstate", new=AsyncMock(return_value=CHAINSTATE)), \
         patch("app.services.swarm_api.check_sufficient_funds", new=AsyncMock(return_value=FUNDS_OK)), \
         patch("app.services.swarm_api.extend_postage_stamp", new=extend), \
         patch("app.x402.dependency._get_facilitator_client", return_value=fac):
        yield {"client": TestClient(_app(fac)), "fac": fac, "listing": listing, "extend": extend}
    stamp_ownership_manager._registry = saved
    reset_rate_limiter()


def _extend(env, batch, headers=None, body=None):
    return env["client"].patch(f"/api/v1/stamps/{batch}/extend",
                               json=body if body is not None else {"duration_hours": 24},
                               headers=headers or {})


def test_unknown_batch_unpaid_gets_404_not_a_402_offer(env):
    r = _extend(env, UNKNOWN)
    assert r.status_code == 404, r.text
    assert r.json() == NOT_FOUND
    assert "accepts" not in r.text
    # The dependency's lookup decided it: one Bee listing, no second one.
    assert env["listing"].await_count == 1
    env["extend"].assert_not_called()


def test_unknown_batch_uppercase_gets_the_same_404(env):
    r = _extend(env, UNKNOWN.upper())
    assert r.status_code == 404, r.text
    assert r.json() == NOT_FOUND


def test_unknown_batch_with_payment_is_not_verified_or_settled(env):
    r = _extend(env, UNKNOWN, headers={"X-PAYMENT": create_valid_payment_header(payer=PAYER)})
    assert r.status_code == 404, r.text
    assert r.json() == NOT_FOUND
    assert "accepts" not in r.text
    env["fac"].verify.assert_not_called()
    env["fac"].settle.assert_not_called()
    assert "X-PAYMENT-RESPONSE" not in r.headers
    env["extend"].assert_not_called()


def test_unknown_batch_on_free_tier_is_not_counted(env, monkeypatch):
    # Decision: a request that can never succeed does not use a free-tier slot.
    monkeypatch.setattr(settings, "X402_FREE_TIER_RATE_LIMIT", 1)
    free = {"X-Payment-Mode": "free"}
    for _ in range(3):
        r = _extend(env, UNKNOWN, headers=free)
        assert r.status_code == 404, r.text
        assert r.json() == NOT_FOUND
        assert "X-RateLimit-Remaining" not in r.headers
    # The one slot is still there for a batch that exists.
    r = _extend(env, KNOWN, headers=free)
    assert r.status_code == 200, r.text
    env["extend"].assert_awaited_once()


def test_404_body_is_the_routes_own(env, monkeypatch):
    gated = _extend(env, UNKNOWN)
    monkeypatch.setattr(settings, "X402_ENABLED", False)
    ungated = _extend(env, UNKNOWN)
    assert ungated.status_code == gated.status_code == 404
    assert ungated.json() == gated.json()


def test_validation_422_still_comes_before_the_404(env):
    r = _extend(env, UNKNOWN, body={"duration_hours": "soon"})
    assert r.status_code == 422, r.text
    env["listing"].assert_not_awaited()


def test_known_batch_is_still_quoted_at_its_depth(env):
    r = _extend(env, KNOWN)
    assert r.status_code == 402, r.text
    accepts = r.json()["detail"]["accepts"][0]
    assert "depth 20" in accepts["description"]
    env["extend"].assert_not_called()


def test_known_batch_paid_is_extended(env):
    r = _extend(env, KNOWN, headers={"X-PAYMENT": create_valid_payment_header(payer=PAYER)})
    assert r.status_code == 200, r.text
    env["fac"].verify.assert_awaited_once()
    env["extend"].assert_awaited_once()


@pytest.mark.parametrize("headers", [
    pytest.param({}, id="unpaid"),
    pytest.param({"X-PAYMENT": "x"}, id="paid"),
    pytest.param({"X-Payment-Mode": "free"}, id="free"),
])
def test_bee_unreachable_is_503_not_404(env, monkeypatch, headers):
    # Documented behaviour: the lookup cannot tell "no such batch" from "no
    # answer", so a Bee outage is the dependency's pricing 503, before any
    # offer, verification or free-tier count, never a 404.
    if "X-PAYMENT" in headers:
        headers = {"X-PAYMENT": create_valid_payment_header(payer=PAYER)}
    monkeypatch.setattr(settings, "X402_FREE_TIER_RATE_LIMIT", 1)
    env["listing"].side_effect = httpx2.ConnectError("bee down")
    for _ in range(2):
        r = _extend(env, UNKNOWN, headers=headers)
        assert r.status_code == 503, r.text
        assert "accepts" not in r.text
        assert r.json()["detail"]["detail"] == "Failed to calculate price for this operation."
    env["fac"].verify.assert_not_called()
    env["extend"].assert_not_called()
    # Not counted against the free tier.
    env["listing"].side_effect = None
    r = _extend(env, KNOWN, headers={"X-Payment-Mode": "free"})
    assert r.status_code == 200, r.text
