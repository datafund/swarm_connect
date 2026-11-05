# app/x402/v2_payment.py
"""Reading an x402 v2 payment (PAYMENT-SIGNATURE) before it is verified (#607).

The dependency (dependency.py) calls read_v2_payment() once v2 is enabled.
It returns the decoded payment and the gateway's own v2 offer for the
request, or raises the refusal. Nothing here calls the facilitator: every
refusal below happens before it, and nothing is charged.

In this order:

1. Decode with codec_v2. Too large, malformed or invalid: 400 with the
   codec's code (PAYMENT_HEADER_TOO_LARGE, PAYMENT_HEADER_MALFORMED,
   PAYMENT_HEADER_INVALID). An x402Version other than 2: 402
   X402_VERSION_UNSUPPORTED, with the v1 offer a client can still pay.
   A Permit2 `exact` payload (permit2Authorization instead of authorization)
   fails the model too; it gets 402 PAYMENT_METHOD_UNSUPPORTED, because
   the gateway offers EIP-3009 only (D9 in #596).
2. accepted.scheme other than "exact": 402 PAYMENT_SCHEME_UNSUPPORTED.
3. Every field of `accepted` must equal the gateway's own v2 offer for this
   request, exactly: network, asset, amount, payTo, maxTimeoutSeconds and
   extra. Strings compare as strings, so "00020000" is not "20000" and an
   address in another letter case does not match. A mismatch: 402
   PAYMENT_OFFER_MISMATCH, naming the fields (never their values).

The client's `accepted` is never what is sent on: the requirements sent to
the facilitator are the gateway's offer, and so is the `accepted` inside the
forwarded payload (they are equal by step 3).

Every 402 here keeps the v1 Payment Required shape (x402Version 1 and the v1
`accepts`), as every other 402 does until PAYMENT-REQUIRED is sent (#608).
"""
from typing import List, Tuple

from fastapi import HTTPException

from app.x402 import codec_v2
from app.x402.models_v1 import PaymentRequirements
from app.x402.models_v2 import PaymentPayloadV2, PaymentRequirementsV2
from app.x402.networks import to_caip2
from app.x402.payment_headers import PAYMENT_SIGNATURE_HEADER

# The fields of `accepted` compared with the gateway's offer, by alias.
OFFER_FIELDS = ("scheme", "network", "asset", "amount", "payTo", "maxTimeoutSeconds", "extra")

NOT_CHARGED = "Nothing was charged."


def v2_offer_from_v1(requirements: PaymentRequirements) -> PaymentRequirementsV2:
    """The gateway's v2 offer for a request, from the same v1 requirements it
    priced, so the two can never describe different payments."""
    return PaymentRequirementsV2(
        scheme=requirements.scheme,
        network=to_caip2(requirements.network),
        asset=requirements.asset,
        amount=requirements.max_amount_required,
        pay_to=requirements.pay_to,
        max_timeout_seconds=requirements.max_timeout_seconds,
        extra=requirements.extra,
    )


def offer_mismatches(accepted: PaymentRequirementsV2, offer: PaymentRequirementsV2) -> List[str]:
    """The OFFER_FIELDS where `accepted` differs from `offer`, exactly."""
    got = accepted.model_dump(mode="json", by_alias=True)
    want = offer.model_dump(mode="json", by_alias=True)
    return [f for f in OFFER_FIELDS if got.get(f) != want.get(f)]


def payment_required_402(requirements: PaymentRequirements, *, code: str, message: str, **fields) -> HTTPException:
    """A 402 with a code and the v1 offer (x402Version 1, accepts)."""
    return HTTPException(status_code=402, detail={
        "code": code,
        "message": message,
        **fields,
        "x402Version": 1,
        "error": message,
        "accepts": [requirements.model_dump(by_alias=True)],
    })


def _is_permit2(value: str) -> bool:
    try:
        obj = codec_v2._decode_json_object(value, PAYMENT_SIGNATURE_HEADER)
    except codec_v2.X402HeaderError:
        return False
    payload = obj.get("payload")
    accepted = obj.get("accepted")
    return (isinstance(payload, dict) and isinstance(accepted, dict)
            and accepted.get("scheme") == "exact"
            and "authorization" not in payload and "permit2Authorization" in payload)


def _header_400(exc: "codec_v2.X402HeaderError") -> HTTPException:
    # exc.code is PAYMENT_HEADER_TOO_LARGE, PAYMENT_HEADER_MALFORMED or
    # PAYMENT_HEADER_INVALID (codec_v2). The message never holds the value.
    return HTTPException(status_code=400, detail={"code": exc.code, "message": f"{exc}. {NOT_CHARGED}"})


def decode_v2_payment(value, requirements: PaymentRequirements) -> PaymentPayloadV2:
    """Step 1: the decoded PAYMENT-SIGNATURE, or raise its refusal."""
    try:
        return codec_v2.decode_payment_signature(value)
    except codec_v2.PaymentHeaderVersionMismatch:
        raise payment_required_402(
            requirements, code="X402_VERSION_UNSUPPORTED",
            message=("The PAYMENT-SIGNATURE header carries a payment whose x402Version is not 2. "
                     + NOT_CHARGED + " Send an x402 v2 payment in PAYMENT-SIGNATURE, or an x402 v1 "
                     "payment for accepts[0] in X-PAYMENT.")) from None
    except codec_v2.PaymentHeaderInvalid as exc:
        if _is_permit2(value):
            raise payment_required_402(
                requirements, code="PAYMENT_METHOD_UNSUPPORTED",
                message=("This gateway accepts exact payments signed with EIP-3009 "
                         "(transferWithAuthorization) only, not Permit2. " + NOT_CHARGED)) from None
        raise _header_400(exc) from None
    except codec_v2.X402HeaderError as exc:
        raise _header_400(exc) from None


def read_v2_payment(value, requirements: PaymentRequirements) -> Tuple[PaymentPayloadV2, PaymentRequirementsV2]:
    """Steps 1 to 3: (payment to verify, the gateway's v2 offer), or raise.

    The returned payment's `accepted` is the gateway's offer."""
    payment = decode_v2_payment(value, requirements)
    offer = v2_offer_from_v1(requirements)
    if payment.accepted.scheme != "exact":
        raise payment_required_402(
            requirements, code="PAYMENT_SCHEME_UNSUPPORTED",
            message="This gateway accepts the exact scheme only. " + NOT_CHARGED)
    try:
        mismatched = offer_mismatches(payment.accepted, offer)
    except Exception:
        # The codec's depth and number limits keep any decoded payment
        # serialisable; should a value still fail to dump (a RecursionError, a
        # ValueError), it is refused as invalid, never answered with a 500.
        raise HTTPException(status_code=400, detail={
            "code": "PAYMENT_HEADER_INVALID",
            "message": ("The PAYMENT-SIGNATURE header's accepted offer cannot be read. "
                        + NOT_CHARGED),
        }) from None
    if mismatched:
        raise payment_required_402(
            requirements, code="PAYMENT_OFFER_MISMATCH",
            message=("The payment's accepted offer does not match this gateway's offer for this "
                     f"request ({', '.join(mismatched)}). " + NOT_CHARGED
                     + " Sign a payment for the offer as sent."),
            fields=mismatched)
    return payment.model_copy(update={"accepted": offer}), offer
