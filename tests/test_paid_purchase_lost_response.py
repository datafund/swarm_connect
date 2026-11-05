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

import httpx2
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
    buy = AsyncMock(side_effect=httpx2.ReadTimeout("slow chain"))
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


def test_caller_label_gets_a_unique_suffix(env):
    """A caller's label alone could match someone else's batch; the one sent to Bee is unique."""
    app, _, buy, find = env
    TestClient(app).post("/api/v1/stamps/", json={**BODY, "label": "mine"}, headers={"X-PAYMENT": pay()})
    sent = buy.await_args.kwargs["label"]
    assert sent.startswith("mine-") and len(sent) == len("mine-") + 12
    assert find.await_args.args[0] == sent


def test_found_batch_already_owned_is_not_taken(env):
    """A batch registered to someone else between the lookup and the claim is left alone."""
    app, fac, buy, find = env
    stamp_ownership_manager._registry[BATCH] = {"owner": "0x" + "d4" * 20, "mode": "paid"}
    r = TestClient(app).post("/api/v1/stamps/", json=BODY, headers={"X-PAYMENT": pay()})
    assert r.status_code == 202
    assert stamp_ownership_manager.get_stamp_info(BATCH)["owner"] == "0x" + "d4" * 20


def test_lookup_error_is_not_found_yet(env):
    app, fac, buy, find = env
    find.side_effect = RuntimeError("bee listing down")
    r = TestClient(app).post("/api/v1/stamps/", json=BODY, headers={"X-PAYMENT": pay()})
    assert r.status_code == 202 and r.json()["code"] == "PURCHASE_PENDING"


@pytest.mark.parametrize("bee_status,looked_up", [(502, True), (504, True), (500, False)])
def test_proxy_gateway_errors_are_unknown_outcomes(env, bee_status, looked_up):
    app, fac, buy, find = env
    req = httpx2.Request("POST", "http://bee/stamps/1/17")
    buy.side_effect = httpx2.HTTPStatusError("x", request=req, response=httpx2.Response(bee_status, request=req))
    r = TestClient(app).post("/api/v1/stamps/", json=BODY, headers={"X-PAYMENT": pay()})
    assert (r.status_code == 201) == looked_up
    assert find.await_count == (1 if looked_up else 0)


def _run_pending(app, find_results, key="k"):
    """First request (202), then wait for the background task, then a keyed retry."""
    from app.api.endpoints import stamps

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://t") as c:
            h = lambda: {"X-PAYMENT": pay(), "Idempotency-Key": key}
            first = await c.post("/api/v1/stamps/", json=BODY, headers=h())
            for _ in range(200):
                if not stamps._PENDING_TASKS:
                    break
                await asyncio.sleep(0.01)
            retry = await c.post("/api/v1/stamps/", json=BODY, headers=h())
            return first, retry
    return asyncio.run(run())


def _audit(event):
    from app.x402.audit import AuditEventType, read_audit_log
    return read_audit_log(max_entries=1, event_type=AuditEventType(event))


def test_202_is_audited_with_what_is_needed_to_find_the_batch(env):
    app, fac, buy, find = env
    find.side_effect = [None, BATCH]
    first, _ = _run_pending(app, None)
    ev = _audit("purchase_pending")[0]
    assert ev["wallet_address"] == PAYER
    assert ev["data"]["label"] == first.json()["label"]
    assert ev["data"]["start_block"] == 180000 and ev["data"]["depth"] == 17
    assert ev["data"]["transaction_hash"] == "0x" + "ab" * 32


def _sent_nonce(fac):
    """The nonce of the first (settled) payment, canonical: lower-case, 0x."""
    return "0x" + str(fac.settle.await_args_list[0].kwargs["payment"].payload.authorization.nonce)[-64:].lower()


