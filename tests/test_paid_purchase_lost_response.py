"""A paid stamp purchase whose Bee response is lost after settlement (#400).

Bee's POST /stamps waits for the on-chain transaction. If it times out after
the payment settled, the batch may still have been bought: the gateway looks
for it by its purchase label, registers it to the payer, and returns it, or
answers 202 and finishes in the background.
"""
import asyncio
import base64
import json
import secrets
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from x402.types import SettleResponse, VerifyResponse

from app.core.config import settings
from app.services import swarm_api
from app.services.stamp_ownership import stamp_ownership_manager
from app.x402.dependency import require_x402_payment
from app.x402.middleware import X402Middleware
from app.x402.ratelimit import reset_rate_limiter

PAYER = "0x" + "c3" * 20
BATCH = "e" * 64


def pay():
    payload = {"x402Version": 1, "scheme": "exact", "network": "base-sepolia", "payload": {
        "signature": "0x" + "ab" * 65,
        "authorization": {"from": PAYER, "to": "0xpayee", "value": "100000", "validAfter": "0",
                          "validBefore": "9999999999", "nonce": "0x" + secrets.token_hex(32)}}}
    return base64.b64encode(json.dumps(payload).encode()).decode()


@pytest.fixture
def env(monkeypatch):
    from app.api.endpoints import stamps
    monkeypatch.setattr(settings, "X402_ENABLED", True)
    monkeypatch.setattr(settings, "X402_PAY_TO_ADDRESS", "0xpayee")
    monkeypatch.setattr(settings, "X402_NETWORK", "base-sepolia")
    monkeypatch.setattr(stamps, "_PENDING_FIRST_WAIT_SECONDS", 0)
    monkeypatch.setattr(stamp_ownership_manager, "_registry", {})
    monkeypatch.setattr(stamp_ownership_manager, "_save_state", lambda: None)
    reset_rate_limiter()
    fac = MagicMock()
    fac.verify = AsyncMock(side_effect=lambda payment, **kw: VerifyResponse(
        isValid=True, payer=payment.payload.authorization.from_))
    fac.settle = AsyncMock(return_value=SettleResponse(success=True, transaction="0x" + "ab" * 32))
    buy = AsyncMock(side_effect=httpx.ReadTimeout("slow chain"))
    find = AsyncMock(return_value=BATCH)
    with patch("app.x402.dependency._get_facilitator_client", return_value=fac), \
         patch("app.x402.dependency._calculate_price_for_request",
               new=AsyncMock(return_value={"price_usd": 0.02, "description": "t"})), \
         patch("app.services.swarm_api.get_chainstate",
               new=AsyncMock(return_value={"currentPrice": "24000", "minimumValidityBlocks": 17280,
                                           "block": 180000})), \
         patch("app.services.swarm_api.check_sufficient_funds",
               new=AsyncMock(return_value={"sufficient": True, "wallet_balance_bzz": 10.0,
                                           "required_bzz": 0.01, "shortfall_bzz": 0.0})), \
         patch("app.services.swarm_api.purchase_postage_stamp", new=buy), \
         patch("app.services.swarm_api.find_purchased_batch", new=find):
        app = FastAPI()
        app.include_router(stamps.router, prefix="/api/v1/stamps", dependencies=[Depends(require_x402_payment)])
        app.add_middleware(X402Middleware, facilitator_client=fac)
        yield app, fac, buy, find
    reset_rate_limiter()


BODY = {"duration_hours": 24, "depth": 17}


def test_bee_timeout_after_settlement_returns_the_batch_found(env):
    app, fac, buy, find = env
    r = TestClient(app).post("/api/v1/stamps/", json=BODY, headers={"X-PAYMENT": pay()})
    assert r.status_code == 201 and r.json()["batchID"] == BATCH
    assert stamp_ownership_manager.get_stamp_info(BATCH)["owner"] == PAYER
    # Looked up by the label the purchase was made with, which a paid
    # purchase always has.
    label = buy.await_args.kwargs["label"]
    assert label.startswith("paid-")
    assert find.await_args.args[:3] == (label, 17, buy.await_args.kwargs["amount"])
    assert find.await_args.args[4] == 180000       # chain block before the purchase
    assert fac.settle.await_count == 1


