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
from app.x402.idempotency import IdempotencyStore, auth_hash, entry_id
from app.x402.middleware import X402Middleware
from app.x402.ratelimit import reset_rate_limiter
from app.x402.settlement import replay_guard, settle_payment

ALICE = "0x" + "a1" * 20
MALLORY = "0x" + "b2" * 20


def pay(payer=ALICE, nonce=None):
    """A payment header with a fresh nonce, as a retrying client would send."""
    payload = {"x402Version": 1, "scheme": "exact", "network": "base-sepolia", "payload": {
        "signature": "0x" + "ab" * 65,
        "authorization": {"from": payer, "to": "0xpayee", "value": "100000", "validAfter": "0",
                          "validBefore": "9999999999", "nonce": nonce or "0x" + secrets.token_hex(32)}}}
    return base64.b64encode(json.dumps(payload).encode()).decode()


@pytest.fixture
def fac(monkeypatch):
    monkeypatch.setattr(settings, "X402_ENABLED", True)
    monkeypatch.setattr(settings, "X402_PAY_TO_ADDRESS", "0xpayee")
    monkeypatch.setattr(settings, "X402_NETWORK", "base-sepolia")
    reset_rate_limiter()
    f = MagicMock()
    # Reports the signer as the payer, as a real facilitator does.
    f.verify = AsyncMock(side_effect=lambda payment, **kw: VerifyResponse(
        isValid=True, payer=payment.payload.authorization.from_))
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
        if body.get("big"):
            return {"batchID": bought[-1] if bought else "x", "pad": "x" * 70000}
        if body.get("fail") == "raise":
            raise RuntimeError("bee went away after payment")
        if body.get("fail") == "500":
            from fastapi import HTTPException
            raise HTTPException(status_code=500, detail="bee refused")
        bought.append(secrets.token_hex(32))
        return {"batchID": bought[-1]}

    @router.post("/chunks/credit")
    async def credit(request: Request):
        from app.services.bandwidth_credit import bandwidth_credit_manager
        await settle_payment(request)
        bought.append("credit")
        return {"address": request.state.x402_payer,
                "token": bandwidth_credit_manager.issue_token(request.state.x402_payer)}

    @router.post("/upload")
    async def upload(request: Request, file: UploadFile = File(...)):
        await settle_payment(request)
        bought.append(await file.read())
        return {"reference": secrets.token_hex(32)}

    app = FastAPI()
    app.include_router(router, prefix="/api/v1/stamps", dependencies=[Depends(require_x402_payment)])
    app.include_router(router, prefix="/api/v1", dependencies=[Depends(require_x402_payment)])
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
    fac.verify.side_effect = None
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


@pytest.mark.parametrize("fail", ["raise", "500"])
def test_key_stays_taken_once_paid_even_if_the_request_fails(fac, fail):
    """Paid but not delivered: a retry is told so, and is not charged again."""
    app, bought = _app(fac)
    c = TestClient(app, raise_server_exceptions=False)
    first = _post(c, body={"depth": 17, "fail": fail})
    assert first.status_code == 500
    retry = _post(c, body={"depth": 17, "fail": fail})
    assert retry.status_code == 409
    detail = retry.json()["detail"]
    assert detail["code"] == "IDEMPOTENCY_KEY_SETTLED_PENDING"
    assert detail["transaction"] == first.headers["X-Payment-Transaction"]
    assert fac.settle.await_count == 1


def test_settled_entry_survives_a_restart_mid_request(fac, tmp_path, monkeypatch):
    """The in-flight marker is lost on restart; the settlement record is not."""
    path = str(tmp_path / "idem.json")
    s = IdempotencyStore(state_file=path)
    eid = entry_id(ALICE, "POST", "/api/v1/stamps/", "k1")
    state, _, token = s.begin(eid, "h", auth_hash((ALICE, "0x01")))
    s.mark_settled(eid, token, auth_hash((ALICE, "0x01")), "0xtx")
    # Process dies here: no complete(), no abandon().
    state, entry, _ = IdempotencyStore(state_file=path).begin(eid, "h", auth_hash((ALICE, "0x02")))
    assert state == "settled" and entry["transaction"] == "0xtx"


def test_unsettled_failure_frees_the_key(fac):
    app, bought = _app(fac)
    c = TestClient(app)
    fac.settle.side_effect = None
    fac.settle.return_value = SettleResponse(success=False, errorReason="insufficient_funds")
    assert _post(c).status_code == 402
    fac.settle.side_effect = lambda **kw: SettleResponse(success=True, transaction="0x" + "cd" * 32)
    assert _post(c).status_code == 201


@pytest.mark.parametrize("forget_guard", [False, True])
def test_original_authorization_cannot_fetch_the_stored_result(fac, forget_guard):
    """Only a new, unused authorization gets a replay; the one that paid is spent."""
    app, bought = _app(fac)
    c = TestClient(app)
    original = pay()
    headers = {"X-PAYMENT": original, "Idempotency-Key": "k1"}
    assert c.post("/api/v1/stamps/", json={"depth": 17}, headers=headers).status_code == 201
    if forget_guard:
        # An hour later, or after a restart: the replay guard no longer has it.
        replay_guard.reset()
    r = c.post("/api/v1/stamps/", json={"depth": 17}, headers=headers)
    assert r.status_code == 402
    assert "batchID" not in r.text
    assert fac.settle.await_count == 1


