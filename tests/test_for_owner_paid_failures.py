"""A paid buy-batch-for-owner that creates no batch is answered honestly (#692).

The x402 payment settles immediately before createBatch. So:

- a signer that is busy is refused BEFORE settlement, with nothing charged;
- a failure AFTER settlement (the signer became busy in between, or the
  transaction failed) is a paid non-delivery: DELIVERY_FAILED_AFTER_PAYMENT,
  as the pool answers it, never "you were not charged";
- the spend hold is given back wherever no BZZ was spent.

Mounts the real for-owner router with the real x402 dependency, settlement and
middleware, and a facilitator mock that counts settlements.
"""
import base64
import json
import secrets
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from app.api.endpoints import stamps_for_owner as ep
from app.core.config import settings
from app.services import gnosis_chain, spend_budget
from app.services.gnosis_chain import GnosisChainClient, GnosisChainError, SignerBusy
from app.services.spend_budget import SpendBudgetTracker
from app.x402.dependency import require_x402_payment
from app.x402.middleware import X402Middleware
from app.x402.models_v1 import SettleResponse, VerifyResponse
from app.x402.ratelimit import reset_rate_limiter
from tests.test_gnosis_chain import KEY, _Chain

PAYER = "0x" + "a1" * 20
OWNER = "0x" + "1" * 40
TX = "0x" + "cd" * 32
CHAINSTATE = {"currentPrice": "24000", "block": 1, "chainTip": 1, "totalAmount": "1"}
BODY = {"owner": OWNER, "depth": 17, "duration_hours": 24}


def pay(nonce=None):
    """A payment header with a fresh nonce, as a retrying client would send."""
    payload = {"x402Version": 1, "scheme": "exact", "network": "base-sepolia", "payload": {
        "signature": "0x" + "ab" * 65,
        "authorization": {"from": PAYER, "to": "0xpayee", "value": "100000", "validAfter": "0",
                          "validBefore": "9999999999", "nonce": nonce or "0x" + secrets.token_hex(32)}}}
    return base64.b64encode(json.dumps(payload).encode()).decode()


@pytest.fixture
def tracker(tmp_path, monkeypatch):
    t = SpendBudgetTracker(state_file=str(tmp_path / "spend.json"))
    monkeypatch.setattr(spend_budget, "spend_budget_tracker", t)
    monkeypatch.setattr(settings, "GATEWAY_DAILY_BZZ_CEILING", 10.0)
    return t


@pytest.fixture
def fac(monkeypatch, tracker):
    monkeypatch.setattr(settings, "X402_ENABLED", True)
    monkeypatch.setattr(settings, "X402_PAY_TO_ADDRESS", "0xpayee")
    monkeypatch.setattr(settings, "X402_NETWORK", "base-sepolia")
    monkeypatch.setattr(settings, "STAMP_PURCHASE_FOR_OTHERS_ENABLED", True)
    monkeypatch.setattr(settings, "STAMP_FOR_OTHERS_REQUIRE_WHITELIST", False)
    reset_rate_limiter()
    f = MagicMock()
    f.verify = AsyncMock(side_effect=lambda payment, **kw: VerifyResponse(
        isValid=True, payer=payment.payload.authorization.from_))
    f.settle = AsyncMock(side_effect=lambda **kw: SettleResponse(success=True, transaction=TX))
    with patch("app.x402.dependency._get_facilitator_client", return_value=f), \
         patch("app.x402.dependency._calculate_price_for_request",
               new=AsyncMock(return_value={"price_usd": 0.02, "description": "t"})), \
         patch("app.services.swarm_api.get_chainstate", new=AsyncMock(return_value=CHAINSTATE)), \
         patch.object(ep, "record_purchase"), patch.object(ep, "stamp_ownership_manager"):
        yield f
    reset_rate_limiter()