def test_pending_purchase_records_carry_the_nonce(env):
    """#553 review 2: purchase_pending, the late payment_delivered and a refund record
    all carry the authorization nonce, for matching an on-chain transfer."""
    app, fac, buy, find = env
    find.side_effect = [None, BATCH]
    first, _ = _run_pending(app, None)
    nonce = _sent_nonce(fac)
    assert _audit("purchase_pending")[0]["data"]["nonce"] == nonce
    assert _audit("payment_delivered")[0]["data"]["nonce"] == nonce


def test_pending_purchase_refund_record_carries_the_nonce(env):
    app, fac, buy, find = env
    find.side_effect = [None, None]
    first, _ = _run_pending(app, None, key="k-never-nonce")
    assert _audit("payment_failed")[0]["data"]["nonce"] == _sent_nonce(fac)


def test_never_found_records_a_refund_and_a_final_result(env):
    app, fac, buy, find = env
    find.side_effect = [None, None]
    first, retry = _run_pending(app, None, key="k-never")
    assert first.status_code == 202
    failed = _audit("payment_failed")[0]
    assert "after settlement: not found" in failed["data"]["reason"]
    assert retry.status_code == 500
    assert retry.json()["code"] == "DELIVERY_FAILED_AFTER_PAYMENT"
    assert fac.settle.await_count == 1


def test_interrupted_background_search_records_a_refund(env):
    from app.api.endpoints import stamps
    app, fac, buy, find = env

    calls = []

    async def lookup(*a, **kw):
        calls.append(1)
        if len(calls) > 1:          # the background search: still looking at shutdown
            await asyncio.sleep(3600)
        return None
    find.side_effect = lookup

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/api/v1/stamps/", json=BODY, headers={"X-PAYMENT": pay()})
            await asyncio.sleep(0.05)
            assert stamps._PENDING_TASKS, "the running search is held"
            for t in list(stamps._PENDING_TASKS):
                t.cancel()
            await asyncio.gather(*list(stamps._PENDING_TASKS), return_exceptions=True)
            return r
    assert asyncio.run(run()).status_code == 202
    assert "interrupted" in _audit("payment_failed")[0]["data"]["reason"]


def test_not_found_answers_202_then_registers_and_resolves_the_key(env):
    app, fac, buy, find = env
    find.side_effect = [None, BATCH]      # in the request, then in the background

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://t") as c:
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
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


def test_label_is_sent_as_query_parameter():
    """Bee reads the label from the query string and ignores a JSON body."""
    seen = []

    def handler(request):
        seen.append(request)
        return httpx2.Response(201, json={"batchID": BATCH})

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
    # Older than the purchase: an orphan with the same label, never this one.
    ([_stamp("a", block=499)], set(), None),
    ([_stamp("a", block=499), _stamp("b", block=501)], set(), "b"),
    # Bee's "recovered" batches are never matched: nothing ties one to this purchase.
    ([_stamp("a", label="recovered", block=600)], set(), None),
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


def test_find_purchased_batch_survives_a_failed_poll():
    lists = AsyncMock(side_effect=[RuntimeError("down"), [_stamp("a")]])
    with patch("app.services.swarm_api.get_local_stamps", new=lists):
        got = asyncio.run(swarm_api.find_purchased_batch("lbl", 17, 1000, lambda b: False, None, 5, interval=0.01))
    assert got == "a"


def test_register_stamp_refuses_or_logs_a_change_of_owner(tmp_path, caplog):
    from app.services.stamp_ownership import StampOwnershipManager
    m = StampOwnershipManager(state_file=str(tmp_path / "o.json"))
    assert m.register_stamp(BATCH, "0x" + "aa" * 20, "paid", "direct_purchase")
    assert not m.register_stamp(BATCH, "0x" + "bb" * 20, "paid", "direct_purchase", only_if_unowned=True)
    assert m.get_stamp_info(BATCH)["owner"] == "0x" + "aa" * 20
    m.register_stamp(BATCH, "0x" + "bb" * 20, "paid", "direct_purchase")
    assert "Re-registering" in caplog.text


