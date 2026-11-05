"""Accepting, verifying and settling x402 v2 payments (#607), and #639.

Every paid route is driven with an x402 v1 (X-PAYMENT) and, with v2 enabled,
an x402 v2 (PAYMENT-SIGNATURE) payment, through the gateway's own facilitator
client on a mocked HTTP transport, so each test sees the exact /verify and
/settle bodies. The harness is the one of tests/test_x402_v1_golden.py
(real settings, real client) with the routes of
tests/test_x402_v2_payment_header.py, plus a paid route the middleware
settles after the response (no settle point of its own).
"""
import base64
import json

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException
from unittest.mock import AsyncMock, patch

from app.core.config import settings
from app.services.metrics import ensure_x402_version_series, x402_payments_total, x402_settlements_total
from app.x402 import codec_v2
from app.x402.dependency import require_x402_payment, settle_payment_if_offered
from app.x402.facilitator_client import FacilitatorClient
from app.x402.middleware import X402Middleware
from app.x402.ratelimit import reset_rate_limiter
from app.x402.settlement import replay_guard
from tests import test_x402_v1_golden as golden
from tests import test_x402_v2_payment_header as v2h
from tests.x402_v2_builders import (
    PAYER, authorization, encode, header_for, nonce_hex, other_receipt_header, pay,
    receipt_header, v1_payload, v2_accepted, v2_payload,
)

URL = "https://facilitator.example"
TX = "0x" + "ab" * 32
VERSIONS = [1, 2]
ROUTES = v2h.ALL
FALLBACK = ("POST", "/api/v1/data/fallback")   # settled by the middleware, after the response


async def _no_settle_handler(request: Request):
    v2h.RAN.append(getattr(request.state, "x402_mode", None))
    return {"reference": "e" * 64}


class Facilitator:
    """The facilitator's HTTP side. Answers in the version of the request."""

    def __init__(self):
        self.calls = []
        self.verify = None          # None: valid, with PAYER
        self.settle = None          # None: success
        self.settle_status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        self.calls.append({"path": path, "body": body})
        network = body["paymentRequirements"]["network"]
        if path == "verify":
            return httpx.Response(200, json=self.verify or {"isValid": True, "payer": PAYER})
        answer = self.settle or {"success": True, "transaction": TX, "network": network, "payer": PAYER}
        return httpx.Response(self.settle_status, json=answer)

    def paths(self):
        return [c["path"] for c in self.calls]


def _app(client):
    app = FastAPI()
    paid = APIRouter(dependencies=[Depends(require_x402_payment)])
    for method, path in v2h.PAID:
        route_path = "/api/v1/stamps/{stamp_id}/extend" if path.endswith("/extend") else path
        paid.add_api_route(route_path, v2h.paid_handler, methods=[method])
    paid.add_api_route(FALLBACK[1], _no_settle_handler, methods=["POST"])
    optional = APIRouter(dependencies=[Depends(settle_payment_if_offered)])
    optional.add_api_route(v2h.POOL[1], v2h.paid_handler, methods=["POST"])
    app.include_router(paid)
    app.include_router(optional)
    app.add_middleware(X402Middleware, facilitator_client=client)
    from app.main import http_exception_handler
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    return app


@pytest.fixture
def gw(monkeypatch):
    stack = []

    def build(versions="1,2", free_tier=True, network="base-sepolia"):
        golden.configure(monkeypatch, network, free_tier=free_tier)
        monkeypatch.setattr(settings, "X402_PROTOCOL_VERSIONS", versions)
        reset_rate_limiter()
        v2h.RAN.clear()
        fac = Facilitator()
        client = FacilitatorClient({"url": URL}, transport=httpx.MockTransport(fac.handler))
        for p in (patch("app.x402.dependency._calculate_price_for_request",
                        new=AsyncMock(return_value={"price_usd": 0.02, "description": "Test operation"})),
                  patch("app.x402.dependency._get_facilitator_client", return_value=client)):
            p.start()
            stack.append(p)
        return TestClient(_app(client), raise_server_exceptions=False), fac

    yield build
    for p in reversed(stack):
        p.stop()
    reset_rate_limiter()
    v2h.RAN.clear()


def _offer(client):
    """The gateway's v1 offer (accepts[0]) for the priced request."""
    r = client.post("/api/v1/stamps/")
    assert r.status_code == 402, r.text
    return r.json()["accepts"][0]


def _send(client, route, headers):
    return client.request(route[0], route[1], headers=headers)


def _settled(version):
    return x402_settlements_total.labels(result="settled", version=str(version))._value.get()


def _v1_body_402(r):
    body = r.json()
    assert body["x402Version"] == 1 and len(body["accepts"]) == 1
    assert body["accepts"][0]["network"] == "base-sepolia"
    assert "PAYMENT-REQUIRED" not in r.headers      # #608
    return body


# --------------------------------------------------------------------------
# Every paid route, in both versions
# --------------------------------------------------------------------------