@pytest.fixture
def signer():
    """A real chain client on a fake chain: its own lock and nonce check run."""
    chain = _Chain(allowance=10**30)
    client = GnosisChainClient(rpc_url="x", private_key=KEY, chain_id=100)
    client.preflight = AsyncMock(return_value={"is_critical": False, "warnings": []})
    with patch.object(ep, "gnosis_chain_client", client), \
         patch.object(GnosisChainClient, "_connect", return_value=(chain.w3, chain.acct)), \
         patch.object(gnosis_chain, "SIGNER_LOCK_TIMEOUT_SECONDS", 0.05):
        yield client, chain


def _app(fac):
    app = FastAPI()
    app.include_router(ep.router, prefix="/api/v1/stamps", dependencies=[Depends(require_x402_payment)])
    app.add_middleware(X402Middleware, facilitator_client=fac)
    return app


def _post(client, key=None):
    headers = {"X-PAYMENT": pay()}
    if key is not None:
        headers["Idempotency-Key"] = key
    return client.post("/api/v1/stamps/for-owner", json=BODY, headers=headers)


def _counts():
    return (REGISTRY.get_sample_value("gateway_for_owner_batches_total",
                                      {"status": "settled_not_delivered"}) or 0.0,
            REGISTRY.get_sample_value("gateway_x402_settlements_total",
                                      {"result": "settled_not_delivered", "version": "1"}) or 0.0)


def _refund_events():
    from app.x402.audit import read_audit_log
    return [e for e in read_audit_log() if e["data"].get("stage") == "delivery_after_settlement"]


def _assert_paid_not_delivered(r):
    assert r.status_code == 500, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "DELIVERY_FAILED_AFTER_PAYMENT"
    assert detail["x402_status"] == "settled_not_delivered"
    assert detail["transaction"] == TX
    assert "not charged" not in r.text
    assert r.headers["X-Payment-Transaction"] == TX
    assert r.headers["X-Payment-Status"] == "settled_not_delivered"


# --- refused before settlement: nothing charged ---

def test_held_signer_lock_is_refused_before_payment(fac, tracker, signer):
    client, chain = signer
    client._signer_lock.acquire()  # another request is creating a batch
    try:
        r = _post(TestClient(_app(fac), raise_server_exceptions=False))
    finally:
        client._signer_lock.release()
    assert r.status_code == 503, r.text
    assert r.json()["detail"]["code"] == "SIGNER_BUSY"
    assert r.headers["Retry-After"] == "60"
    assert fac.settle.await_count == 0
    assert chain.sent == []
    assert tracker.snapshot()["gateway_spent"] == 0.0


def test_unconfirmed_transaction_is_refused_before_payment(fac, tracker, signer):
    _, chain = signer
    chain.sent = [0]  # pending nonce 1, latest 0
    r = _post(TestClient(_app(fac), raise_server_exceptions=False))
    assert r.status_code == 503, r.text
    assert r.json()["detail"]["code"] == "SIGNER_BUSY"
    assert fac.settle.await_count == 0
    assert chain.sent == [0]
    assert tracker.snapshot()["gateway_spent"] == 0.0


def test_unreadable_signer_state_is_refused_before_payment(fac, tracker, signer):
    _, chain = signer
    chain.w3.eth.get_transaction_count.side_effect = OSError("rpc down")
    r = _post(TestClient(_app(fac), raise_server_exceptions=False))
    assert r.status_code == 503, r.text
    assert r.json()["detail"]["code"] == "SIGNER_STATE_UNAVAILABLE"
    assert r.headers["Retry-After"] == "60"
    assert fac.settle.await_count == 0
    assert tracker.snapshot()["gateway_spent"] == 0.0


def test_refused_settlement_gives_the_hold_back(fac, tracker, signer):
    client, _ = signer
    fac.settle = AsyncMock(return_value=SettleResponse(success=False, errorReason="insufficient_funds"))
    with patch.object(client, "create_batch", new=AsyncMock()) as create:
        r = _post(TestClient(_app(fac), raise_server_exceptions=False))
    assert r.status_code == 402, r.text
    create.assert_not_called()
    assert tracker.snapshot()["gateway_spent"] == 0.0