def _slow_bee(result, delay=0.2):
    """Bee answers only after the gateway's deadline, as a slow chain does."""
    async def buy(**kw):
        await asyncio.sleep(delay)
        if isinstance(result, Exception):
            raise result
        return result
    return buy


def test_slow_bee_is_awaited_not_cut_off(env, monkeypatch):
    """Bee's request stays open past our deadline, so the batch keeps its label
    and Bee's own answer delivers it: no lookup at all."""
    app, fac, buy, find = env
    monkeypatch.setattr(settings, "SWARM_STAMP_PURCHASE_TIMEOUT_SECONDS", 0.05)
    buy.side_effect = _slow_bee(BATCH)
    first, retry = _run_pending(app, None, key="k-slow")
    assert first.status_code == 202
    assert buy.await_args.kwargs["timeout"] == settings.STAMP_PURCHASE_BEE_TIMEOUT_SECONDS
    assert stamp_ownership_manager.get_stamp_info(BATCH)["owner"] == PAYER
    assert retry.status_code == 201 and retry.json()["batchID"] == BATCH
    find.assert_not_called()
    assert fac.settle.await_count == 1


def test_slow_bee_refusal_is_recorded_as_such(env, monkeypatch):
    app, fac, buy, find = env
    monkeypatch.setattr(settings, "SWARM_STAMP_PURCHASE_TIMEOUT_SECONDS", 0.05)
    req = httpx2.Request("POST", "http://bee/stamps/1/17")
    buy.side_effect = _slow_bee(httpx2.HTTPStatusError(
        "x", request=req, response=httpx2.Response(400, json={"message": "insufficient funds"}, request=req)))
    first, retry = _run_pending(app, None, key="k-refused")
    assert first.status_code == 202
    assert "refused by Bee" in _audit("payment_failed")[0]["data"]["reason"]
    assert retry.json()["code"] == "DELIVERY_FAILED_AFTER_PAYMENT"
    find.assert_not_called()


@pytest.mark.parametrize("bee_response", [
    {"text": ""},
    {"json": {"message": "dial tcp 10.0.0.1:1633: connect: refused (bee.internal)"}},
])
def test_slow_bee_refusal_hides_the_bee_address(env, monkeypatch, bee_response):
    """A refusal after payment: neither the refund reason in the audit log nor
    any response names the internal Bee address (#549)."""
    app, fac, buy, find = env
    monkeypatch.setattr(settings, "SWARM_STAMP_PURCHASE_TIMEOUT_SECONDS", 0.05)
    req = httpx2.Request("POST", "http://bee.internal:1633/stamps/1/17")
    buy.side_effect = _slow_bee(httpx2.HTTPStatusError(
        "Client error '400 Bad Request' for url 'http://bee.internal:1633/stamps/1/17'",
        request=req, response=httpx2.Response(400, request=req, **bee_response)))
    first, retry = _run_pending(app, None, key="k-refused-hidden")
    reason = _audit("payment_failed")[0]["data"]["reason"]
    assert "refused by Bee" in reason
    for shown in (reason, first.text, retry.text):
        for secret in ("bee.internal", "10.0.0.1", "1633"):
            assert secret not in shown, shown


def test_slow_bee_connection_lost_falls_back_to_the_label(env, monkeypatch):
    app, fac, buy, find = env
    monkeypatch.setattr(settings, "SWARM_STAMP_PURCHASE_TIMEOUT_SECONDS", 0.05)
    buy.side_effect = _slow_bee(httpx2.RemoteProtocolError("bee restarted"))
    first, retry = _run_pending(app, None, key="k-lost")
    assert first.status_code == 202
    assert find.await_count == 1
    assert retry.status_code == 201 and retry.json()["batchID"] == BATCH


