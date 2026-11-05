"""The gateway's facilitator client with x402 v2 payments (#607).

- The request body is {"x402Version": 2, paymentPayload, paymentRequirements},
  dumped by alias without None fields; unknown fields and `extensions` are
  never sent.
- Answers: verify takes a 2xx body, or a 4xx body with isValid:false, and
  raises for anything else; settle takes a 2xx body, a 4xx body with
  success:false (a definite refusal), and raises for anything else (an
  unknown outcome): a 5xx, a non-2xx success:true, an unparsable body.
- v1 and v2 types cannot be mixed.
"""
import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from app.x402.facilitator_client import FacilitatorAnswerUnusable, FacilitatorClient
from app.x402.models_v1 import PaymentPayload, PaymentRequirements
from app.x402.models_v2 import PaymentPayloadV2, PaymentRequirementsV2, SettleResponseV2, VerifyResponseV2

URL = "https://facilitator.example"
PAYER = "0x" + "a1" * 20
PAY_TO = "0x" + "22" * 20
OFFER = {
    "scheme": "exact", "network": "eip155:84532",
    "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e", "amount": "20000",
    "payTo": PAY_TO, "maxTimeoutSeconds": 300, "extra": {"name": "USDC", "version": "2"},
}
AUTH = {"from": PAYER, "to": PAY_TO, "value": "20000", "validAfter": "0",
        "validBefore": "9999999999", "nonce": "0x" + "01" * 32}


def _payment(**extra):
    body = {"x402Version": 2, "accepted": dict(OFFER, unknownField=1),
            "payload": {"authorization": dict(AUTH, junk="x"), "signature": "0x" + "ab" * 65, "more": 1},
            "resource": {"url": "https://gateway.example/api/v1/stamps/", "mimeType": "application/json"},
            "topLevelUnknown": True}
    body.update(extra)
    return PaymentPayloadV2.model_validate(body)


REQS = PaymentRequirementsV2.model_validate(OFFER)


class Stub:
    def __init__(self, status=200, body=None):
        self.status, self.body, self.requests = status, body, []

    def handler(self, request):
        self.requests.append({"path": request.url.path, "body": json.loads(request.content)})
        if isinstance(self.body, (dict, list)):
            return httpx.Response(self.status, json=self.body)
        return httpx.Response(self.status, content=self.body or b"")


def _client(stub):
    return FacilitatorClient({"url": URL}, transport=httpx.MockTransport(stub.handler))


SETTLED = {"success": True, "transaction": "0x" + "cd" * 32, "network": "eip155:84532", "payer": PAYER}
REFUSED = {"success": False, "errorReason": "invalid_signature", "transaction": "",
           "network": "eip155:84532", "payer": PAYER}


@pytest.mark.parametrize("path", ["verify", "settle"])
def test_v2_request_body_is_exact(path):
    stub = Stub(body={"isValid": True, "payer": PAYER} if path == "verify" else SETTLED)
    asyncio.run(getattr(_client(stub), path)(payment=_payment(extensions={"bazaar": {"x": 1}}),
                                                    payment_requirements=REQS))
    assert [r["path"] for r in stub.requests] == [f"/{path}"]
    assert stub.requests[0]["body"] == {
        "x402Version": 2,
        "paymentPayload": {
            "x402Version": 2,
            "resource": {"url": "https://gateway.example/api/v1/stamps/", "mimeType": "application/json"},
            "accepted": OFFER,
            "payload": {"signature": "0x" + "ab" * 65, "authorization": AUTH},
        },
        "paymentRequirements": OFFER,
    }


@pytest.mark.parametrize("status,body,valid", [
    (200, {"isValid": True, "payer": PAYER}, True),
    (200, {"isValid": False, "invalidReason": "insufficient_funds", "payer": PAYER}, False),
    (400, {"isValid": False, "invalidReason": "insufficient_funds", "payer": PAYER}, False),
    (422, {"isValid": False, "invalidReason": "bad"}, False),
])
def test_verify_results(status, body, valid):
    got = asyncio.run(_client(Stub(status, body)).verify(payment=_payment(), payment_requirements=REQS))
    assert isinstance(got, VerifyResponseV2) and got.is_valid is valid


@pytest.mark.parametrize("status,body", [
    (500, {"isValid": True, "payer": PAYER}),     # never "verified" from a 5xx
    (503, {"isValid": False, "invalidReason": "x"}),
    (400, {"isValid": True, "payer": PAYER}),
    (302, {"isValid": True, "payer": PAYER}),
])
def test_verify_unusable_answers_raise(status, body):
    with pytest.raises(FacilitatorAnswerUnusable):
        asyncio.run(_client(Stub(status, body)).verify(payment=_payment(), payment_requirements=REQS))


def test_verify_unparsable_raises():
    with pytest.raises(Exception):
        asyncio.run(_client(Stub(200, b"not json")).verify(payment=_payment(), payment_requirements=REQS))


@pytest.mark.parametrize("status,body,ok", [
    (200, SETTLED, True), (200, REFUSED, False), (400, REFUSED, False), (422, REFUSED, False),
])
def test_settle_results(status, body, ok):
    got = asyncio.run(_client(Stub(status, body)).settle(payment=_payment(), payment_requirements=REQS))
    assert isinstance(got, SettleResponseV2) and got.success is ok


@pytest.mark.parametrize("status,body,exc", [
    (500, REFUSED, FacilitatorAnswerUnusable),
    (502, REFUSED, FacilitatorAnswerUnusable),
    (400, SETTLED, FacilitatorAnswerUnusable),
    (302, SETTLED, FacilitatorAnswerUnusable),
    (200, {"success": False, "errorReason": "x"}, ValidationError),   # no transaction or network
    (400, {"error": "bad"}, ValidationError),
    (200, b"<html>", json.JSONDecodeError),
])
def test_settle_unknown_outcomes_raise(status, body, exc):
    with pytest.raises(exc):
        asyncio.run(_client(Stub(status, body)).settle(payment=_payment(), payment_requirements=REQS))


def test_versions_cannot_be_mixed():
    v1_reqs = PaymentRequirements(scheme="exact", network="base-sepolia", max_amount_required="1",
                                  resource="r", description="d", mime_type="m", pay_to=PAY_TO,
                                  max_timeout_seconds=300, asset=OFFER["asset"])
    v1_payment = PaymentPayload.model_validate({"x402Version": 1, "scheme": "exact", "network": "base-sepolia",
                                                "payload": {"signature": "0x", "authorization": AUTH}})
    stub = Stub(body=SETTLED)
    with pytest.raises(TypeError):
        asyncio.run(_client(stub).settle(payment=_payment(), payment_requirements=v1_reqs))
    with pytest.raises(TypeError):
        asyncio.run(_client(stub).verify(payment=v1_payment, payment_requirements=REQS))
    assert stub.requests == []