# --- failed after settlement: paid, not delivered ---

def test_signer_busy_after_settlement_is_a_paid_non_delivery(fac, tracker, signer):
    """The race: the check passed, then the signer was taken before createBatch."""
    client, _ = signer
    before = _counts()
    with patch.object(client, "create_batch",
                      new=AsyncMock(side_effect=SignerBusy("signer is busy with another request"))):
        r = _post(TestClient(_app(fac), raise_server_exceptions=False))
    _assert_paid_not_delivered(r)
    assert fac.settle.await_count == 1  # the middleware does not settle again
    assert tracker.snapshot()["gateway_spent"] == 0.0
    after = _counts()
    assert after[0] == before[0] + 1 and after[1] == before[1] + 1
    assert any(TX in e["data"]["reason"] for e in _refund_events())


def test_chain_failure_after_settlement_is_a_paid_non_delivery(fac, tracker, signer):
    client, _ = signer
    before = _counts()
    with patch.object(client, "create_batch", new=AsyncMock(
            side_effect=GnosisChainError("rpc https://secret-rpc.example/key123 reverted: 0xdeadbeef"))):
        r = _post(TestClient(_app(fac), raise_server_exceptions=False))
    _assert_paid_not_delivered(r)
    assert "secret-rpc" not in r.text and "0xdeadbeef" not in r.text
    assert fac.settle.await_count == 1
    assert tracker.snapshot()["gateway_spent"] == 0.0
    after = _counts()
    assert after[0] == before[0] + 1 and after[1] == before[1] + 1
    assert any(TX in e["data"]["reason"] for e in _refund_events())


def test_retry_with_the_same_key_is_told_the_same_and_not_charged(fac, tracker, signer):
    client, _ = signer
    c = TestClient(_app(fac), raise_server_exceptions=False)
    with patch.object(client, "create_batch", new=AsyncMock(side_effect=SignerBusy("busy"))) as create:
        first = _post(c, key="k-692")
        retry = _post(c, key="k-692")
    _assert_paid_not_delivered(first)
    assert retry.status_code == 409, retry.text
    detail = retry.json()["detail"]
    assert detail["code"] == "IDEMPOTENCY_KEY_SETTLED_PENDING"
    assert detail["x402_status"] == "settled_not_delivered"
    assert detail["transaction"] == TX
    # The retry's own payment was verified, never settled; createBatch ran once.
    assert fac.settle.await_count == 1
    assert fac.verify.await_count == 2
    assert create.await_count == 1


# --- unchanged: success and a pending transaction ---

def test_success_still_settles_once_and_spends(fac, tracker, signer):
    client, _ = signer
    created = {"batch_id": "0x" + "ab" * 32, "tx_hash": "0xbeef", "owner": OWNER}
    with patch.object(client, "create_batch", new=AsyncMock(return_value=created)):
        r = _post(TestClient(_app(fac), raise_server_exceptions=False))
    assert r.status_code == 201, r.text
    assert fac.settle.await_count == 1
    assert tracker.snapshot()["gateway_spent"] > 0


def test_pending_transaction_still_answers_202_and_keeps_the_hold(fac, tracker, signer):
    from app.services.gnosis_chain import TransactionPending
    client, _ = signer
    with patch.object(client, "create_batch",
                      new=AsyncMock(side_effect=TransactionPending("0xfeed", "0x" + "cd" * 32, OWNER))):
        r = _post(TestClient(_app(fac), raise_server_exceptions=False))
    assert r.status_code == 202, r.text
    assert r.json()["confirmed"] is False
    assert fac.settle.await_count == 1
    assert tracker.snapshot()["gateway_spent"] > 0