def test_taken_batch_is_recorded_as_taken(env):
    app, fac, buy, find = env
    stamp_ownership_manager._registry[BATCH] = {"owner": "0x" + "d4" * 20, "mode": "paid"}
    first, _ = _run_pending(app, None, key="k-taken")
    assert first.status_code == 202
    assert "already registered" in _audit("payment_failed")[0]["data"]["reason"]


def test_paid_purchases_waiting_on_bee_are_capped_before_payment(env, monkeypatch):
    from app.api.endpoints import stamps
    app, fac, buy, find = env
    monkeypatch.setattr(settings, "STAMP_MAX_CONCURRENT_PAID_PURCHASES", 1)
    monkeypatch.setattr(settings, "SWARM_STAMP_PURCHASE_TIMEOUT_SECONDS", 0.05)
    gate = asyncio.Event()

    async def slow(**kw):
        await gate.wait()
        return BATCH
    buy.side_effect = slow

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://t") as c:
            first = await c.post("/api/v1/stamps/", json=BODY, headers={"X-PAYMENT": pay()})
            second = await c.post("/api/v1/stamps/", json=BODY, headers={"X-PAYMENT": pay()})
            gate.set()
            for _ in range(200):
                if not stamps._PENDING_TASKS:
                    break
                await asyncio.sleep(0.01)
            return first, second
    first, second = asyncio.run(run())
    assert first.status_code == 202
    assert second.status_code == 503 and second.json()["detail"]["code"] == "PURCHASE_CAPACITY"
    assert fac.settle.await_count == 1          # the refused one was not charged
    assert stamps._paid_purchases_in_flight == 0


def test_shutdown_waits_then_records_a_purchase_still_at_bee(env, monkeypatch):
    from app.api.endpoints import stamps
    app, fac, buy, find = env
    monkeypatch.setattr(settings, "SWARM_STAMP_PURCHASE_TIMEOUT_SECONDS", 0.05)

    async def hung(**kw):
        await asyncio.sleep(3600)
    buy.side_effect = hung

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/api/v1/stamps/", json=BODY, headers={"X-PAYMENT": pay()})
            await stamps.drain_pending_purchases(0.05)
            return r
    assert asyncio.run(run()).status_code == 202
    reason = _audit("payment_failed")[0]["data"]["reason"]
    assert "in flight at shutdown" in reason and "paid-" in reason
    assert not stamps._PENDING_TASKS


def test_shutdown_lets_a_purchase_finish_within_the_grace(env, monkeypatch):
    from app.api.endpoints import stamps
    app, fac, buy, find = env
    monkeypatch.setattr(settings, "SWARM_STAMP_PURCHASE_TIMEOUT_SECONDS", 0.05)
    buy.side_effect = _slow_bee(BATCH, delay=0.2)

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://t") as c:
            await c.post("/api/v1/stamps/", json=BODY, headers={"X-PAYMENT": pay()})
            await stamps.drain_pending_purchases(5)
    asyncio.run(run())
    assert stamp_ownership_manager.get_stamp_info(BATCH)["owner"] == PAYER


def test_pending_setup_failure_is_still_recorded(env, monkeypatch):
    from app.api.endpoints import stamps
    app, fac, buy, find = env
    find.return_value = None

    def broken(*a, **kw):
        raise TypeError("cannot start")
    monkeypatch.setattr(stamps, "_finish_pending_purchase", broken)
    r = TestClient(app).post("/api/v1/stamps/", json=BODY, headers={"X-PAYMENT": pay()})
    assert r.status_code == 202
    assert "not followed up" in _audit("payment_failed")[0]["data"]["reason"]