@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("route", ROUTES + [FALLBACK], ids=lambda r: f"{r[0]} {r[1]}")
def test_every_paid_route_settles_in_its_version(gw, route, version):
    client, fac = gw("1,2")
    offer = _offer(client)
    before = _settled(version)
    r = _send(client, route, pay(version, offer))
    assert r.status_code == 200, r.text
    assert v2h.RAN == ["paid"]
    assert fac.paths() == ["verify", "settle"]
    for call in fac.calls:
        assert call["body"]["x402Version"] == version
        if version == 2:
            assert call["body"]["paymentRequirements"] == v2_accepted(offer)
            assert call["body"]["paymentPayload"]["accepted"] == v2_accepted(offer)
        else:
            assert call["body"]["paymentRequirements"]["network"] == "base-sepolia"
    assert receipt_header(version) in r.headers
    assert other_receipt_header(version) not in r.headers
    if version == 2:
        receipt = codec_v2.decode_payment_response(r.headers["PAYMENT-RESPONSE"])
        assert receipt.success is True and receipt.transaction == TX and receipt.network == "eip155:84532"
    assert r.headers["X-Payment-Transaction"] == TX
    assert _settled(version) == before + 1


@pytest.mark.parametrize("network,caip2", [("base", "eip155:8453"), ("eip155:84532", "eip155:84532")])
def test_v2_offer_uses_the_caip2_network(gw, network, caip2):
    client, fac = gw("1,2", network=network)
    offer = _offer(client)
    r = client.post("/api/v1/stamps/", headers=pay(2, offer))
    assert r.status_code == 200, r.text
    assert fac.calls[0]["body"]["paymentRequirements"]["network"] == caip2


def test_v2_payment_never_takes_the_free_tier(gw):
    client, fac = gw("1,2", free_tier=True)
    offer = _offer(client)
    r = client.post("/api/v1/stamps/", headers={**pay(2, offer), "X-Payment-Mode": "free"})
    assert r.status_code == 200 and r.json() == {"mode": "paid"}
    assert fac.paths() == ["verify", "settle"]


def test_v2_settle_sends_extensions_nowhere(gw):
    client, fac = gw("1,2")
    offer = _offer(client)
    payload = v2_payload(offer, authorization(offer), extensions={"eip2612GasSponsoring": {"on": True}},
                         unknownTop="x")
    r = client.post("/api/v1/stamps/", headers=header_for(2, payload))
    assert r.status_code == 200, r.text
    for call in fac.calls:
        assert set(call["body"]["paymentPayload"]) == {"x402Version", "payload", "resource", "accepted"}


# --------------------------------------------------------------------------
# Refusals of a v2 payment, all before the facilitator
# --------------------------------------------------------------------------

def _refused_402(r, code):
    assert r.status_code == 402, r.text
    body = _v1_body_402(r)
    assert body["code"] == code and body["detail"]["code"] == code
    return body


@pytest.mark.parametrize("route", ROUTES, ids=lambda r: f"{r[0]} {r[1]}")
def test_v2_header_errors_get_400(gw, route):
    client, fac = gw("1,2")
    for value, code in [
        ("A" * 8200, "PAYMENT_HEADER_TOO_LARGE"),
        ("", "PAYMENT_HEADER_MALFORMED"),
        ("not base64!", "PAYMENT_HEADER_MALFORMED"),
        (encode({"x402Version": 2}), "PAYMENT_HEADER_INVALID"),
        (encode({"accepted": {}, "payload": {}}), "PAYMENT_HEADER_INVALID"),
    ]:
        r = _send(client, route, {"PAYMENT-SIGNATURE": value})
        assert r.status_code == 400, (code, r.text)
        assert r.json()["code"] == code
        assert "accepts" not in r.json()
    assert fac.calls == [] and v2h.RAN == []


@pytest.mark.parametrize("claimed", [1, 3, 0])
def test_v2_header_with_another_payload_version_gets_402(gw, claimed):
    client, fac = gw("1,2")
    offer = _offer(client)
    r = client.post("/api/v1/stamps/", headers=header_for(2, v2_payload(offer, authorization(offer),
                                                                         x402_version=claimed)))
    _refused_402(r, "X402_VERSION_UNSUPPORTED")
    assert fac.calls == []


def test_non_exact_scheme_is_refused_before_the_facilitator(gw):
    client, fac = gw("1,2")
    offer = _offer(client)
    accepted = dict(v2_accepted(offer), scheme="upto")
    r = client.post("/api/v1/stamps/", headers=header_for(2, v2_payload(offer, authorization(offer), accepted)))
    _refused_402(r, "PAYMENT_SCHEME_UNSUPPORTED")
    assert fac.calls == []


def test_permit2_payload_is_refused_as_a_method(gw):
    client, fac = gw("1,2")
    offer = _offer(client)
    payload = v2_payload(offer, authorization(offer))
    payload["payload"] = {"signature": "0x" + "ab" * 65, "permit2Authorization": {"from": PAYER}}
    r = client.post("/api/v1/stamps/", headers=header_for(2, payload))
    _refused_402(r, "PAYMENT_METHOD_UNSUPPORTED")
    assert fac.calls == []


