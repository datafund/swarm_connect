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



def accepted_x402_versions() -> tuple:
    """The x402 protocol versions this gateway accepts a payment in, as /health
    reports them (#602): the X402_PROTOCOL_VERSIONS setting (#607). One place
    to read it."""
    from app.core.config import settings
    return settings.x402_protocol_versions()


def version_enabled(version: int) -> bool:
    """Whether payments in this x402 version are accepted (#607)."""
    return version in accepted_x402_versions()


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


def payment_version_label(versions) -> str:
    """The `version` label of a payment metric for the headers a request carried (#601).

    "none" without a payment header, "2" when PAYMENT-SIGNATURE is present
    (alone, or with X-PAYMENT: a client attempting v2), else "1".
    """
    if not versions:
        return "none"
    return "2" if 2 in versions else "1"


def paid_version_label(request) -> str:
    """The x402 version of the payment a request carries, as "1" or "2" (#601).

    For settlement metrics and payment audit records. The dependency records
    the version of a verified payment on the request (request.state.x402_version).
    Before that (a refusal during verification) it is read from the request's
    payment headers. "1" when neither says, including without a request: only
    v1 reaches verify or settle until v2 is enabled (#609).
    """
    version = getattr(getattr(request, "state", None), "x402_version", None)
    if version in (1, 2):
        return str(version)
    headers = getattr(request, "headers", None)
    if headers is not None:
        try:
            label = payment_version_label(offered_payment_versions(headers))
        except Exception:
            label = "none"
        if label != "none":
            return label
    return "1"


def paid_version(request) -> int:
    """paid_version_label as the int written to audit records' data.x402_version."""
    return int(paid_version_label(request))


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
