# tests/test_bee_gate_payments.py
"""The Bee call gate never hides what a client needs about a payment (#588 review).

503 BEE_BUSY tells the client "nothing was charged; retry". Once a payment may
have moved, that is not known to be true, and replacing the gateway's own
answer (an outcome-unknown 502 with X-Payment-Status, a 409 for an
authorization whose outcome is unknown, a settled-not-delivered error) would
drop the headers and the code a client relies on not to pay twice.

Here a Bee call refused by the gate is followed by each payment outcome, and
the payment answer must come through unchanged.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from x402.types import SettleResponse, VerifyResponse

from app.middleware.bee_gate import BeeGateMiddleware
from app.services import bee_gate
from app.services.bee_gate import BeeBusyError, bee_call_gate
from app.x402.dependency import require_x402_payment
from app.x402.middleware import X402Middleware
from app.x402.settlement import OUTCOME_UNKNOWN_HEADERS, settle_payment
from tests.test_paid_purchase_lost_response import BODY, BATCH, pay
from tests.test_paid_purchase_lost_response import env as purchase_env  # noqa: F401  (fixture)
from tests.test_x402_integration import create_valid_payment_header
from tests.test_x402_settlement_integrity import env  # noqa: F401  (fixture)

CHAIN = {"currentPrice": "24000", "minimumValidityBlocks": 17280, "block": 180000}


def refused_by_the_gate() -> BeeBusyError:
    """What the gate raises for a request's Bee call that got no slot (sets the request's flag)."""
    return bee_call_gate._refuse("user", 0.0)


async def swallow_a_refusal():
    """A Bee call refused by the gate, caught by the handler, as get_chainstate's
    caller does before a paid purchase."""
    try:
        raise refused_by_the_gate()
    except httpx.HTTPError:
        pass


def assert_not_rewritten(r):
    body = r.json()
    assert body.get("code") != "BEE_BUSY"
    if isinstance(body.get("detail"), dict):
        assert body["detail"].get("code") != "BEE_BUSY"


# --------------------------------------------------------------------------
# Through the real paid stamp purchase
# --------------------------------------------------------------------------

def _with_gate(app):
    app.add_middleware(BeeGateMiddleware)  # outside X402Middleware, as in app.main
    return app


def _chainstate_refused_once_before_settle():
    """get_chainstate answers for the price, then is refused by the gate when the
    paid purchase reads the start block (a call it swallows), then answers."""
    calls = {"n": 0}

    async def chainstate():
        calls["n"] += 1
        if calls["n"] == 2:
            raise refused_by_the_gate()
        return dict(CHAIN)
    return chainstate


def test_purchase_outcome_unknown_502_keeps_its_headers(purchase_env):
    app, fac, buy, find = purchase_env
    fac.settle = AsyncMock(side_effect=RuntimeError("facilitator down"))
    with patch("app.services.swarm_api.get_chainstate", new=_chainstate_refused_once_before_settle()):
        r = TestClient(_with_gate(app), raise_server_exceptions=False).post(
            "/api/v1/stamps/", json=BODY, headers={"X-PAYMENT": pay()})
    assert r.status_code == 502
    for name, value in OUTCOME_UNKNOWN_HEADERS.items():
        assert r.headers[name] == value
    assert r.json()["detail"]["code"] == "PAYMENT_SETTLEMENT_UNAVAILABLE"
    assert "retry-after" not in r.headers
    buy.assert_not_called()


def test_purchase_refused_after_settlement_goes_to_the_refund_path(purchase_env):
    """A committed-lane call refused after its wait never reached Bee: not an
    unknown outcome, so no label search and no 202 pending."""
    app, fac, buy, find = purchase_env
    buy.side_effect = BeeBusyError("no Bee call slot free within 30 s (committed)")
    r = TestClient(_with_gate(app), raise_server_exceptions=False).post(
        "/api/v1/stamps/", json=BODY, headers={"X-PAYMENT": pay()})
    assert r.status_code == 502
    assert r.headers.get("X-Payment-Status") == "settled_not_delivered"
    find.assert_not_called()
    assert fac.settle.await_count == 1


def test_slow_purchase_refused_after_settlement_is_refunded_not_searched(purchase_env, monkeypatch):
    from app.core.config import settings
    from tests.test_paid_purchase_lost_response import _audit, _run_pending
    app, fac, buy, find = purchase_env
    monkeypatch.setattr(settings, "SWARM_STAMP_PURCHASE_TIMEOUT_SECONDS", 0.05)

    async def busy(**kw):
        await asyncio.sleep(0.2)
        raise BeeBusyError("no Bee call slot free within 30 s (committed)")
    buy.side_effect = busy
    first, retry = _run_pending(app, None, key="k-busy")
    assert first.status_code == 202
    assert "not sent to Bee" in _audit("payment_failed")[0]["data"]["reason"]
    assert retry.json()["code"] == "DELIVERY_FAILED_AFTER_PAYMENT"
    find.assert_not_called()


def test_refusal_counts_as_certainly_unspent():
    from app.api.endpoints.stamps import _outcome_unknown
    from app.services.spend_budget import spend_certainly_did_not_happen
    e = BeeBusyError("busy")
    assert spend_certainly_did_not_happen(e) is True
    assert _outcome_unknown(e) is False
    # Unchanged for a real transport failure after the request was sent.
    assert _outcome_unknown(httpx.ReadTimeout("slow")) is True
    assert spend_certainly_did_not_happen(httpx.ReadTimeout("slow")) is False


# --------------------------------------------------------------------------
# Through the x402 settlement paths (settle_payment and the middleware fallback)
# --------------------------------------------------------------------------

async def refuse_then_settle(request: Request):
    await swallow_a_refusal()
    await settle_payment(request)
    return {"batchID": "b" * 64}


async def refuse_then_no_settle_point():
    await swallow_a_refusal()
    return {"ok": True}


def _x402_app(fac):
    app = FastAPI()
    router = APIRouter(dependencies=[Depends(require_x402_payment)])
    router.add_api_route("/api/v1/stamps/", refuse_then_settle, methods=["POST"])
    router.add_api_route("/api/v1/data/manifest", refuse_then_no_settle_point, methods=["POST"])
    app.include_router(router)
    app.add_middleware(X402Middleware, facilitator_client=fac)
    app.add_middleware(BeeGateMiddleware)
    return app


def _fac(*settles):
    f = MagicMock()
    f.verify = AsyncMock(return_value=VerifyResponse(isValid=True, payer="0xa1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1"))
    f.settle = AsyncMock(side_effect=list(settles))
    return f


def _post(fac, path, header=None):
    with patch("app.x402.dependency._get_facilitator_client", return_value=fac):
        client = TestClient(_x402_app(fac), raise_server_exceptions=False)
        return client.post(path, headers={"X-PAYMENT": header or create_valid_payment_header()})


def _assert_unknown_502(r):
    assert r.status_code == 502
    for name, value in OUTCOME_UNKNOWN_HEADERS.items():
        assert r.headers[name] == value
    assert "retry-after" not in r.headers
    assert_not_rewritten(r)


def test_settle_payment_outcome_unknown_502_is_kept(env):
    _assert_unknown_502(_post(_fac(RuntimeError("facilitator down")), "/api/v1/stamps/"))


def test_fallback_settle_outcome_unknown_502_is_kept(env):
    r = _post(_fac(RuntimeError("facilitator down")), "/api/v1/data/manifest")
    _assert_unknown_502(r)
    assert r.json()["code"] == "PAYMENT_SETTLEMENT_UNKNOWN"


def test_outcome_unknown_refusal_409_is_kept(env):
    """Retry of an authorization whose first settlement gave no answer; the
    facilitator then refuses it. The 409 tells the client not to pay again."""
    from tests.test_x402_settlement_integrity import _assert_outcome_unknown, refused
    fac = _fac(RuntimeError("facilitator down"), refused())
    header = create_valid_payment_header()
    _assert_unknown_502(_post(fac, "/api/v1/stamps/", header))
    r = _post(fac, "/api/v1/stamps/", header)
    _assert_outcome_unknown(r)
    assert "retry-after" not in r.headers


def test_fallback_retry_409_is_kept(env):
    from tests.test_x402_settlement_integrity import _assert_outcome_unknown, ok
    fac = _fac(RuntimeError("facilitator down"), ok())
    header = create_valid_payment_header()
    _assert_unknown_502(_post(fac, "/api/v1/data/manifest", header))
    _assert_outcome_unknown(_post(fac, "/api/v1/data/manifest", header))


def test_settlement_failure_402_is_kept(env):
    from tests.test_x402_settlement_integrity import refused
    r = _post(_fac(refused()), "/api/v1/stamps/")
    assert r.status_code == 402
    assert r.json()["detail"]["code"] == "PAYMENT_SETTLEMENT_FAILED"


# --------------------------------------------------------------------------
# Each protection on its own, with the gate's small app (no x402 middleware)
# --------------------------------------------------------------------------

def _plain_app():
    app = FastAPI()

    @app.get("/attempted-then-502")
    async def attempted():
        await swallow_a_refusal()
        bee_gate.mark_payment_attempted()
        raise HTTPException(status_code=502, detail="facilitator gave no answer")

    @app.get("/payment-header-502")
    async def payment_header():
        await swallow_a_refusal()
        raise HTTPException(status_code=502, detail="x", headers=dict(OUTCOME_UNKNOWN_HEADERS))

    @app.get("/settled-not-delivered-502")
    async def settled_header():
        await swallow_a_refusal()
        raise HTTPException(status_code=502, detail="x",
                            headers={"X-Payment-Transaction": "0x" + "ab" * 32})

    @app.get("/payment-code-500")
    async def payment_code():
        await swallow_a_refusal()
        raise HTTPException(status_code=500, detail={"code": "DELIVERY_FAILED_AFTER_PAYMENT",
                                                     "message": "refund", "transaction": "0x1"})

    @app.get("/x402-fields-409")
    async def x402_fields():
        await swallow_a_refusal()
        raise HTTPException(status_code=409, detail={"code": "SOMETHING", "x402_status": "settlement_unknown"})

    @app.get("/plain-422")
    async def plain():
        await swallow_a_refusal()
        raise HTTPException(status_code=422, detail="bad input")

    @app.get("/plain-502")
    async def plain_502():
        await swallow_a_refusal()
        raise HTTPException(status_code=502, detail="Bee node request failed")

    app.add_middleware(BeeGateMiddleware)
    return app


@pytest.fixture
def plain():
    return TestClient(_plain_app(), raise_server_exceptions=False)


@pytest.mark.parametrize("path, status", [
    ("/attempted-then-502", 502),          # (a) settlement started
    ("/payment-header-502", 502),          # (b) X-Payment-Status
    ("/settled-not-delivered-502", 502),   # (b) X-Payment-Transaction
    ("/payment-code-500", 500),            # (b) a payment/delivery code in the body
    ("/x402-fields-409", 409),             # (b) x402 fields in the body
])
def test_payment_answers_are_never_rewritten(plain, path, status):
    r = plain.get(path)
    assert r.status_code == status
    assert "retry-after" not in r.headers
    assert_not_rewritten(r)


def test_a_request_carrying_a_payment_is_never_rewritten(plain):
    r = plain.get("/plain-422", headers={"X-PAYMENT": "e30="})
    assert r.status_code == 422 and r.json()["detail"] == "bad input"


@pytest.mark.parametrize("path", ["/plain-422", "/plain-502"])
def test_a_plain_error_with_no_payment_is_still_rewritten(plain, path):
    r = plain.get(path)
    assert r.status_code == 503
    assert r.json()["code"] == "BEE_BUSY"
    assert r.headers["retry-after"]


# --------------------------------------------------------------------------
# Work started by a request but run outside it is background work
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pool_maintenance_after_the_response_runs_as_background(monkeypatch):
    from app.api.endpoints import pool
    from app.services.stamp_pool import stamp_pool_manager
    seen = []

    async def check():
        seen.append(bee_gate.current_request_state())
        return {}
    monkeypatch.setattr(stamp_pool_manager, "check_and_replenish", check)

    async def as_request():
        bee_gate.begin_request()
        await pool._check_and_replenish_in_background()
    await asyncio.get_running_loop().create_task(as_request())
    assert seen == [None]


@pytest.mark.asyncio
async def test_pool_replenish_task_runs_as_background(monkeypatch):
    from app.services import stamp_pool
    manager = stamp_pool.stamp_pool_manager
    seen = []

    async def purchase(depth):
        seen.append(bee_gate.current_request_state())
        return "f" * 64
    monkeypatch.setattr(manager, "_purchase_stamp", purchase)
    monkeypatch.setattr(manager, "_immediate_replenish_paused", lambda depth: False)
    monkeypatch.setattr(manager, "_end_pending_locked", lambda depth: None)

    async def as_request():
        bee_gate.begin_request()
        # As the pool acquire does: a task of its own, started from the request.
        await asyncio.get_running_loop().create_task(manager._async_replenish_one(17))
    await asyncio.get_running_loop().create_task(as_request())
    assert seen == [None]


@pytest.mark.asyncio
async def test_client_recovery_runs_as_background(monkeypatch):
    from app.services import http_client as hc
    seen = []

    async def probe():
        seen.append(bee_gate.current_request_state())
        return False
    monkeypatch.setattr(hc, "probe_bee", probe)
    monkeypatch.setattr(hc, "pool_snapshot", lambda t: None)
    saved = {k: getattr(hc, k) for k in ("_client", "_transport", "_replace_lock", "_last_recovery_attempt")}
    await hc.init_client()
    try:
        async def as_request():
            bee_gate.begin_request()
            await asyncio.get_running_loop().create_task(hc.recover_pool(hc._transport.generation))
        await asyncio.get_running_loop().create_task(as_request())
    finally:
        await hc.close_client()
        for k, v in saved.items():
            setattr(hc, k, v)
    assert seen == [None]
