# app/x402/payment_headers.py
"""Which x402 payment headers a request carries, independent of the version (#600).

x402 v1 clients pay with `X-PAYMENT`; v2 clients with `PAYMENT-SIGNATURE`.
Every place that asks "does this request offer a payment?" asks here, so a v2
header is never mistaken for no payment: before #600 such a request was
treated as unpaid and, where the free tier is on, was served free while the
client believed it had paid.

The gateway settles only v1 so far. Until v2 is enabled, a paid route answers
a `PAYMENT-SIGNATURE` request with 402 X402_VERSION_UNSUPPORTED (a v1 Payment
Required body a v1 client can still pay), and a request carrying both headers
with 400 PAYMENT_HEADER_CONFLICT (app/x402/dependency.py).

A header counts as present by its name, whatever its value: an empty
`PAYMENT-SIGNATURE` is refused too, so a v2 request never falls through to the
free tier. (An empty `X-PAYMENT` alone is still treated as no payment by the
payment dependency, as before.)

No heavy imports: the plain-ASGI Bee call gate imports this for every request.
"""
from typing import Set

X_PAYMENT_HEADER = "X-PAYMENT"               # x402 v1
PAYMENT_SIGNATURE_HEADER = "PAYMENT-SIGNATURE"  # x402 v2

_VERSION_BY_NAME = {
    X_PAYMENT_HEADER.lower(): 1,
    PAYMENT_SIGNATURE_HEADER.lower(): 2,
}

def offered_payment_versions(headers) -> Set[int]:
    """The x402 versions whose payment header the request carries: a subset of {1, 2}.

    Takes a request's Starlette `Headers` (case-insensitive) or an ASGI
    scope's raw header list of (name, value) byte pairs. Names are compared
    case-insensitively either way.
    """
    raw = getattr(headers, "raw", headers)
    found = set()
    for name, _ in raw:
        if isinstance(name, bytes):
            name = name.decode("latin-1")
        version = _VERSION_BY_NAME.get(name.lower())
        if version is not None:
            found.add(version)
    return found


def offers_payment(headers) -> bool:
    """The request carries an x402 payment header of any version, so it could be charged."""
    return bool(offered_payment_versions(headers))


VERSION_UNSUPPORTED_MESSAGE = (
    "This gateway does not accept x402 v2 payments (the PAYMENT-SIGNATURE header) yet. "
    "Nothing was charged. Pay with x402 v1: sign a payment for accepts[0] and send it "
    "in the X-PAYMENT header instead."
)
HEADER_CONFLICT_MESSAGE = (
    "The request carries both X-PAYMENT (x402 v1) and PAYMENT-SIGNATURE (x402 v2). "
    "Send exactly one payment header. Nothing was charged."
)
