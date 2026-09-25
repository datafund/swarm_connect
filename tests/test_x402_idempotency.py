"""Idempotency-Key on paid requests: a retry is answered, not charged again (#359).

Mounts a paid route with the real x402 dependency, settlement and middleware,
and a facilitator mock that counts settlements.
"""
import asyncio
import base64
import json
import secrets
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI, File, Request, UploadFile
from fastapi.testclient import TestClient
from x402.types import SettleResponse, VerifyResponse

from app.core.config import settings
from app.x402.dependency import require_x402_payment
from app.x402.idempotency import IdempotencyStore, entry_id, idempotency_store
from app.x402.middleware import X402Middleware
from app.x402.ratelimit import reset_rate_limiter
from app.x402.settlement import settle_payment

ALICE = "0x" + "a1" * 20
MALLORY = "0x" + "b2" * 20


def pay(payer=ALICE):
    """A payment header with a fresh nonce, as a retrying client would send."""
    payload = {"x402Version": 1, "scheme": "exact", "network": "base-sepolia", "payload": {
        "signature": "0x" + "ab" * 65,
        "authorization": {"from": payer, "to": "0xpayee", "value": "100000", "validAfter": "0",
                          "validBefore": "9999999999", "nonce": "0x" + secrets.token_hex(32)}}}
    return base64.b64encode(json.dumps(payload).encode()).decode()


@pytest.fixture
def fac(monkeypatch):
    monkeypatch.setattr(settings, "X402_ENABLED", True)
    monkeypatch.setattr(settings, "X402_PAY_TO_ADDRESS", "0xpayee")
    monkeypatch.setattr(settings, "X402_NETWORK", "base-sepolia")
    reset_rate_limiter()
    f = MagicMock()
    f.verify = AsyncMock(return_value=VerifyResponse(isValid=True, payer=ALICE))
    f.settle = AsyncMock(side_effect=lambda **kw: SettleResponse(
        success=True, transaction="0x" + secrets.token_hex(32)))
    with patch("app.x402.dependency._get_facilitator_client", return_value=f), \
         patch("app.x402.dependency._calculate_price_for_request",
               new=AsyncMock(return_value={"price_usd": 0.02, "description": "t"})):
        yield f
    reset_rate_limiter()


def _app(fac, gate=None, status=201):
    """POST /api/v1/stamps/ settles, then 'buys' a new batch id each time."""
    router = APIRouter()
    bought = []

    @router.post("/", status_code=status)
    async def buy(request: Request, body: dict):
        await settle_payment(request)
        if gate is not None:
            await gate.wait()
        bought.append(secrets.token_hex(32))
        return {"batchID": bought[-1]}

    @router.post("/upload")
    async def upload(request: Request, file: UploadFile = File(...)):
        await settle_payment(request)
        bought.append(await file.read())
        return {"reference": secrets.token_hex(32)}

    app = FastAPI()
    app.include_router(router, prefix="/api/v1/stamps", dependencies=[Depends(require_x402_payment)])
    app.add_middleware(X402Middleware, facilitator_client=fac)
    return app, bought


def _post(client, payer=ALICE, key="k1", body=None, path="/api/v1/stamps/"):
    headers = {"X-PAYMENT": pay(payer)}
    if key is not None:
        headers["Idempotency-Key"] = key
    return client.post(path, json=body if body is not None else {"depth": 17}, headers=headers)


def test_retry_with_same_key_returns_first_result_without_settling(fac):
    app, bought = _app(fac)
    c = TestClient(app)
    first = _post(c)
    second = _post(c)
    assert first.status_code == second.status_code == 201
    assert second.json() == first.json()
    assert len(bought) == 1
    assert fac.settle.await_count == 1
    # The new payment was verified (that is what proves the payer), never settled.
    assert fac.verify.await_count == 2
    assert second.headers["Idempotent-Replayed"] == "true"
    assert second.headers["X-Payment-Transaction"] == first.headers["X-Payment-Transaction"]
    assert "Idempotent-Replayed" not in first.headers


def test_without_key_each_request_is_paid(fac):
    app, bought = _app(fac)
    c = TestClient(app)
    assert _post(c, key=None).json() != _post(c, key=None).json()
    assert fac.settle.await_count == 2


def test_other_payer_with_same_key_gets_its_own_result(fac):
    app, bought = _app(fac)
    c = TestClient(app)
    a = _post(c, payer=ALICE)
    m = _post(c, payer=MALLORY)
    assert m.json() != a.json()
    assert "Idempotent-Replayed" not in m.headers
    assert fac.settle.await_count == 2


