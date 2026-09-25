"""A payment is collected before what it pays for is delivered (#355-#358).

- a failed settlement delivers nothing (SettleResponse reports failure with
  success=False, not an exception);
- one payment authorization buys one delivery, concurrently or in sequence;
- an authorization that paid for nothing can be retried;
- the settlement transaction reaches the response headers and the audit log.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.testclient import TestClient
from x402.types import SettleResponse, VerifyResponse

from app.x402.dependency import require_x402_payment
from app.x402.middleware import X402Middleware
from app.x402.ratelimit import reset_rate_limiter
from app.x402.settlement import settle_payment
from tests.test_x402_integration import OK_BALANCE, _configure, create_valid_payment_header

TX = "0x" + "cd" * 32
DELIVERED = []


async def buy(request: Request):
    """Stands in for an irreversible step, settled first as the real handlers do."""
    await settle_payment(request)
    await asyncio.sleep(0.02)
    DELIVERED.append(1)
    return {"batchID": "b" * 64}


async def refuse(request: Request):
    from fastapi import HTTPException
    raise HTTPException(status_code=400, detail="refused before any work")


async def no_settle_point():
    DELIVERED.append(1)
    return {"ok": True}


async def settle_then_fail(request: Request):
    from fastapi import HTTPException
    await settle_payment(request)
    raise HTTPException(status_code=502, detail="Bee unavailable")


async def settle_then_crash(request: Request):
    await settle_payment(request)
    raise RuntimeError("unexpected")


async def crash_before_settle(request: Request):
    raise RuntimeError("unexpected")


def _app(facilitator):
    app = FastAPI()
    router = APIRouter(dependencies=[Depends(require_x402_payment)])
    router.add_api_route("/api/v1/stamps/", buy, methods=["POST"])
    router.add_api_route("/api/v1/stamps/fail", settle_then_fail, methods=["POST"])
    router.add_api_route("/api/v1/stamps/crash", settle_then_crash, methods=["POST"])
    router.add_api_route("/api/v1/data/crash", crash_before_settle, methods=["POST"])
    router.add_api_route("/api/v1/data/", refuse, methods=["POST"])
    router.add_api_route("/api/v1/data/manifest", no_settle_point, methods=["POST"])
    app.include_router(router)
    app.add_middleware(X402Middleware, facilitator_client=facilitator)
    return app


def _facilitator(*settles):
    f = MagicMock()
    f.verify = AsyncMock(return_value=VerifyResponse(isValid=True, payer="0xpayer"))
    f.settle = AsyncMock(side_effect=list(settles))
    return f


def ok():
    return SettleResponse(success=True, transaction=TX, network="base-sepolia")


def refused(reason="insufficient_funds"):
    return SettleResponse(success=False, errorReason=reason, network="base-sepolia")


@pytest.fixture
def env():
    DELIVERED.clear()
    reset_rate_limiter()
    with patch("app.x402.dependency.check_base_eth_balance", new=AsyncMock(return_value=OK_BALANCE)), \
         patch("app.x402.dependency._calculate_price_for_request",
               new=AsyncMock(return_value={"price_usd": 0.02, "description": "t"})), \
         patch("app.x402.middleware.settings") as mw, \
         patch("app.x402.dependency.settings") as dep:
        _configure(dep, mw)
        yield
    reset_rate_limiter()


def _run(fac, path="/api/v1/stamps/", header=None):
    with patch("app.x402.dependency._get_facilitator_client", return_value=fac):
        client = TestClient(_app(fac), raise_server_exceptions=False)
        return client.post(path, headers={"X-PAYMENT": header or create_valid_payment_header()})


def test_failed_settlement_delivers_nothing(env):
    fac = _facilitator(refused())
    r = _run(fac)
    assert r.status_code == 402
    assert r.json()["detail"]["code"] == "PAYMENT_SETTLEMENT_FAILED"
    assert DELIVERED == []


def test_settlement_error_delivers_nothing(env):
    fac = _facilitator(RuntimeError("facilitator down"))
    r = _run(fac)
    assert r.status_code == 502
    assert DELIVERED == []


def test_successful_settlement_delivers_once_and_reports_the_transaction(env):
    fac = _facilitator(ok())
    r = _run(fac)
    assert r.status_code == 200
    assert DELIVERED == [1]
    assert r.headers["X-Payment-Transaction"] == TX
    assert fac.settle.await_count == 1  # the middleware does not settle a second time


def test_concurrent_reuse_of_one_authorization_delivers_once(env):
    # Every settle would succeed: only the replay guard can stop the reuse.
    fac = _facilitator(*([ok()] * 10))
    header = create_valid_payment_header()

    async def burst():
        transport = httpx.ASGITransport(app=_app(fac))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await asyncio.gather(*[c.post("/api/v1/stamps/", headers={"X-PAYMENT": header})
                                          for _ in range(10)])

    with patch("app.x402.dependency._get_facilitator_client", return_value=fac):
        responses = asyncio.run(burst())

    assert DELIVERED == [1]
    assert sorted(r.status_code for r in responses) == [200] + [402] * 9
    assert fac.settle.await_count == 1


def test_sequential_reuse_is_refused(env):
    fac = _facilitator(ok(), ok())
    header = create_valid_payment_header()
    assert _run(fac, header=header).status_code == 200
    assert _run(fac, header=header).status_code == 402
    assert DELIVERED == [1]


def test_an_authorization_that_paid_for_nothing_can_be_retried(env):
    fac = _facilitator(ok())
    header = create_valid_payment_header()
    assert _run(fac, path="/api/v1/data/", header=header).status_code == 400
    assert fac.settle.await_count == 0
    assert _run(fac, header=header).status_code == 200


def test_route_without_a_settle_point_is_still_checked(env):
    """The middleware fallback checks success rather than assuming it."""
    fac = _facilitator(refused())
    r = _run(fac, path="/api/v1/data/manifest")
    assert r.status_code == 402
    assert "batchID" not in r.text


def test_chunk_credit_is_not_granted_when_settlement_fails(env):
    """The real top-up handler settles before crediting."""
    from types import SimpleNamespace
    from app.api.endpoints import chunks

    req = MagicMock()
    req.state = SimpleNamespace(x402_mode="paid", x402_payer="0xP", x402_payment=object(),
                                x402_requirements=object())
    req.headers = {}
    req.client = SimpleNamespace(host="1.2.3.4")
    mgr = MagicMock()
    settings = MagicMock(CHUNK_UPLOAD_ENABLED=True, X402_ENABLED=True,
                         BANDWIDTH_CREDIT_MIN_TOPUP_MB=1, BANDWIDTH_CREDIT_MAX_TOPUP_MB=1000)
    fac = _facilitator(refused())
    with patch("app.api.endpoints.chunks.settings", settings), \
         patch("app.api.endpoints.chunks.bandwidth_credit_manager", mgr), \
         patch("app.x402.dependency._get_facilitator_client", return_value=fac):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            asyncio.run(chunks.top_up_credit(req, mb="100"))
    assert exc.value.status_code == 402
    mgr.credit.assert_not_called()


def test_failure_after_settlement_is_recorded_for_refund(env):
    from app.x402.audit import read_audit_log
    fac = _facilitator(ok())
    r = _run(fac, path="/api/v1/stamps/fail")
    assert r.status_code == 502
    assert r.headers["X-Payment-Transaction"] == TX
    assert r.headers["X-Payment-Status"] == "settled_not_delivered"
    events = [e for e in read_audit_log() if e["data"].get("stage") == "delivery_after_settlement"]
    assert events and TX in events[-1]["data"]["reason"]


def test_crash_after_settlement_returns_the_transaction(env):
    fac = _facilitator(ok())
    r = _run(fac, path="/api/v1/stamps/crash")
    assert r.status_code == 500
    assert r.json()["x402_status"] == "settled_not_delivered"
    assert r.json()["transaction"] == TX


def test_crash_before_settlement_releases_the_authorization(env):
    fac = _facilitator(ok())
    header = create_valid_payment_header()
    r = _run(fac, path="/api/v1/data/crash", header=header)
    assert r.status_code == 500 and fac.settle.await_count == 0
    assert _run(fac, header=header).status_code == 200


def test_settlement_error_is_audited_and_the_authorization_can_be_retried(env):
    from app.x402.audit import read_audit_log
    fac = _facilitator(RuntimeError("facilitator down"), ok())
    header = create_valid_payment_header()
    assert _run(fac, header=header).status_code == 502
    assert any(e["data"].get("stage") == "settle" for e in read_audit_log())
    assert _run(fac, header=header).status_code == 200
    assert DELIVERED == [1]


def test_payload_without_an_authorization_is_refused(env):
    fac = _facilitator(ok())
    with patch("app.x402.settlement.authorization_key", return_value=None):
        r = _run(fac)
    assert r.status_code == 402
    assert "EIP-3009" in r.json()["detail"]["error"]
    assert DELIVERED == []


def test_returned_pool_batch_is_available_again(tmp_path):
    """return_released_stamp on the real manager: back in the pool and on disk."""
    import json
    from datetime import datetime, timezone
    from app.services.stamp_pool import PoolStamp, PoolStampStatus, StampPoolManager
    state = tmp_path / "pool.json"
    mgr = StampPoolManager(state_file=str(state))
    batch = "f" * 64
    mgr._pool[batch] = PoolStamp(batch_id=batch, depth=17, amount=1, created_at=datetime.now(timezone.utc),
                                 ttl_at_creation=3600, status=PoolStampStatus.AVAILABLE)
    released = mgr.release_stamp(batch, released_to="1.2.3.4")
    assert mgr.get_available_stamp(17) is None
    mgr.return_released_stamp(released)
    assert mgr.get_available_stamp(17).batch_id == batch
    assert batch in json.dumps(json.load(open(state)))
    assert mgr.release_stamp(batch) is not None