def test_replay_releases_the_new_authorization_and_is_audited(fac):
    from app.x402.audit import AuditEventType, read_audit_log
    app, _ = _app(fac)
    c = TestClient(app)
    first = _post(c)
    nonce = "0x" + "77" * 32
    headers = {"X-PAYMENT": pay(nonce=nonce), "Idempotency-Key": "k1"}
    assert c.post("/api/v1/stamps/", json={"depth": 17}, headers=headers).json() == first.json()
    # Not settled, so not spent: the guard does not hold it.
    assert replay_guard.reserve((ALICE, nonce))
    events = read_audit_log(max_entries=1, event_type=AuditEventType.PAYMENT_IDEMPOTENT_REPLAY)
    assert events and events[0]["wallet_address"] == ALICE
    assert events[0]["data"]["transaction_hash"] == first.headers["X-Payment-Transaction"]


def test_facilitator_payer_must_match_signer_for_keyed_requests(fac):
    app, _ = _app(fac)
    fac.verify.side_effect = None
    fac.verify.return_value = VerifyResponse(isValid=True, payer=MALLORY)
    r = _post(TestClient(app))
    assert r.status_code == 402 and fac.settle.await_count == 0


def test_unreadable_state_fails_closed_without_overwriting(fac, tmp_path, monkeypatch):
    import app.x402.idempotency as idem
    path = tmp_path / "idem.json"
    path.write_text("{not json")
    store = IdempotencyStore(state_file=str(path))
    assert store.unavailable
    assert list(tmp_path.glob("idem.json.corrupt-*"))
    # A second start does not pile up copies.
    IdempotencyStore(state_file=str(path))
    assert len(list(tmp_path.glob("idem.json.corrupt-*"))) == 1
    monkeypatch.setattr(idem, "idempotency_store", store)
    app, bought = _app(fac)
    c = TestClient(app)
    r = _post(c)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "IDEMPOTENCY_UNAVAILABLE"
    assert fac.settle.await_count == 0
    # Requests without a key are unaffected.
    assert _post(c, key=None).status_code == 201
    assert path.read_text() == "{not json"


def test_stale_request_cannot_overwrite_a_newer_claim(tmp_path):
    s = IdempotencyStore(state_file=str(tmp_path / "idem.json"))
    eid = entry_id(ALICE, "POST", "/p", "k")
    _, _, old = s.begin(eid, "h", "a1")
    s._pending[eid] = ("h", 0, old)          # marker expired while the request ran
    state, _, new = s.begin(eid, "h", "a2")
    assert state == "new"
    from starlette.responses import JSONResponse
    s.complete(eid, old, JSONResponse({"stale": True}))
    s.abandon(eid, old)
    assert s.begin(eid, "h", "a3")[0] == "in_progress"


def test_oversize_result_is_not_stored_and_is_logged(tmp_path, caplog):
    from starlette.responses import Response as R
    s = IdempotencyStore(state_file=str(tmp_path / "idem.json"))
    eid = entry_id(ALICE, "POST", "/p", "k")
    _, _, token = s.begin(eid, "h", "a1")
    s.complete(eid, token, R(b"x" * (64 * 1024 + 1)))
    assert "not stored" in caplog.text
    assert s.begin(eid, "h", "a2")[0] == "new"


def test_entry_cap_evicts_completed_before_settled(tmp_path, monkeypatch):
    from starlette.responses import JSONResponse
    monkeypatch.setattr(settings, "X402_IDEMPOTENCY_MAX_ENTRIES", 2)
    s = IdempotencyStore(state_file=str(tmp_path / "idem.json"))
    ids = [entry_id(ALICE, "POST", "/p", str(i)) for i in range(3)]
    _, _, t0 = s.begin(ids[0], "h", "a0")
    s.mark_settled(ids[0], t0, "a0", "0xtx")          # oldest, but paid and pending
    for eid in ids[1:]:
        _, _, t = s.begin(eid, "h", "a")
        s.complete(eid, t, JSONResponse({}))
    assert set(s._entries) == {ids[0], ids[2]}


def test_stored_results_survive_restart_and_expire(tmp_path, monkeypatch):
    from starlette.responses import JSONResponse
    path = str(tmp_path / "idem.json")
    s = IdempotencyStore(state_file=path)
    eid = entry_id(ALICE, "POST", "/api/v1/stamps/", "k1")
    state, _, token = s.begin(eid, "h", "a1")
    assert state == "new"
    s.complete(eid, token, JSONResponse({"batchID": "b"}, status_code=201))
    state, entry, _ = IdempotencyStore(state_file=path).begin(eid, "h", "a2")
    assert state == "done" and entry["status"] == 201

    import app.x402.idempotency as idem
    real = idem.time.time
    monkeypatch.setattr(idem.time, "time", lambda: real() + idem.TTL_SECONDS + 1)
    assert IdempotencyStore(state_file=path).begin(eid, "h", "a2")[0] == "new"