@pytest.mark.parametrize("field,value", [
    ("network", "eip155:8453"),
    ("asset", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"),
    ("amount", "1"),
    ("amount", "00020000"),                       # same number, not the same string
    ("payTo", "0x" + "33" * 20),
    ("maxTimeoutSeconds", 600),
    ("extra", {"name": "USD Coin", "version": "2"}),
    ("extra", None),
])
def test_accepted_must_equal_the_gateway_offer(gw, field, value):
    client, fac = gw("1,2")
    offer = _offer(client)
    accepted = v2_accepted(offer)
    if value is None:
        accepted.pop(field)
    else:
        accepted[field] = value
    r = client.post("/api/v1/stamps/", headers=header_for(2, v2_payload(offer, authorization(offer), accepted)))
    body = _refused_402(r, "PAYMENT_OFFER_MISMATCH")
    assert body["fields"] == [field]
    assert str(value) not in body["message"] or value in (None,)
    assert fac.calls == []


def test_address_letter_case_must_match_too(gw):
    client, fac = gw("1,2")
    offer = _offer(client)
    accepted = dict(v2_accepted(offer), asset=offer["asset"].lower())
    r = client.post("/api/v1/stamps/", headers=header_for(2, v2_payload(offer, authorization(offer), accepted)))
    assert _refused_402(r, "PAYMENT_OFFER_MISMATCH")["fields"] == ["asset"]


# --------------------------------------------------------------------------
# The setting
# --------------------------------------------------------------------------

def test_default_setting_still_refuses_v2(gw):
    client, fac = gw("1")
    offer = _offer(client)
    r = client.post("/api/v1/stamps/", headers=pay(2, offer))
    _refused_402(r, "X402_VERSION_UNSUPPORTED")
    assert fac.calls == []


def test_v2_only_refuses_x_payment(gw):
    client, fac = gw("2")
    offer = _offer(client)
    before = x402_payments_total.labels(mode="version_unsupported", version="1")._value.get()
    r = client.post("/api/v1/stamps/", headers=pay(1, offer))
    body = _refused_402(r, "X402_VERSION_UNSUPPORTED")
    assert "PAYMENT-SIGNATURE" in body["message"]
    assert x402_payments_total.labels(mode="version_unsupported", version="1")._value.get() == before + 1
    assert fac.calls == []
    r = client.post("/api/v1/stamps/", headers=pay(2, offer, nonce=nonce_hex("02")))
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------
# #639: an X-PAYMENT whose payload claims another version
# --------------------------------------------------------------------------

@pytest.mark.parametrize("versions", ["1", "1,2"])
@pytest.mark.parametrize("claimed", [2, 0, 3])
def test_x_payment_with_another_payload_version_gets_400(gw, versions, claimed):
    client, fac = gw(versions)
    offer = _offer(client)
    r = client.post("/api/v1/stamps/", headers=header_for(1, v1_payload(offer, authorization(offer),
                                                                         x402_version=claimed)))
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "PAYMENT_HEADER_INVALID"
    assert "accepts" not in r.json()
    assert fac.calls == [] and v2h.RAN == []


def test_x_payment_with_version_1_as_text_is_still_accepted(gw):
    client, fac = gw("1")
    offer = _offer(client)
    r = client.post("/api/v1/stamps/", headers=header_for(1, v1_payload(offer, authorization(offer),
                                                                         x402_version="1")))
    assert r.status_code == 200, r.text
    assert fac.calls[0]["body"]["x402Version"] == 1


# --------------------------------------------------------------------------
# Outcomes, in both versions
# --------------------------------------------------------------------------

@pytest.mark.parametrize("version", VERSIONS)
def test_verify_invalid_gets_402(gw, version):
    client, fac = gw("1,2")
    offer = _offer(client)
    fac.verify = {"isValid": False, "invalidReason": "insufficient_funds", "payer": PAYER}
    r = client.post("/api/v1/stamps/", headers=pay(version, offer))
    assert r.status_code == 402
    assert "insufficient_funds" in _v1_body_402(r)["error"]
    assert fac.paths() == ["verify"] and v2h.RAN == []


@pytest.mark.parametrize("version,status", [(1, 200), (2, 200), (2, 400)])
def test_settle_refused_gets_402_settlement_failed(gw, version, status):
    client, fac = gw("1,2")
    offer = _offer(client)
    fac.settle_status = status
    fac.settle = {"success": False, "errorReason": "invalid_signature", "transaction": "",
                  "network": "base-sepolia" if version == 1 else "eip155:84532"}
    before = x402_settlements_total.labels(result="refused", version=str(version))._value.get()
    r = client.post("/api/v1/stamps/", headers=pay(version, offer))
    assert r.status_code == 402, r.text
    assert r.json()["code"] == "PAYMENT_SETTLEMENT_FAILED"
    _v1_body_402(r)
    assert v2h.RAN == []
    assert x402_settlements_total.labels(result="refused", version=str(version))._value.get() == before + 1
    # Nothing collected: the same authorization may be tried again.
    fac.settle, fac.settle_status = None, 200
    assert client.post("/api/v1/stamps/", headers=pay(version, offer)).status_code == 200


@pytest.mark.parametrize("status", [500, 502, 503])
def test_v2_settle_5xx_is_an_unknown_outcome(gw, status):
    client, fac = gw("1,2")
    offer = _offer(client)
    fac.settle_status = status
    fac.settle = {"success": False, "errorReason": "busy", "transaction": "", "network": "eip155:84532"}
    before = x402_settlements_total.labels(result="error", version="2")._value.get()
    r = client.post("/api/v1/stamps/", headers=pay(2, offer))
    assert r.status_code == 502, r.text
    assert r.json()["code"] == "PAYMENT_SETTLEMENT_UNAVAILABLE"
    assert x402_settlements_total.labels(result="error", version="2")._value.get() == before + 1
    # The same authorization again, in either envelope: not "sign a new payment".
    fac.settle, fac.settle_status = None, 200
    for version in (2, 1):
        fac.verify = {"isValid": False, "invalidReason": "nonce_used", "payer": PAYER}
        r = client.post("/api/v1/stamps/", headers=pay(version, offer))
        assert r.status_code == 409, r.text
        assert r.json()["code"] == "PAYMENT_AUTHORIZATION_OUTCOME_UNKNOWN"


@pytest.mark.parametrize("version", VERSIONS)
def test_fallback_settle_without_answer_withholds_the_result(gw, version):
    client, fac = gw("1,2")
    offer = _offer(client)
    fac.settle_status, fac.settle = 500, {"oops": True}
    r = _send(client, FALLBACK, pay(version, offer))
    assert r.status_code == 502, r.text
    assert r.json()["code"] == "PAYMENT_SETTLEMENT_UNKNOWN"
    assert receipt_header(version) not in r.headers


# --------------------------------------------------------------------------
# Replay guard (D5): one signature, either envelope
# --------------------------------------------------------------------------

@pytest.mark.parametrize("first,second", [(1, 2), (2, 1)])
def test_one_signature_cannot_be_replayed_in_the_other_version(gw, first, second):
    client, fac = gw("1,2")
    offer = _offer(client)
    assert client.post("/api/v1/stamps/", headers=pay(first, offer)).status_code == 200
    settles = fac.paths().count("settle")
    r = client.post("/api/v1/stamps/", headers=pay(second, offer))     # same from + nonce
    assert r.status_code == 402
    assert "already been used" in r.json()["error"]
    assert fac.paths().count("settle") == settles
    assert v2h.RAN == ["paid"]


@pytest.mark.parametrize("first,second", [(1, 2), (2, 1)])
def test_cross_version_replay_after_the_guard_forgets_is_still_refused(gw, first, second):
    """Past the reservation window (or a restart), the Idempotency-Key store
    still knows the authorization that paid, by its version-neutral hash."""
    client, fac = gw("1,2")
    offer = _offer(client)
    key = {"Idempotency-Key": "k-replay"}
    assert client.post("/api/v1/stamps/", headers={**pay(first, offer), **key}).status_code == 200
    replay_guard.reset()
    r = client.post("/api/v1/stamps/", headers={**pay(second, offer), **key})
    assert r.status_code == 402 and "already been used" in r.json()["error"]
    assert fac.paths().count("settle") == 1


# --------------------------------------------------------------------------
# Idempotency (D4)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("version", VERSIONS)
def test_replay_returns_only_its_versions_receipt(gw, version):
    client, fac = gw("1,2")
    offer = _offer(client)
    key = {"Idempotency-Key": "k-1"}
    first = client.post("/api/v1/stamps/", headers={**pay(version, offer), **key})
    assert first.status_code == 200
    again = client.post("/api/v1/stamps/", headers={**pay(version, offer, nonce=nonce_hex("02")), **key})
    assert again.status_code == 200, again.text
    assert again.headers["Idempotent-Replayed"] == "true"
    assert again.headers[receipt_header(version)] == first.headers[receipt_header(version)]
    assert other_receipt_header(version) not in again.headers
    assert fac.paths() == ["verify", "settle", "verify"]


@pytest.mark.parametrize("first,second", [(1, 2), (2, 1)])
def test_replay_in_the_other_version_gets_409(gw, first, second):
    client, fac = gw("1,2")
    offer = _offer(client)
    key = {"Idempotency-Key": "k-2"}
    assert client.post("/api/v1/stamps/", headers={**pay(first, offer), **key}).status_code == 200
    other = pay(second, offer, nonce=nonce_hex("03"))
    r = client.post("/api/v1/stamps/", headers={**other, **key})
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["code"] == "IDEMPOTENCY_KEY_VERSION_MISMATCH"
    assert body["detail"]["original_x402_version"] == first
    assert ("X-PAYMENT" if first == 1 else "PAYMENT-SIGNATURE") in body["message"]
    assert "X-PAYMENT-RESPONSE" not in r.headers and "PAYMENT-RESPONSE" not in r.headers
    assert fac.paths().count("settle") == 1
    # Not charged, and the new authorization was released: it can pay for something else.
    assert client.post("/api/v1/stamps/", headers=other).status_code == 200
    # The original version still gets the stored result.
    r = client.post("/api/v1/stamps/", headers={**pay(first, offer, nonce=nonce_hex("04")), **key})
    assert r.status_code == 200 and r.headers["Idempotent-Replayed"] == "true"


def test_version_is_stored_for_v2_only(gw):
    from app.x402.idempotency import idempotency_store
    client, _ = gw("1,2")
    offer = _offer(client)
    client.post("/api/v1/stamps/", headers={**pay(1, offer), "Idempotency-Key": "a"})
    client.post("/api/v1/stamps/", headers={**pay(2, offer, nonce=nonce_hex("05")), "Idempotency-Key": "b"})
    entries = list(idempotency_store._entries.values())
    assert sorted(e.get("version") for e in entries if e.get("version")) == [2]
    assert sum("version" not in e for e in entries) == 1
    v1 = next(e for e in entries if "version" not in e)
    assert "x-payment-response" in v1["headers"] and "payment-response" not in v1["headers"]


def test_settlement_unknown_entries_record_version_2(gw):
    from app.x402.settlement import outcome_unknown
    client, fac = gw("1,2")
    offer = _offer(client)
    fac.settle_status, fac.settle = 500, {"oops": True}    # unparsable in either version
    for version, nonce in ((1, "06"), (2, "07")):
        client.post("/api/v1/stamps/", headers=pay(version, offer, nonce=nonce_hex(nonce)))
    kinds = sorted(e.get("version", 1) for e in outcome_unknown._entries.values())
    assert kinds == [1, 2]


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------

def _series(metric):
    return {tuple(sorted(s.labels.items())) for m in metric.collect() for s in m.samples
            if s.name.endswith("_total")}


def test_v2_settlement_series_exist_once_enabled():
    ensure_x402_version_series((1, 2))
    series = _series(x402_settlements_total)
    for result in ("settled", "refused", "error", "settled_not_delivered", "refused_after_unknown"):
        assert (("result", result), ("version", "2")) in series


def test_v1_only_creates_no_v2_series_at_import():
    import subprocess
    import sys
    code = ("from app.services.metrics import x402_settlements_total as m;"
            "print(sorted({s.labels['version'] for x in m.collect() for s in x.samples}))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={**__import__("os").environ, "X402_PROTOCOL_VERSIONS": "1",
                              "SWARM_BEE_API_URL": "http://bee.invalid:1633"})
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "['1']"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={**__import__("os").environ, "X402_PROTOCOL_VERSIONS": "1,2",
                              "SWARM_BEE_API_URL": "http://bee.invalid:1633"})
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "['1', '2']"


