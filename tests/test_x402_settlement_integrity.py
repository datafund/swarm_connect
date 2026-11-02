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
    f.verify = AsyncMock(return_value=VerifyResponse(isValid=True, payer="0xa1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1"))
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
    with patch("app.x402.dependency._calculate_price_for_request",
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


def test_fallback_settlement_error_is_not_reported_as_a_refusal(env):
    """#553: the work ran, then the facilitator gave no answer. Not a 402: an x402
    client would sign and pay again, and the first transfer may have gone through."""
    fac = _facilitator(RuntimeError("facilitator down"))
    r = _run(fac, path="/api/v1/data/manifest")
    assert r.status_code == 502
    body = r.json()
    assert body["code"] == "PAYMENT_SETTLEMENT_UNKNOWN"
    assert body["x402_status"] == "settlement_unknown"
    assert body["nonce"] == "0x" + "00" * 32     # the authorization to check on-chain
    assert "ok" not in body                       # the unpaid result is withheld
    assert "accepts" not in body                  # nothing to sign: do not pay again blindly
    assert r.headers["X-Payment-Mode"] == "failed"
    assert r.headers["X-Payment-Status"] == "settlement_unknown"
    assert DELIVERED == [1]                       # the handler did run


def test_fallback_settlement_error_is_audited_and_counted_as_error(env):
    from app.services.metrics import x402_settlements_total
    from app.x402.audit import read_audit_log

    def count(result):
        return x402_settlements_total.labels(result=result)._value.get()

    error, refused_before = count("error"), count("refused")
    fac = _facilitator(RuntimeError("facilitator down"))
    assert _run(fac, path="/api/v1/data/manifest").status_code == 502
    assert count("error") == error + 1
    assert count("refused") == refused_before
    assert any(e["event_type"] == "payment_failed" and e["data"].get("stage") == "settle"
               for e in read_audit_log())


def _assert_outcome_unknown(r, nonce="0x" + "00" * 32):
    """The answer to a retry of an authorization whose settlement outcome is unknown:
    not a 402 and no `accepts`, so an x402 client does not sign and pay again."""
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "PAYMENT_AUTHORIZATION_OUTCOME_UNKNOWN"
    assert detail["x402_status"] == "settlement_unknown"
    assert detail["nonce"] == nonce
    assert "accepts" not in detail and "accepts" not in r.json()
    return detail


def test_fallback_settlement_error_answers_a_retry_without_a_402(env):
    """#553 review: the work ran and the nonce may be spent. A retry with the same
    authorization is not run again, and is not told to sign a new payment."""
    fac = _facilitator(RuntimeError("facilitator down"), ok())
    header = create_valid_payment_header()
    assert _run(fac, path="/api/v1/data/manifest", header=header).status_code == 502
    _assert_outcome_unknown(_run(fac, path="/api/v1/data/manifest", header=header))
    assert fac.settle.await_count == 1 and DELIVERED == [1]


def test_unavailable_then_refused_retry_is_not_a_402(env):
    """#553 review: after 502 PAYMENT_SETTLEMENT_UNAVAILABLE the documented retry is the
    same authorization. If the facilitator then refuses it (for instance because the
    first transfer went through and spent the nonce), the payer must not be sent to pay
    again from a 402."""
    fac = _facilitator(RuntimeError("facilitator down"), refused("invalid_transaction_state"))
    header = create_valid_payment_header()
    assert _run(fac, header=header).status_code == 502
    detail = _assert_outcome_unknown(_run(fac, header=header))
    assert detail["reason"] == "invalid_transaction_state"
    assert DELIVERED == []


def test_unavailable_then_verify_rejects_the_retry_is_not_a_402(env):
    """A facilitator may reject a spent nonce at verify rather than at settle."""
    fac = _facilitator(RuntimeError("facilitator down"))
    good = VerifyResponse(isValid=True, payer="0xa1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1")
    fac.verify = AsyncMock(side_effect=[good, VerifyResponse(isValid=False, invalidReason="nonce_used", payer=None)])
    header = create_valid_payment_header()
    assert _run(fac, header=header).status_code == 502
    detail = _assert_outcome_unknown(_run(fac, header=header))
    assert detail["reason"] == "nonce_used"
    assert DELIVERED == []


def test_unavailable_then_successful_retry_forgets_the_unknown_outcome(env):
    """A retry of the same authorization that settles proves the first did not; the
    authorization is then an ordinary settled one."""
    fac = _facilitator(RuntimeError("facilitator down"), ok())
    header = create_valid_payment_header()
    assert _run(fac, header=header).status_code == 502
    assert _run(fac, header=header).status_code == 200
    from app.x402.settlement import outcome_unknown
    assert not outcome_unknown.contains(("0x1234567890abcdef1234567890abcdef12345678", "0x" + "00" * 32))


def test_a_refusal_without_an_earlier_unknown_outcome_is_still_a_402(env):
    fac = _facilitator(refused())
    r = _run(fac)
    assert r.status_code == 402
    assert r.json()["detail"]["code"] == "PAYMENT_SETTLEMENT_FAILED"


def _assert_unknown_headers(r):
    assert r.headers["X-Payment-Mode"] == "failed"
    assert r.headers["X-Payment-Status"] == "settlement_unknown"


def test_outcome_unknown_409_carries_the_headers_from_every_layer(env):
    """#553 review 2: the same 409 from the dependency, settle_payment and the
    middleware carries the same payment headers."""
    header = create_valid_payment_header()
    # dependency, verify rejects
    fac = _facilitator(RuntimeError("down"))
    good = VerifyResponse(isValid=True, payer="0xa1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1")
    fac.verify = AsyncMock(side_effect=[good, VerifyResponse(isValid=False, invalidReason="x", payer=None)])
    assert _run(fac, header=header).status_code == 502
    _assert_unknown_headers(_run(fac, header=header))
    # settle_payment refuses
    from app.x402.settlement import outcome_unknown
    outcome_unknown.reset()
    fac = _facilitator(RuntimeError("down"), refused())
    assert _run(fac, header=header).status_code == 502
    _assert_unknown_headers(_run(fac, header=header))
    # dependency, fallback kind
    outcome_unknown.reset()
    fac = _facilitator(RuntimeError("down"))
    assert _run(fac, path="/api/v1/data/manifest", header=header).status_code == 502
    _assert_unknown_headers(_run(fac, path="/api/v1/data/manifest", header=header))


def test_fallback_unknown_never_reruns_the_work_after_the_replay_window(env):
    """#553 review 2: the reservation lapses after an hour, the registry entry does not.
    A remembered after-the-work authorization is stopped before any work runs."""
    from app.x402.settlement import replay_guard
    fac = _facilitator(RuntimeError("facilitator down"), refused("nonce_used"))
    header = create_valid_payment_header()
    assert _run(fac, path="/api/v1/data/manifest", header=header).status_code == 502
    replay_guard.reset()   # the replay window has passed
    _assert_outcome_unknown(_run(fac, path="/api/v1/data/manifest", header=header))
    assert DELIVERED == [1] and fac.settle.await_count == 1


def test_unavailable_same_authorization_retry_is_still_allowed(env):
    """Where the work has not run, the same-X-PAYMENT retry stays the recovery."""
    from app.x402.settlement import replay_guard
    fac = _facilitator(RuntimeError("facilitator down"), ok())
    header = create_valid_payment_header()
    assert _run(fac, header=header).status_code == 502
    replay_guard.reset()
    assert _run(fac, header=header).status_code == 200
    assert DELIVERED == [1]


def test_middleware_refusal_of_a_remembered_authorization_is_a_409(env):
    """A before-the-work unknown outcome, then the same authorization on a route that
    settles after its work, refused by the facilitator: the middleware's own 409."""
    fac = _facilitator(RuntimeError("facilitator down"), refused("nonce_used"))
    header = create_valid_payment_header()
    assert _run(fac, header=header).status_code == 502
    r = _run(fac, path="/api/v1/data/manifest", header=header)
    detail = _assert_outcome_unknown(r)
    assert detail["reason"] == "nonce_used"
    _assert_unknown_headers(r)
    assert "ok" not in r.text


def test_outcome_unknown_registry_expires_and_is_capped(monkeypatch, tmp_path):
    # Wall-clock expiry since the registry is persisted (#553 review 3).
    import app.x402.settlement as st
    reg = st.OutcomeUnknownRegistry(state_file=str(tmp_path / "s.json"))
    now = [1000.0]
    monkeypatch.setattr(st.time, "time", lambda: now[0])
    reg.add(("0xa", "0x1"), st.BEFORE_WORK)
    assert reg.kind(("0xa", "0x1")) == st.BEFORE_WORK
    now[0] += st.OUTCOME_UNKNOWN_SECONDS + 1
    assert not reg.contains(("0xa", "0x1"))
    monkeypatch.setattr(st, "OUTCOME_UNKNOWN_MAX_ENTRIES", 2)
    for i in range(3):
        now[0] += 1
        reg.add(("0xa", f"0x{i}"), st.BEFORE_WORK)
    assert not reg.contains(("0xa", "0x0"))          # oldest evicted
    assert reg.contains(("0xa", "0x1")) and reg.contains(("0xa", "0x2"))
    # after-the-work wins over before-the-work for the same authorization
    reg.add(("0xa", "0x2"), st.AFTER_WORK)
    reg.add(("0xa", "0x2"), st.BEFORE_WORK)
    assert reg.kind(("0xa", "0x2")) == st.AFTER_WORK


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
    assert any(TX in e["data"]["reason"] for e in events)


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