def test_late_result_reaches_a_key_whose_request_ended_before_the_202(tmp_path):
    """Client gone before the 202: the key's entry is still 'settled'. The late
    201 must finalise it, not leave SETTLED_PENDING (ask for a refund)."""
    from app.x402.idempotency import IdempotencyStore
    s = IdempotencyStore(state_file=str(tmp_path / "idem.json"))
    _, _, token = s.begin("e", "h", "a1")
    s.mark_settled("e", token, "a1", "0xtx")
    s.abandon("e", token)                      # the request ended without a response
    s.resolve("e", token, 201, b'{"batchID": "b"}')
    state, entry, _ = s.begin("e", "h", "a2")
    assert state == "done" and entry["status"] == 201
    assert entry["headers"]["x-payment-transaction"] == "0xtx"


# --- with the spend reservation (#408) --------------------------------------

def _gateway_spent():
    from app.api.endpoints import stamps
    return stamps.spend_budget_tracker.snapshot()["gateway_spent"]


def test_pending_paid_purchase_keeps_its_spend_hold(env):
    """A 202 is not a refusal: the batch may still be bought, so the BZZ stays reserved."""
    app, fac, buy, find = env
    find.return_value = None
    r = TestClient(app).post("/api/v1/stamps/", json=BODY, headers={"X-PAYMENT": pay()})
    assert r.status_code == 202
    assert _gateway_spent() > 0


def test_refused_paid_purchase_releases_its_spend_hold(env):
    """Bee's own 4xx answer: nothing was bought, so nothing stays reserved."""
    app, fac, buy, find = env
    req = httpx2.Request("POST", "http://bee/stamps/1/17")
    buy.side_effect = httpx2.HTTPStatusError("x", request=req, response=httpx2.Response(400, request=req))
    r = TestClient(app).post("/api/v1/stamps/", json=BODY, headers={"X-PAYMENT": pay()})
    assert r.status_code == 400
    find.assert_not_called()
    assert _gateway_spent() == 0


def test_capacity_refusal_releases_its_spend_hold(env, monkeypatch):
    from app.api.endpoints import stamps
    app, fac, buy, find = env
    monkeypatch.setattr(stamps, "_paid_purchases_in_flight", 1)
    monkeypatch.setattr(settings, "STAMP_MAX_CONCURRENT_PAID_PURCHASES", 1)
    r = TestClient(app).post("/api/v1/stamps/", json=BODY, headers={"X-PAYMENT": pay()})
    assert r.status_code == 503
    assert fac.settle.await_count == 0
    assert _gateway_spent() == 0


# --- after the 202: spend metric, hold and expiry (#468) ---------------------

def _spend_recorded():
    from prometheus_client import REGISTRY
    return sum(REGISTRY.get_sample_value("gateway_stamp_spend_bzz_total",
                                         {"operation": "stamp purchase", "charged": c}) or 0.0
               for c in ("budget", "paid"))


def test_late_delivery_is_counted_as_spent(env, monkeypatch):
    """Delivered after the 202: the BZZ is in gateway_stamp_spend_bzz_total, as
    for a purchase Bee answered in time."""
    app, fac, buy, find = env
    monkeypatch.setattr(settings, "SWARM_STAMP_PURCHASE_TIMEOUT_SECONDS", 0.05)
    buy.side_effect = _slow_bee(BATCH)
    before = _spend_recorded()
    first, retry = _run_pending(app, None, key="k-late-spend")
    assert first.status_code == 202
    assert retry.status_code == 201
    assert _spend_recorded() - before == pytest.approx(_gateway_spent())
    assert _gateway_spent() > 0


def test_late_delivery_found_by_label_is_counted_as_spent(env):
    """The other late path: Bee gave no answer, the background lookup found it."""
    app, fac, buy, find = env
    find.side_effect = [None, BATCH]
    before = _spend_recorded()
    first, retry = _run_pending(app, None, key="k-late-lookup-spend")
    assert first.status_code == 202 and retry.status_code == 201
    assert _spend_recorded() - before == pytest.approx(_gateway_spent())