def test_receipt_is_codec_v2_of_the_typed_settle_response(gw):
    client, fac = gw("1,2")
    offer = _offer(client)
    r = client.post("/api/v1/stamps/", headers=pay(2, offer))
    raw = json.loads(base64.b64decode(r.headers["PAYMENT-RESPONSE"]))
    assert raw == {"success": True, "transaction": TX, "network": "eip155:84532", "payer": PAYER}


# --------------------------------------------------------------------------
# The Bee call gate
# --------------------------------------------------------------------------

def test_bee_gate_keeps_an_answer_carrying_payment_response():
    """A v2 receipt marks an answer about a payment, as X-PAYMENT-RESPONSE does:
    the gate never replaces it with 503 BEE_BUSY ("nothing was charged")."""
    from fastapi import HTTPException
    from app.middleware.bee_gate import BeeGateMiddleware
    from tests.test_bee_gate_payments import assert_not_rewritten, swallow_a_refusal

    app = FastAPI()

    @app.get("/v2-receipt-502")
    async def receipt():
        await swallow_a_refusal()
        raise HTTPException(status_code=502, detail="x", headers={"PAYMENT-RESPONSE": "e30="})

    @app.get("/plain-502")
    async def plain():
        await swallow_a_refusal()
        raise HTTPException(status_code=502, detail="Bee node request failed")

    app.add_middleware(BeeGateMiddleware)
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/v2-receipt-502")
    assert r.status_code == 502 and "retry-after" not in r.headers
    assert_not_rewritten(r)
    assert client.get("/plain-502").json()["code"] == "BEE_BUSY"    # the control