def test_caller_label_is_kept(env):
    app, _, buy, find = env
    TestClient(app).post("/api/v1/stamps/", json={**BODY, "label": "mine"}, headers={"X-PAYMENT": pay()})
    assert buy.await_args.kwargs["label"] == "mine" and find.await_args.args[0] == "mine"


def test_not_found_answers_202_then_registers_and_resolves_the_key(env):
    app, fac, buy, find = env
    find.side_effect = [None, BATCH]      # in the request, then in the background

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            h = lambda: {"X-PAYMENT": pay(), "Idempotency-Key": "k"}
            first = await c.post("/api/v1/stamps/", json=BODY, headers=h())
            for _ in range(100):
                if stamp_ownership_manager.get_stamp_info(BATCH):
                    break
                await asyncio.sleep(0.01)
            retry = await c.post("/api/v1/stamps/", json=BODY, headers=h())
            return first, retry

    first, retry = asyncio.run(run())
    assert first.status_code == 202
    body = first.json()
    assert body["code"] == "PURCHASE_PENDING"
    assert body["transaction"] == "0x" + "ab" * 32 and body["label"].startswith("paid-")
    assert stamp_ownership_manager.get_stamp_info(BATCH)["owner"] == PAYER
    # The retry gets the final result, and pays nothing.
    assert retry.status_code == 201 and retry.json()["batchID"] == BATCH
    assert retry.headers["Idempotent-Replayed"] == "true"
    assert fac.settle.await_count == 1


def test_free_tier_timeout_is_still_a_502_without_lookup(env, monkeypatch):
    app, fac, buy, find = env
    monkeypatch.setattr(settings, "X402_FREE_TIER_ENABLED", True)
    r = TestClient(app).post("/api/v1/stamps/", json=BODY, headers={"X-Payment-Mode": "free"})
    assert r.status_code == 502
    find.assert_not_called()
    assert buy.await_args.kwargs["label"] is None


def test_batch_is_registered_even_if_bookkeeping_fails(env):
    app, _, buy, _ = env
    buy.side_effect = None
    buy.return_value = BATCH
    with patch("app.api.endpoints.stamps.record_purchase", side_effect=RuntimeError("boom")):
        r = TestClient(app).post("/api/v1/stamps/", json=BODY, headers={"X-PAYMENT": pay()})
    assert r.status_code == 201 and r.json()["batchID"] == BATCH
    assert stamp_ownership_manager.get_stamp_info(BATCH)["owner"] == PAYER


# --- swarm_api ------------------------------------------------------------

def _bee(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_label_is_sent_as_query_parameter():
    """Bee reads the label from the query string and ignores a JSON body."""
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(201, json={"batchID": BATCH})

    with patch("app.services.swarm_api.get_client", return_value=_bee(handler)):
        asyncio.run(swarm_api.purchase_postage_stamp(1000, 17, label="lbl"))
    assert seen[0].url.params["label"] == "lbl"


def _stamp(batch, label="lbl", depth=17, amount="1000", block=500):
    return {"batchID": batch, "label": label, "depth": depth, "amount": amount, "blockNumber": block}


@pytest.mark.parametrize("stamps,known,expected", [
    ([_stamp("a"), _stamp("b", label="other"), _stamp("c", depth=20)], set(), "a"),
    ([_stamp("a"), _stamp("b")], {"a"}, "b"),           # someone else's already
    ([_stamp("a"), _stamp("b")], set(), None),          # ambiguous: do not guess
    ([_stamp("a", amount="999")], set(), None),
    # Bee's label for a batch whose API call was cut off: only if new enough.
    ([_stamp("a", label="recovered", block=500)], set(), "a"),
    ([_stamp("a", label="recovered", block=499)], set(), None),
])
def test_find_purchased_batch(stamps, known, expected):
    with patch("app.services.swarm_api.get_local_stamps", new=AsyncMock(return_value=stamps)):
        got = asyncio.run(swarm_api.find_purchased_batch("lbl", 17, 1000, known.__contains__, 500, 0))
    assert got == expected


def test_find_purchased_batch_waits_for_the_node_to_see_it():
    lists = AsyncMock(side_effect=[[], [], [_stamp("a")]])
    with patch("app.services.swarm_api.get_local_stamps", new=lists):
        got = asyncio.run(swarm_api.find_purchased_batch("lbl", 17, 1000, lambda b: False, None, 5, interval=0.01))
    assert got == "a" and lists.await_count == 3