def test_unverified_payment_never_sees_stored_result(fac):
    """Claiming a payer's address is not enough: the signature must verify."""
    app, _ = _app(fac)
    c = TestClient(app)
    _post(c)
    fac.verify.return_value = VerifyResponse(isValid=False, invalidReason="invalid_signature", payer=None)
    r = _post(c)
    assert r.status_code == 402
    assert "batchID" not in r.text
    assert fac.settle.await_count == 1


def test_same_key_different_body_is_refused(fac):
    app, bought = _app(fac)
    c = TestClient(app)
    _post(c, body={"depth": 17})
    r = _post(c, body={"depth": 20})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert fac.settle.await_count == 1 and len(bought) == 1


def test_same_key_different_query_is_refused(fac):
    app, _ = _app(fac)
    c = TestClient(app)
    _post(c, path="/api/v1/stamps/?x=1")
    assert _post(c, path="/api/v1/stamps/?x=2").status_code == 422


def test_multipart_upload_retry_and_changed_file(fac):
    app, bought = _app(fac)
    c = TestClient(app)

    def up(content):
        return c.post("/api/v1/stamps/upload", files={"file": ("a.json", content, "application/json")},
                      headers={"X-PAYMENT": pay(), "Idempotency-Key": "u1"})

    first = up(b'{"a":1}')
    again = up(b'{"a":1}')
    assert again.json() == first.json()
    # The handler still read the whole file after the key was hashed.
    assert bought == [b'{"a":1}']
    assert up(b'{"a":2}').status_code == 422
    assert fac.settle.await_count == 1


def test_failure_is_not_cached(fac):
    app, bought = _app(fac, status=201)
    c = TestClient(app)
    fac.settle.side_effect = None
    fac.settle.return_value = SettleResponse(success=False, errorReason="insufficient_funds")
    assert _post(c).status_code == 402
    fac.settle.side_effect = lambda **kw: SettleResponse(success=True, transaction="0x" + "cd" * 32)
    r = _post(c)
    assert r.status_code == 201 and "Idempotent-Replayed" not in r.headers
    assert len(bought) == 1


def test_retry_while_first_is_running_gets_409_and_is_not_charged(fac):
    gate = asyncio.Event()
    app, bought = _app(fac, gate=gate)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            h = lambda: {"X-PAYMENT": pay(), "Idempotency-Key": "k1"}
            first = asyncio.create_task(c.post("/api/v1/stamps/", json={"depth": 17}, headers=h()))
            while fac.settle.await_count == 0:
                await asyncio.sleep(0.01)
            second = await c.post("/api/v1/stamps/", json={"depth": 17}, headers=h())
            gate.set()
            first = await first
            third = await c.post("/api/v1/stamps/", json={"depth": 17}, headers=h())
            return first, second, third

    first, second, third = asyncio.run(run())
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "IDEMPOTENCY_KEY_IN_PROGRESS"
    assert first.status_code == 201
    assert third.json() == first.json()
    assert fac.settle.await_count == 1 and len(bought) == 1


def test_invalid_key_is_refused_unpaid(fac):
    app, bought = _app(fac)
    r = _post(TestClient(app), key="x" * 256)
    assert r.status_code == 400
    assert fac.settle.await_count == 0 and not bought


def test_free_tier_ignores_key(fac, monkeypatch):
    monkeypatch.setattr(settings, "X402_FREE_TIER_ENABLED", True)
    app, bought = _app(fac)
    c = TestClient(app)
    h = {"X-Payment-Mode": "free", "Idempotency-Key": "k1"}
    a = c.post("/api/v1/stamps/", json={"depth": 17}, headers=h)
    b = c.post("/api/v1/stamps/", json={"depth": 17}, headers=h)
    assert a.json() != b.json()


def test_stored_results_survive_restart_and_expire(tmp_path, monkeypatch):
    path = str(tmp_path / "idem.json")
    s = IdempotencyStore(state_file=path)
    eid = entry_id(ALICE, "POST", "/api/v1/stamps/", "k1")
    assert s.begin(eid, "h") == ("new", None)
    from starlette.responses import JSONResponse
    s.complete(eid, JSONResponse({"batchID": "b"}, status_code=201))
    state, entry = IdempotencyStore(state_file=path).begin(eid, "h")
    assert state == "replay" and entry["status"] == 201

    import app.x402.idempotency as idem
    real = idem.time.time
    monkeypatch.setattr(idem.time, "time", lambda: real() + idem.TTL_SECONDS + 1)
    assert IdempotencyStore(state_file=path).begin(eid, "h") == ("new", None)


def test_payer_match_is_case_insensitive():
    assert entry_id(ALICE.upper(), "post", "/p/", "k") == entry_id(ALICE, "POST", "/p", "k")