# --------------------------------------------------------------------------
# Deeply nested values (#645 review)
# --------------------------------------------------------------------------

def _nested(depth):
    value = "x"
    for _ in range(depth):
        value = {"a": value}
    return value


def test_deeply_nested_extra_gets_400_from_the_codec(gw):
    client, fac = gw("1,2")
    offer = _offer(client)
    accepted = dict(v2_accepted(offer), extra=_nested(400))
    payload = v2_payload(offer, authorization(offer), accepted)
    # json.dumps recurses too; build the 400-deep JSON text by hand.
    text = json.dumps(dict(payload, accepted=dict(accepted, extra="@@")), separators=(",", ":"))
    text = text.replace('"@@"', '{"a":' * 400 + '"x"' + "}" * 400)
    r = client.post("/api/v1/stamps/", headers={"PAYMENT-SIGNATURE": base64.b64encode(text.encode()).decode()})
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "PAYMENT_HEADER_MALFORMED"
    assert fac.calls == [] and v2h.RAN == []


def test_an_offer_that_cannot_be_dumped_gets_400_not_500(gw):
    """Past the codec's depth limit (a model built directly): still 400."""
    from fastapi import HTTPException
    from app.x402 import v2_payment
    from app.x402.models_v1 import PaymentRequirements
    from app.x402.models_v2 import PaymentPayloadV2
    client, fac = gw("1,2")
    offer = _offer(client)
    deep = {}
    node = deep
    for _ in range(5000):
        node["a"] = {}
        node = node["a"]
    payment = PaymentPayloadV2.model_validate(v2_payload(offer, authorization(offer)))
    payment.accepted.extra = deep
    requirements = PaymentRequirements.model_validate(offer)
    with patch("app.x402.v2_payment.decode_v2_payment", return_value=payment):
        with pytest.raises(HTTPException) as exc:
            v2_payment.read_v2_payment("ignored", requirements)
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "PAYMENT_HEADER_INVALID"
    with patch("app.x402.v2_payment.decode_v2_payment", return_value=payment):
        r = client.post("/api/v1/stamps/", headers={"PAYMENT-SIGNATURE": "e30="})
    assert r.status_code == 400 and r.json()["code"] == "PAYMENT_HEADER_INVALID"
    assert fac.calls == []


