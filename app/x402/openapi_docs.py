# app/x402/openapi_docs.py
"""
The x402 payment headers of both versions, as OpenAPI declarations (#609, #428).

Each paid route passes `paid_route_openapi(...)` as its `openapi_extra`. FastAPI
merges that dict into the route's generated operation when it builds the
schema, and uses it for nothing else: the headers are not route parameters, so
they are never required, never validated and never change a 422. The payment
itself is read by the x402 dependency (app/x402/dependency.py), as before.

Declared on every route x402 prices (`PROTECTED_ENDPOINTS` in middleware.py):

- request: `X-PAYMENT` (v1) and `PAYMENT-SIGNATURE` (v2);
- 402: the `PAYMENT-REQUIRED` header (v2);
- each declared success status: `X-PAYMENT-RESPONSE` (v1), `PAYMENT-RESPONSE`
  (v2) and `X-Payment-Transaction`.

The schema is the same whatever the gateway's settings, so the descriptions
say when each header applies. A new dict is built on every call: FastAPI puts
the values into the schema by reference.
"""
from typing import Any, Dict, Iterable

from app.x402.codec_v2 import PAYMENT_REQUIRED_HEADER, PAYMENT_RESPONSE_HEADER
from app.x402.payment_headers import PAYMENT_SIGNATURE_HEADER, X_PAYMENT_HEADER

# The same names as middleware.X_PAYMENT_RESPONSE_HEADER and the header the
# middleware sets; not imported, to keep this module free of the middleware.
X_PAYMENT_RESPONSE_HEADER = "X-PAYMENT-RESPONSE"
X_PAYMENT_TRANSACTION_HEADER = "X-Payment-Transaction"

REQUEST_HEADERS = (X_PAYMENT_HEADER, PAYMENT_SIGNATURE_HEADER)
PAYMENT_REQUIRED_HEADERS = (PAYMENT_REQUIRED_HEADER,)
RECEIPT_HEADERS = (X_PAYMENT_RESPONSE_HEADER, PAYMENT_RESPONSE_HEADER, X_PAYMENT_TRANSACTION_HEADER)

_V2_ACCEPTED = "the gateway accepts x402 v2 (`x402.versions` in `GET /health` includes 2)"


def _request_headers(payment_optional: bool) -> list:
    if payment_optional:
        without = ("Payment is optional here: without a payment header the request is served "
                   "from the free daily allowance, and refused by the handler once that is spent.")
    else:
        without = ("With x402 enabled, a request with no payment header (and no "
                   "`X-Payment-Mode: free`) gets 402.")
    both = "Send one payment header, not both: both in one request get 400 `PAYMENT_HEADER_CONFLICT`."
    return [
        {
            "name": X_PAYMENT_HEADER,
            "in": "header",
            "required": False,
            "schema": {"type": "string"},
            "description": (
                "x402 v1 payment: base64 of the JSON `PaymentPayload` for an offer in the 402 body's "
                f"`accepts`. {without} {both}"
            ),
        },
        {
            "name": PAYMENT_SIGNATURE_HEADER,
            "in": "header",
            "required": False,
            "schema": {"type": "string"},
            "description": (
                "x402 v2 payment: base64 of the JSON v2 `PaymentPayload`, its `accepted` an unchanged "
                f"copy of an offer from the 402's `{PAYMENT_REQUIRED_HEADER}` header. Accepted only when "
                f"{_V2_ACCEPTED}; otherwise 402 `X402_VERSION_UNSUPPORTED`. {without} {both}"
            ),
        },
    ]


def _payment_required_response(payment_optional: bool) -> Dict[str, Any]:
    when = ("Only when a payment header is sent and cannot be accepted. "
            if payment_optional else "")
    return {
        "description": (
            f"Payment required (x402 enabled). {when}The JSON body is the x402 v1 `PaymentRequired` "
            "with `code` and `message`; when the gateway accepts x402 v2 only, it is the v2 copy. "
            "See docs/error-codes.md. Nothing was charged."
        ),
        "headers": {
            PAYMENT_REQUIRED_HEADER: {
                "description": (
                    "x402 v2 `PaymentRequired`: base64 of compact JSON with `x402Version` 2, `error`, "
                    "`resource` and `accepts` (the body's offers in v2 form). Sent when "
                    f"{_V2_ACCEPTED} and the 402 offers a payment."
                ),
                "schema": {"type": "string"},
            },
        },
    }


def _receipt_headers() -> Dict[str, Any]:
    return {
        X_PAYMENT_RESPONSE_HEADER: {
            "description": ("x402 v1 settlement receipt: base64 of the JSON `SettleResponse`. "
                            f"Only on a request paid with `{X_PAYMENT_HEADER}`."),
            "schema": {"type": "string"},
        },
        PAYMENT_RESPONSE_HEADER: {
            "description": ("x402 v2 settlement receipt: base64 of the JSON v2 `SettleResponse`. "
                            f"Only on a request paid with `{PAYMENT_SIGNATURE_HEADER}`; never together "
                            f"with `{X_PAYMENT_RESPONSE_HEADER}`."),
            "schema": {"type": "string"},
        },
        X_PAYMENT_TRANSACTION_HEADER: {
            "description": ("The settlement transaction hash, or `unknown` when the facilitator "
                            "returned none that could be checked. Only on a paid request."),
            "schema": {"type": "string"},
        },
    }


def paid_route_openapi(success_codes: Iterable[int], *, payment_optional: bool = False) -> Dict[str, Any]:
    """The `openapi_extra` of a paid route: both versions' payment headers.

    `success_codes` are the route's declared 2xx statuses (its `status_code`
    and any 2xx in `responses`); each gets the receipt headers. A code the
    route does not otherwise declare would produce a response without a
    description, so pass only declared ones (tests/test_x402_openapi_headers.py
    checks this).
    """
    responses: Dict[str, Any] = {"402": _payment_required_response(payment_optional)}
    for code in success_codes:
        responses[str(int(code))] = {"headers": _receipt_headers()}
    return {"parameters": _request_headers(payment_optional), "responses": responses}
