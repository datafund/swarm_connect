"""Builders for x402 payments in either version, for tests (#607).

The shared builders of #605 do not exist yet; these cover what the v2 tests
need and build v1 payments the way tests/test_x402_v1_golden.py does, so one
test can run for both versions:

    headers = pay(version, offer_v1, payer=..., nonce=...)

`offer_v1` is the v1 `accepts[0]` the gateway sent (camelCase dict). The v2
payment's `accepted` is the gateway's v2 offer derived from it exactly as the
gateway derives it (app.x402.v2_payment.v2_offer_from_v1). Signatures are
placeholders: these tests mock the facilitator.
"""
import base64
import json
from typing import Optional

from app.x402.networks import to_caip2

PAYER = "0x" + "a1" * 20
SIGNATURE = "0x" + "ab" * 65


def nonce_hex(byte: str = "01") -> str:
    return "0x" + byte * 32


def authorization(offer_v1: dict, payer: str = PAYER, nonce: Optional[str] = None,
                  value: Optional[str] = None) -> dict:
    return {
        "from": payer,
        "to": offer_v1["payTo"],
        "value": value if value is not None else offer_v1["maxAmountRequired"],
        "validAfter": "0",
        "validBefore": "9999999999",
        "nonce": nonce or nonce_hex(),
    }


def v2_accepted(offer_v1: dict) -> dict:
    """The gateway's v2 offer for a v1 offer, as a client copies it."""
    out = {
        "scheme": offer_v1["scheme"],
        "network": to_caip2(offer_v1["network"]),
        "asset": offer_v1["asset"],
        "amount": offer_v1["maxAmountRequired"],
        "payTo": offer_v1["payTo"],
        "maxTimeoutSeconds": offer_v1["maxTimeoutSeconds"],
    }
    if offer_v1.get("extra") is not None:
        out["extra"] = offer_v1["extra"]
    return out


def v1_payload(offer_v1: dict, auth: dict, x402_version=1) -> dict:
    return {"x402Version": x402_version, "scheme": offer_v1["scheme"], "network": offer_v1["network"],
            "payload": {"signature": SIGNATURE, "authorization": auth}}


def v2_payload(offer_v1: dict, auth: dict, accepted: Optional[dict] = None, x402_version=2,
               **extra_fields) -> dict:
    body = {
        "x402Version": x402_version,
        "payload": {"authorization": auth, "signature": SIGNATURE},
        "resource": {"url": offer_v1["resource"], "description": offer_v1["description"],
                     "mimeType": offer_v1["mimeType"]},
        "accepted": accepted if accepted is not None else v2_accepted(offer_v1),
    }
    body.update(extra_fields)
    return body


def encode(obj) -> str:
    return base64.b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode()


def header_for(version: int, payload: dict) -> dict:
    return {"X-PAYMENT" if version == 1 else "PAYMENT-SIGNATURE": encode(payload)}


def pay(version: int, offer_v1: dict, payer: str = PAYER, nonce: Optional[str] = None) -> dict:
    """The payment header of `version` for the gateway's offer."""
    auth = authorization(offer_v1, payer=payer, nonce=nonce)
    payload = v1_payload(offer_v1, auth) if version == 1 else v2_payload(offer_v1, auth)
    return header_for(version, payload)


def receipt_header(version: int) -> str:
    return "X-PAYMENT-RESPONSE" if version == 1 else "PAYMENT-RESPONSE"


def other_receipt_header(version: int) -> str:
    return receipt_header(2 if version == 1 else 1)