# --------------------------------------------------------------------------
# The payment gate (#589) lets a v2 retry of a possibly-charged payment through
# --------------------------------------------------------------------------

def _gate_closed():
    from tests.test_bee_unreachable_589 import _closed
    _closed()


@pytest.mark.parametrize("version", VERSIONS)
def test_same_key_retry_gets_its_stored_result_while_payments_are_refused(gw, version):
    client, fac = gw("1,2")
    offer = _offer(client)
    key = {"Idempotency-Key": "k-gate"}
    first = client.post("/api/v1/stamps/", headers={**pay(version, offer), **key})
    assert first.status_code == 200
    _gate_closed()
    retry = client.post("/api/v1/stamps/", headers={**pay(version, offer, nonce=nonce_hex("09")), **key})
    assert retry.status_code == 200, retry.text
    assert retry.headers["Idempotent-Replayed"] == "true"
    assert receipt_header(version) in retry.headers
    assert fac.paths().count("settle") == 1


@pytest.mark.parametrize("version", VERSIONS)
def test_after_work_unknown_outcome_retry_gets_its_409_while_payments_are_refused(gw, version):
    from app.x402.settlement import AFTER_WORK, outcome_unknown
    client, fac = gw("1,2")
    offer = _offer(client)
    nonce = nonce_hex("5e")
    outcome_unknown.add((PAYER.lower(), nonce), AFTER_WORK)
    _gate_closed()
    r = client.post("/api/v1/stamps/", headers=pay(version, offer, nonce=nonce))
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "PAYMENT_AUTHORIZATION_OUTCOME_UNKNOWN"
    assert "settle" not in fac.paths()


@pytest.mark.parametrize("version", VERSIONS)
def test_reserved_authorization_retry_gets_its_answer_while_payments_are_refused(gw, version):
    client, fac = gw("1,2")
    offer = _offer(client)
    nonce = nonce_hex("7a")
    assert replay_guard.reserve((PAYER.lower(), nonce))
    _gate_closed()
    r = client.post("/api/v1/stamps/", headers=pay(version, offer, nonce=nonce))
    assert r.status_code == 402, r.text
    assert "already been used" in r.json()["error"]
    assert "settle" not in fac.paths()


@pytest.mark.parametrize("headers", [
    {"PAYMENT-SIGNATURE": "not base64!"},          # cannot be decoded: not a retry
    {"PAYMENT-SIGNATURE": encode({"x402Version": 2})},
])
def test_undecodable_v2_header_is_not_a_retry(gw, headers):
    client, fac = gw("1,2")
    _gate_closed()
    r = client.post("/api/v1/stamps/", headers=headers)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "PAYMENTS_UNAVAILABLE"
    assert fac.calls == []


def test_v2_header_is_not_read_while_v2_is_off(gw):
    client, fac = gw("1")
    offer = _offer(client)
    nonce = nonce_hex("7c")
    assert replay_guard.reserve((PAYER.lower(), nonce))
    _gate_closed()
    r = client.post("/api/v1/stamps/", headers=pay(2, offer, nonce=nonce))
    assert r.status_code == 503 and r.json()["detail"]["code"] == "PAYMENTS_UNAVAILABLE"
    assert fac.calls == []


