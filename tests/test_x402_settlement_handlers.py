"""The real paid handlers settle before their irreversible step (#355, #357).

Each test mounts the real router with the real x402 dependency and middleware,
has the facilitator refuse settlement, and checks that the irreversible call
was never made.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from x402.types import SettleResponse, VerifyResponse

from app.core.config import settings
from app.x402.dependency import require_x402_payment, settle_payment_if_offered
from app.x402.middleware import X402Middleware
from app.x402.ratelimit import reset_rate_limiter
from tests.test_x402_integration import OK_BALANCE, create_valid_payment_header

BATCH = "d" * 64


def _fac(settle):
    f = MagicMock()
    f.verify = AsyncMock(return_value=VerifyResponse(isValid=True, payer="0xpayer"))
    f.settle = AsyncMock(return_value=settle)
    return f


REFUSED = SettleResponse(success=False, errorReason="insufficient_funds")


@pytest.fixture
def x402_on(monkeypatch):
    monkeypatch.setattr(settings, "X402_ENABLED", True)
    monkeypatch.setattr(settings, "X402_PAY_TO_ADDRESS", "0xpayee")
    monkeypatch.setattr(settings, "X402_NETWORK", "base-sepolia")
    reset_rate_limiter()
    with patch("app.x402.dependency._calculate_price_for_request",
               new=AsyncMock(return_value={"price_usd": 0.02, "description": "t"})):
        yield
    reset_rate_limiter()


def _client(router, prefix, fac, dep=require_x402_payment):
    app = FastAPI()
    app.include_router(router, prefix=prefix, dependencies=[Depends(dep)])
    app.add_middleware(X402Middleware, facilitator_client=fac)
    return TestClient(app)


def test_stamp_purchase_buys_nothing_when_settlement_fails(x402_on):
    from app.api.endpoints import stamps
    fac = _fac(REFUSED)
    buy = AsyncMock(return_value=BATCH)
    with patch("app.x402.dependency._get_facilitator_client", return_value=fac), \
         patch("app.services.swarm_api.get_chainstate",
               new=AsyncMock(return_value={"currentPrice": "24000", "minimumValidityBlocks": 17280})), \
         patch("app.services.swarm_api.check_sufficient_funds",
               new=AsyncMock(return_value={"sufficient": True, "wallet_balance_bzz": 10.0,
                                           "required_bzz": 0.01, "shortfall_bzz": 0.0})), \
         patch("app.services.swarm_api.purchase_postage_stamp", new=buy):
        r = _client(stamps.router, "/api/v1/stamps", fac).post(
            "/api/v1/stamps/", json={"duration_hours": 24, "depth": 17},
            headers={"X-PAYMENT": create_valid_payment_header()})
    assert r.status_code == 402
    buy.assert_not_called()


def test_upload_uploads_nothing_when_settlement_fails(x402_on, monkeypatch):
    from app.api.endpoints import data
    from app.services.stamp_ownership import stamp_ownership_manager
    monkeypatch.setitem(stamp_ownership_manager._registry, BATCH, {"owner": "0xpayer", "mode": "paid"})
    fac = _fac(REFUSED)
    upload = AsyncMock(return_value="e" * 64)
    validate = AsyncMock(return_value={})
    with patch("app.x402.dependency._get_facilitator_client", return_value=fac), \
         patch("app.api.endpoints.data.upload_data_to_swarm", new=upload), \
         patch("app.api.endpoints.data.validate_stamp_for_upload", new=validate):
        r = _client(data.router, "/api/v1/data", fac).post(
            f"/api/v1/data/?stamp_id={BATCH}", files={"file": ("a.json", b'{"a":1}', "application/json")},
            headers={"X-PAYMENT": create_valid_payment_header()})
    assert r.status_code == 402
    # A paid upload always validates the stamp first, with the single-batch lookup.
    validate.assert_awaited_once_with(BATCH, local_only=True)
    upload.assert_not_called()


def test_pool_batch_goes_back_when_settlement_fails(x402_on, monkeypatch):
    from app.api.endpoints import pool
    monkeypatch.setattr(settings, "STAMP_POOL_ENABLED", True)
    stamp = SimpleNamespace(batch_id=BATCH, depth=17)
    mgr = pool.stamp_pool_manager
    fac = _fac(REFUSED)
    with patch("app.x402.dependency._get_facilitator_client", return_value=fac), \
         patch.object(mgr, "get_available_stamp", return_value=stamp), \
         patch.object(mgr, "release_stamp", return_value=stamp), \
         patch.object(mgr, "return_released_stamp") as give_back, \
         patch.object(pool.stamp_ownership_manager, "register_stamp") as register:
        r = _client(pool.router, "/api/v1/pool", fac, dep=settle_payment_if_offered).post(
            "/api/v1/pool/acquire", json={"size": "small"},
            headers={"X-PAYMENT": create_valid_payment_header()})
    assert r.status_code == 402
    give_back.assert_called_once_with(stamp)
    register.assert_not_called()


def test_pool_batch_taken_by_someone_else_is_not_charged(x402_on, monkeypatch):
    """The batch is claimed before settling, so losing the race costs nothing."""
    from app.api.endpoints import pool
    monkeypatch.setattr(settings, "STAMP_POOL_ENABLED", True)
    stamp = SimpleNamespace(batch_id=BATCH, depth=17)
    mgr = pool.stamp_pool_manager
    fac = _fac(SettleResponse(success=True, transaction="0xtx"))
    with patch("app.x402.dependency._get_facilitator_client", return_value=fac), \
         patch.object(mgr, "get_available_stamp", return_value=stamp), \
         patch.object(mgr, "release_stamp", return_value=None):
        r = _client(pool.router, "/api/v1/pool", fac, dep=settle_payment_if_offered).post(
            "/api/v1/pool/acquire", json={"size": "small"},
            headers={"X-PAYMENT": create_valid_payment_header()})
    assert r.status_code == 409
    fac.settle.assert_not_called()


def _bee(handler):
    import httpx
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_local_only_validation_uses_one_batch_lookup(monkeypatch):
    import asyncio
    import httpx
    from app.services import swarm_api
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={"batchID": BATCH, "utilization": 0, "depth": 17,
                                         "bucketDepth": 16, "batchTTL": 86400, "usable": True})

    with patch("app.services.swarm_api.get_client", return_value=_bee(handler)):
        info = asyncio.run(swarm_api.validate_stamp_for_upload(BATCH, local_only=True))
    assert seen == [f"/stamps/{BATCH}"]
    assert info is not None


@pytest.mark.parametrize("status,body,code", [
    (404, {"message": "not found"}, "NOT_FOUND"),
    (200, {"batchID": BATCH, "utilization": 2, "depth": 17, "bucketDepth": 16,
           "batchTTL": 86400, "usable": True}, "FULL"),
    (200, {"batchID": BATCH, "utilization": 0, "depth": 17, "bucketDepth": 16,
           "batchTTL": 86400, "usable": False}, "NOT_USABLE"),
])
def test_local_only_validation_refuses_what_bee_would_refuse(status, body, code):
    import asyncio
    import httpx
    from app.services import swarm_api
    with patch("app.services.swarm_api.get_client",
               return_value=_bee(lambda r: httpx.Response(status, json=body))):
        with pytest.raises(swarm_api.StampValidationError) as exc:
            asyncio.run(swarm_api.validate_stamp_for_upload(BATCH, local_only=True))
    assert exc.value.code == code