def test_background_bee_4xx_releases_the_hold(env, monkeypatch):
    """Bee refused the purchase after the 202: nothing was spent, so the hold
    goes back now instead of at midnight, and nothing is counted as spent."""
    app, fac, buy, find = env
    monkeypatch.setattr(settings, "SWARM_STAMP_PURCHASE_TIMEOUT_SECONDS", 0.05)
    req = httpx2.Request("POST", "http://bee/stamps/1/17")
    buy.side_effect = _slow_bee(httpx2.HTTPStatusError(
        "x", request=req, response=httpx2.Response(400, json={"message": "insufficient funds"}, request=req)))
    before = _spend_recorded()
    first, retry = _run_pending(app, None, key="k-refused-hold")
    assert first.status_code == 202
    assert retry.json()["code"] == "DELIVERY_FAILED_AFTER_PAYMENT"
    assert _gateway_spent() == 0
    assert _spend_recorded() == before


def test_background_bee_5xx_keeps_the_hold(env, monkeypatch):
    """A 5xx may have spent: the hold stays and is counted as uncertain."""
    from prometheus_client import REGISTRY
    app, fac, buy, find = env
    monkeypatch.setattr(settings, "SWARM_STAMP_PURCHASE_TIMEOUT_SECONDS", 0.05)
    req = httpx2.Request("POST", "http://bee/stamps/1/17")
    buy.side_effect = _slow_bee(httpx2.HTTPStatusError(
        "x", request=req, response=httpx2.Response(500, request=req)))
    uncertain = lambda: REGISTRY.get_sample_value(
        "gateway_spend_uncertain_bzz_total", {"operation": "stamp purchase"}) or 0.0
    before = uncertain()
    first, _ = _run_pending(app, None, key="k-5xx-hold")
    assert first.status_code == 202
    assert _gateway_spent() > 0
    assert uncertain() - before == pytest.approx(_gateway_spent())


def test_late_201_carries_expires_at(env, monkeypatch):
    """The 201 a keyed retry gets after a late delivery has expires_at, as the 201
    answered in time does: estimated from the amount at today's price."""
    app, fac, buy, find = env
    monkeypatch.setattr(settings, "SWARM_STAMP_PURCHASE_TIMEOUT_SECONDS", 0.05)
    buy.side_effect = _slow_bee(BATCH)
    first, retry = _run_pending(app, None, key="k-late-expiry")
    assert first.status_code == 202 and retry.status_code == 201
    amount = int(first.json()["amount"])
    assert retry.json()["expires_at"] == swarm_api.expiry_from_amount(amount, 24000)
    assert retry.json()["expires_at"] is not None


# --- round 2: every pending outcome settles the hold once (#468) ----------------

def _uncertain():
    from prometheus_client import REGISTRY
    return REGISTRY.get_sample_value("gateway_spend_uncertain_bzz_total", {"operation": "stamp purchase"}) or 0.0


def test_timeout_then_not_found_counts_the_hold_as_uncertain(env):
    """Bee timed out after the payment and the background lookup found nothing:
    the hold stays and is counted as uncertain, once."""
    app, fac, buy, find = env
    find.return_value = None
    before, spent_before = _uncertain(), _spend_recorded()
    first, retry = _run_pending(app, None, key="k-not-found-uncertain")
    assert first.status_code == 202
    assert retry.json()["code"] == "DELIVERY_FAILED_AFTER_PAYMENT"
    assert _gateway_spent() > 0
    assert _uncertain() - before == pytest.approx(_gateway_spent())
    assert _spend_recorded() == spent_before


def test_batch_taken_by_someone_else_counts_the_hold_as_uncertain(env):
    app, fac, buy, find = env
    stamp_ownership_manager._registry[BATCH] = {"owner": "0x" + "d4" * 20, "mode": "paid"}
    before = _uncertain()
    first, _ = _run_pending(app, None, key="k-taken-uncertain")
    assert first.status_code == 202
    assert _uncertain() - before == pytest.approx(_gateway_spent())
    assert _gateway_spent() > 0