# --------------------------------------------------------------------------
# A pruned v2 claim leaves no version behind (#645 review)
# --------------------------------------------------------------------------

def test_pruned_v2_claim_does_not_label_a_later_v1_entry(monkeypatch):
    from starlette.responses import Response
    from app.x402 import idempotency
    from app.x402.idempotency import DONE, IN_FLIGHT_TIMEOUT_SECONDS, idempotency_store as store
    eid = idempotency.entry_id(PAYER, "POST", "/api/v1/stamps/", "k-pruned")
    now = [1_000_000.0]
    monkeypatch.setattr(idempotency.time, "time", lambda: now[0])
    state, _, _ = store.begin(eid, "h", "auth-v2", version=2)       # a v2 claim that never ends
    assert state == "new"
    now[0] += IN_FLIGHT_TIMEOUT_SECONDS + 1                           # its claim is pruned
    state, _, token = store.begin(eid, "h", "auth-v1", version=1)
    assert state == "new"
    assert eid not in store._versions
    store.mark_settled(eid, token, "auth-v1", "0x" + "ab" * 32, persist="none")
    store.complete(eid, token, Response(b"{}", status_code=200, headers={"X-PAYMENT-RESPONSE": "r1"}),
                   persist="none")
    entry = store._entries[eid]
    assert entry["state"] == DONE and "version" not in entry
    assert entry["headers"]["x-payment-response"] == "r1" and "payment-response" not in entry["headers"]
    # A v1 retry is replayed with the v1 receipt, not refused as another version.
    state, replay, _ = store.begin(eid, "h", "auth-v1-retry", version=1)
    assert state == DONE
    assert idempotency._replay_response(replay, PAYER).headers["x-payment-response"] == "r1"


def test_prune_drops_markers_of_expired_claims(monkeypatch):
    from app.x402 import idempotency
    from app.x402.idempotency import IN_FLIGHT_TIMEOUT_SECONDS, idempotency_store as store
    now = [2_000_000.0]
    monkeypatch.setattr(idempotency.time, "time", lambda: now[0])
    eid = idempotency.entry_id(PAYER, "POST", "/api/v1/stamps/", "k-prune-2")
    store.begin(eid, "h", "a", version=2)
    assert store._versions[eid] == 2
    now[0] += IN_FLIGHT_TIMEOUT_SECONDS + 1
    other = idempotency.entry_id(PAYER, "POST", "/api/v1/stamps/", "k-other")
    store.begin(other, "h", "b", version=1)                            # prunes on the way
    assert eid not in store._versions


@pytest.mark.parametrize("mode", ["free", "free-tier", "FREE-TIER"])
@pytest.mark.parametrize("route", [("POST", "/api/v1/stamps/"), v2h.POOL], ids=lambda r: r[1])
def test_v2_payment_with_free_mode_is_refused_while_payments_are_unavailable(gw, mode, route):
    """Free mode exempts free-tier requests from the #589 gate; a v2 payment
    ignores free mode, so it must not be exempted (#645 review)."""
    client, fac = gw("1,2", free_tier=True)
    offer = _offer(client)
    _gate_closed()
    r = _send(client, route, {**pay(2, offer), "X-Payment-Mode": mode})
    assert r.status_code == 503, r.text
    assert r.json()["detail"]["code"] == "PAYMENTS_UNAVAILABLE"
    assert fac.calls == [] and v2h.RAN == []


def test_free_mode_without_a_payment_still_passes_the_closed_gate(gw):
    client, fac = gw("1,2", free_tier=True)
    _gate_closed()
    r = client.post("/api/v1/stamps/", headers={"X-Payment-Mode": "free"})
    assert r.status_code == 200 and r.json() == {"mode": "free-tier"}
    assert fac.calls == []


# --------------------------------------------------------------------------
# The v2 receipt is the gateway's, from fixed fields (#645 review)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("route", [("POST", "/api/v1/stamps/"), FALLBACK], ids=["settle_payment", "fallback"])
def test_large_facilitator_extra_still_gives_the_result_and_a_receipt(gw, route):
    client, fac = gw("1,2")
    offer = _offer(client)
    fac.settle = {"success": True, "transaction": TX, "network": "eip155:84532", "payer": PAYER,
                  "amount": "20000", "extra": {"blob": "x" * 9000},
                  "extensions": {"e": "y" * 100}, "extensionResponses": {"r": {"z": 1}}}
    r = _send(client, route, pay(2, offer))
    assert r.status_code == 200, r.text
    receipt = json.loads(base64.b64decode(r.headers["PAYMENT-RESPONSE"]))
    assert receipt == {"success": True, "transaction": TX, "network": "eip155:84532", "payer": PAYER,
                       "amount": "20000"}
    assert codec_v2.decode_payment_response(r.headers["PAYMENT-RESPONSE"]).success is True