def test_payer_match_is_case_insensitive():
    assert entry_id(ALICE.upper(), "post", "/p/", "k") == entry_id(ALICE, "POST", "/p", "k")


def test_unknown_settlement_outcome_keeps_the_key(fac):
    """The facilitator may have moved the money: a retry must not pay again."""
    app, bought = _app(fac)
    c = TestClient(app)
    fac.settle.side_effect = httpx.ReadTimeout("facilitator slow")
    first_nonce = "0x" + "5e" * 32
    r = c.post("/api/v1/stamps/", json={"depth": 17},
               headers={"X-PAYMENT": pay(nonce=first_nonce), "Idempotency-Key": "k1"})
    assert r.status_code == 502
    fac.settle.side_effect = lambda **kw: SettleResponse(success=True, transaction="0x" + "cd" * 32)
    retry = _post(c)
    assert retry.status_code == 409
    detail = retry.json()["detail"]
    assert detail["code"] == "IDEMPOTENCY_KEY_SETTLEMENT_UNKNOWN"
    assert detail["nonce"] == first_nonce
    assert fac.settle.await_count == 1 and not bought


def test_settling_entry_survives_a_restart(tmp_path):
    """Killed or cancelled while waiting for the facilitator: still unknown after a restart."""
    path = str(tmp_path / "idem.json")
    s = IdempotencyStore(state_file=path)
    eid = entry_id(ALICE, "POST", "/p", "k")
    _, _, token = s.begin(eid, "h", "a1")
    s.mark_settling(eid, token, "a1", "0x" + "01" * 32)
    state, entry, _ = IdempotencyStore(state_file=path).begin(eid, "h", "a2")
    assert state == "settling" and entry["nonce"] == "0x" + "01" * 32


def test_definite_refusal_clears_the_settling_entry(tmp_path):
    s = IdempotencyStore(state_file=str(tmp_path / "idem.json"))
    eid = entry_id(ALICE, "POST", "/p", "k")
    _, _, token = s.begin(eid, "h", "a1")
    s.mark_settling(eid, token, "a1", "n")
    s.clear_settling(eid, token)
    s.abandon(eid, token)
    assert s.begin(eid, "h", "a2")[0] == "new"


def test_oversize_result_tells_the_retry_it_was_delivered(fac):
    app, bought = _app(fac)
    c = TestClient(app)
    first = _post(c, body={"depth": 17, "big": True})
    assert first.status_code == 201
    retry = _post(c, body={"depth": 17, "big": True})
    assert retry.status_code == 409
    assert retry.json()["detail"]["code"] == "IDEMPOTENCY_KEY_DELIVERED_NOT_STORED"
    assert retry.json()["detail"]["x402_status"] == "delivered"
    assert fac.settle.await_count == 1


def test_credit_token_is_not_stored_and_replay_gets_the_current_one(fac, tmp_path, monkeypatch):
    import app.x402.idempotency as idem
    from app.services.bandwidth_credit import bandwidth_credit_manager
    store = IdempotencyStore(state_file=str(tmp_path / "idem.json"))
    monkeypatch.setattr(idem, "idempotency_store", store)
    tokens = iter(["tok-original", "tok-current"])
    issue = MagicMock(side_effect=lambda addr: next(tokens))
    monkeypatch.setattr(bandwidth_credit_manager, "issue_token", issue)
    app, bought = _app(fac)
    c = TestClient(app)
    first = _post(c, path="/api/v1/chunks/credit", body={})
    assert first.json()["token"] == "tok-original"
    assert "tok-original" not in (tmp_path / "idem.json").read_text()
    retry = _post(c, path="/api/v1/chunks/credit", body={})
    assert retry.json() == {"address": ALICE, "token": "tok-current"}
    # Looked up for the payer, with no rotation and no second top-up.
    issue.assert_called_with(ALICE)
    assert bought == ["credit"] and fac.settle.await_count == 1


def test_malformed_entry_makes_the_store_unavailable(tmp_path):
    path = tmp_path / "idem.json"
    path.write_text(json.dumps({"entries": {"abc": {"state": "done", "expires": 9e12}}}))
    assert IdempotencyStore(state_file=str(path)).unavailable


@pytest.mark.parametrize("nonce,expected", [
    ("0x" + "AB" * 32, "0x" + "ab" * 32),
    ("ab" * 32, "0x" + "ab" * 32),          # same bytes32 without the prefix
    ("0x" + "ab" * 31, None),               # too short
    ("0x" + "zz" * 32, None),
])
def test_authorization_key_is_canonical(nonce, expected):
    from types import SimpleNamespace
    from app.x402.settlement import authorization_key
    payload = SimpleNamespace(payload=SimpleNamespace(
        authorization=SimpleNamespace(from_=ALICE.upper().replace("0X", "0x"), nonce=nonce)))
    key = authorization_key(payload)
    assert key == ((ALICE, expected) if expected else None)