def test_lookup_error_in_the_background_counts_the_hold_as_uncertain(env):
    app, fac, buy, find = env
    find.side_effect = [None, RuntimeError("node gone")]
    before = _uncertain()
    first, _ = _run_pending(app, None, key="k-lookup-error-uncertain")
    assert first.status_code == 202
    assert _uncertain() - before == pytest.approx(_gateway_spent())


def _cancelled_request_then_background(app, key):
    """A paid request cut off while Bee works; its background half finishes."""
    from app.api.endpoints import stamps

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://t") as c:
            h = {"X-PAYMENT": pay(), "Idempotency-Key": key}
            req = asyncio.ensure_future(c.post("/api/v1/stamps/", json=BODY, headers=h))
            for _ in range(200):           # until Bee's request is running
                if stamps._PENDING_TASKS:
                    break
                await asyncio.sleep(0.005)
            req.cancel()
            await asyncio.gather(req, return_exceptions=True)
            for _ in range(400):
                if not stamps._PENDING_TASKS:
                    break
                await asyncio.sleep(0.01)
            assert not stamps._PENDING_TASKS
    asyncio.run(run())


def test_cancelled_request_then_delivered_is_counted_once_as_spent(env, monkeypatch):
    """The request is cut off after the hand-off: only the background half
    settles the hold, so the cost is spent, never also uncertain."""
    app, fac, buy, find = env
    monkeypatch.setattr(settings, "SWARM_STAMP_PURCHASE_TIMEOUT_SECONDS", 30)
    buy.side_effect = _slow_bee(BATCH, delay=0.1)
    before, spent_before = _uncertain(), _spend_recorded()
    _cancelled_request_then_background(app, "k-cancel-delivered")
    assert stamp_ownership_manager.get_stamp_info(BATCH)["owner"] == PAYER
    assert _spend_recorded() - spent_before == pytest.approx(_gateway_spent())
    assert _uncertain() == before


def test_cancelled_request_then_bee_4xx_releases_without_counting(env, monkeypatch):
    app, fac, buy, find = env
    monkeypatch.setattr(settings, "SWARM_STAMP_PURCHASE_TIMEOUT_SECONDS", 30)
    req = httpx2.Request("POST", "http://bee/stamps/1/17")
    buy.side_effect = _slow_bee(httpx2.HTTPStatusError(
        "x", request=req, response=httpx2.Response(400, request=req)), delay=0.1)
    before = _uncertain()
    _cancelled_request_then_background(app, "k-cancel-4xx")
    assert _gateway_spent() == 0
    assert _uncertain() == before


def test_shutdown_during_the_late_expiry_read_still_stores_the_201(env, monkeypatch):
    """A cancel while the late 201's price is read must not leave the 202 for
    the Idempotency-Key: the 201 is stored, without expires_at."""
    from app.api.endpoints import stamps
    app, fac, buy, find = env
    monkeypatch.setattr(settings, "SWARM_STAMP_PURCHASE_TIMEOUT_SECONDS", 0.05)
    buy.side_effect = _slow_bee(BATCH)

    async def chainstate():
        # Only the background half reads the price after the batch is
        # registered: that is the read to hang.
        if stamp_ownership_manager.get_stamp_info(BATCH):
            await asyncio.sleep(3600)
        return {"currentPrice": "24000", "minimumValidityBlocks": 17280, "block": 180000}

    async def run():
        with patch("app.services.swarm_api.get_chainstate", new=chainstate):
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://t") as c:
                h = lambda: {"X-PAYMENT": pay(), "Idempotency-Key": "k-late-cancel"}
                first = await c.post("/api/v1/stamps/", json=BODY, headers=h())
                for _ in range(200):       # until the batch is registered
                    if stamp_ownership_manager.get_stamp_info(BATCH):
                        break
                    await asyncio.sleep(0.01)
                await asyncio.sleep(0.05)  # now waiting on the price read
                for t in list(stamps._PENDING_TASKS):
                    t.cancel()
                await asyncio.gather(*list(stamps._PENDING_TASKS), return_exceptions=True)
                retry = await c.post("/api/v1/stamps/", json=BODY, headers=h())
                return first, retry
    first, retry = asyncio.run(run())
    assert first.status_code == 202
    assert retry.status_code == 201, retry.text
    assert retry.json()["batchID"] == BATCH and retry.json()["expires_at"] is None