def test_receipt_falls_back_to_the_minimal_fields_then_to_none():
    from app.x402.middleware import encode_payment_response_v2
    from app.x402.models_v2 import SettleResponseV2
    big_payer = SettleResponseV2(success=True, transaction=TX, network="eip155:84532", payer="p" * 9000)
    got = json.loads(base64.b64decode(encode_payment_response_v2(big_payer)))
    assert got == {"success": True, "transaction": TX, "network": "eip155:84532"}
    hopeless = SettleResponseV2(success=True, transaction="t" * 9000, network="eip155:84532")
    assert encode_payment_response_v2(hopeless) is None


def test_unencodable_receipt_still_delivers_without_the_header(gw):
    client, fac = gw("1,2")
    offer = _offer(client)
    fac.settle = {"success": True, "transaction": "0x" + "ab" * 4600, "network": "eip155:84532"}
    r = client.post("/api/v1/stamps/", headers=pay(2, offer))
    assert r.status_code == 200, r.text
    assert "PAYMENT-RESPONSE" not in r.headers and "X-PAYMENT-RESPONSE" not in r.headers
    assert r.headers["X-Payment-Transaction"] == "0x" + "ab" * 4600


def test_v2_verify_5xx_saying_valid_gets_502_and_nothing_runs(gw):
    client, fac = gw("1,2")
    offer = _offer(client)
    real = fac.handler

    def handler(request):
        if request.url.path.endswith("/verify"):
            fac.calls.append({"path": "verify", "body": json.loads(request.content)})
            return httpx.Response(500, json={"isValid": True, "payer": PAYER})
        return real(request)
    fac.handler = handler
    client2 = FacilitatorClient({"url": URL}, transport=httpx.MockTransport(handler))
    with patch("app.x402.dependency._get_facilitator_client", return_value=client2):
        r = client.post("/api/v1/stamps/", headers=pay(2, offer))
    assert r.status_code == 502, r.text
    assert fac.paths() == ["verify"] and v2h.RAN == []


# --------------------------------------------------------------------------
# Hardening: the forwarded resource and the signed authorization (#645 review)
# --------------------------------------------------------------------------

def test_forwarded_resource_is_the_gateways_own(gw):
    client, fac = gw("1,2")
    offer = _offer(client)
    payload = v2_payload(offer, authorization(offer))
    payload["resource"] = {"url": "https://evil.example/x", "description": "anything the client says",
                           "mimeType": "text/html", "serviceName": "Evil"}
    r = client.post("/api/v1/stamps/", headers=header_for(2, payload))
    assert r.status_code == 200, r.text
    for call in fac.calls:
        assert call["body"]["paymentPayload"]["resource"] == {
            "url": offer["resource"], "description": offer["description"], "mimeType": offer["mimeType"]}


def test_missing_resource_gets_the_gateways_own(gw):
    client, fac = gw("1,2")
    offer = _offer(client)
    payload = v2_payload(offer, authorization(offer))
    del payload["resource"]
    assert client.post("/api/v1/stamps/", headers=header_for(2, payload)).status_code == 200
    assert fac.calls[0]["body"]["paymentPayload"]["resource"]["url"] == offer["resource"]


@pytest.mark.parametrize("field,value", [
    ("to", "0x" + "33" * 20),
    ("value", "1"),
    ("value", "020000"),                           # the same number, not the same string
    ("to", "0x" + "22" * 19 + "2A"),               # another address in mixed case
])
def test_authorization_must_pay_the_offer(gw, field, value):
    client, fac = gw("1,2")
    offer = _offer(client)
    auth = dict(authorization(offer), **{field: value})
    r = client.post("/api/v1/stamps/", headers=header_for(2, v2_payload(offer, auth)))
    body = _refused_402(r, "PAYMENT_OFFER_MISMATCH")
    assert body["fields"] == [f"payload.authorization.{field}"]
    assert value not in body["message"]
    assert fac.calls == []


def test_authorization_to_in_another_letter_case_is_the_same_address(gw, monkeypatch):
    from eth_utils import to_checksum_address
    client, fac = gw("1,2")
    checksummed = to_checksum_address("0x" + "ab" * 20)
    monkeypatch.setattr(settings, "X402_PAY_TO_ADDRESS", checksummed)
    offer = _offer(client)
    for i, to in enumerate((checksummed.lower(), "0x" + checksummed[2:].upper(), checksummed)):
        auth = dict(authorization(offer, nonce=nonce_hex(f"c{i}")), to=to)
        r = client.post("/api/v1/stamps/", headers=header_for(2, v2_payload(offer, auth)))
        assert r.status_code == 200, (to, r.text)
    bad = checksummed[:2] + checksummed[2:].swapcase()   # mixed case, wrong checksum
    auth = dict(authorization(offer, nonce=nonce_hex("0f")), to=bad)
    r = client.post("/api/v1/stamps/", headers=header_for(2, v2_payload(offer, auth)))
    assert _refused_402(r, "PAYMENT_OFFER_MISMATCH")["fields"] == ["payload.authorization.to"]