# --- round 3: the cancellation paths keep the hold and count it once (#468) -------

def test_background_cancelled_at_shutdown_counts_the_hold_once(env):
    """The background search is cancelled at shutdown: the hold stays and is
    counted as uncertain exactly once, by the background half."""
    from app.api.endpoints import stamps
    app, fac, buy, find = env
    calls = []

    async def lookup(*a, **kw):
        calls.append(1)
        if len(calls) > 1:              # the background search: still looking at shutdown
            await asyncio.sleep(3600)
        return None
    find.side_effect = lookup
    before, spent_before = _uncertain(), _spend_recorded()

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/api/v1/stamps/", json=BODY, headers={"X-PAYMENT": pay()})
            for _ in range(200):
                if len(calls) > 1:
                    break
                await asyncio.sleep(0.01)
            assert len(calls) > 1, "the background search is running"
            await stamps.drain_pending_purchases(0)
            return r
    assert asyncio.run(run()).status_code == 202
    assert "interrupted" in _audit("payment_failed")[0]["data"]["reason"]
    assert _gateway_spent() > 0                                   # the hold is kept
    assert _uncertain() - before == pytest.approx(_gateway_spent())   # counted once
    assert _spend_recorded() == spent_before


def _cancel_request_while(app, started):
    """Start a paid request, cancel it once `started()` is true, and return
    whether a background half was left running."""
    from app.api.endpoints import stamps

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://t") as c:
            req = asyncio.ensure_future(c.post("/api/v1/stamps/", json=BODY, headers={"X-PAYMENT": pay()}))
            for _ in range(400):
                if started():
                    break
                await asyncio.sleep(0.005)
            assert started(), "the request reached the point to cancel at"
            req.cancel()
            await asyncio.gather(req, return_exceptions=True)
            await asyncio.sleep(0.05)    # anything left behind would run now
            return bool(stamps._PENDING_TASKS)
    return asyncio.run(run())


def test_request_cancelled_during_the_in_handler_lookup_counts_the_hold_once(env):
    """Cancelled before any hand-off, while the handler itself looks for the
    batch: the handler keeps the hold and counts it once; nothing else does."""
    app, fac, buy, find = env
    looking = []

    async def lookup(*a, **kw):
        looking.append(1)
        await asyncio.sleep(3600)
    find.side_effect = lookup
    before, spent_before = _uncertain(), _spend_recorded()
    left_running = _cancel_request_while(app, lambda: bool(looking))
    assert not left_running                                      # nothing handed off
    assert len(looking) == 1
    assert _gateway_spent() > 0                                   # the hold is kept
    assert _uncertain() - before == pytest.approx(_gateway_spent())   # counted once
    assert _spend_recorded() == spent_before


def test_request_cancelled_during_settlement_counts_the_hold_once(env):
    """Cancelled while the payment is settling: the outcome is unknown, so the
    hold is kept and counted once, in the handler."""
    app, fac, buy, find = env
    settling = []

    async def settle(*a, **kw):
        settling.append(1)
        await asyncio.sleep(3600)
    fac.settle.side_effect = settle
    before, spent_before = _uncertain(), _spend_recorded()
    left_running = _cancel_request_while(app, lambda: bool(settling))
    assert not left_running
    buy.assert_not_called()
    assert _gateway_spent() > 0
    assert _uncertain() - before == pytest.approx(_gateway_spent())
    assert _spend_recorded() == spent_before
