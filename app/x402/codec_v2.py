# app/x402/codec_v2.py
"""Encode and decode the x402 v2 headers (#606, part of #596).

- PAYMENT-REQUIRED (gateway -> client, on a 402): PaymentRequired
- PAYMENT-SIGNATURE (client -> gateway): PaymentPayloadV2
- PAYMENT-RESPONSE (gateway -> client, after settlement): SettleResponseV2

Each value is standard base64 (with padding) of the UTF-8 compact JSON,
exactly what @x402/core writes: safeBase64Encode(JSON.stringify(x)).

Decoding is strict, in this order, and stops at the first failure:

1. the value is a str of at most MAX_HEADER_CHARS characters
   (PaymentHeaderTooLarge), checked before anything is decoded;
2. canonical base64: only the standard alphabet, correct padding, no
   whitespace, and it must re-encode to the same text (PaymentHeaderMalformed);
3. UTF-8, then JSON with no duplicate keys and no NaN/Infinity, and the value
   must be an object (PaymentHeaderMalformed). Duplicate keys are refused so the
   gateway and a facilitator can never read one header two ways. A string
   holding a lone surrogate (a JSON escape such as \\ud800) is refused too:
   it decodes, but the model could then not be encoded back to UTF-8. So is
   a number too large for a float (1e400 reads as infinity) and nesting deeper
   than MAX_JSON_DEPTH (32) levels. Together these keep the invariant that every
   model that decodes can be encoded again without any error other than
   X402HeaderError;
4. x402Version, when present, must be 2 (PaymentHeaderVersionMismatch);
5. the v2 model (PaymentHeaderInvalid).

MAX_HEADER_CHARS is 8192, for encoding and decoding. The largest headers the
gateway produces or expects are far smaller (docs/adr/x402-v2.md, Question 5:
PAYMENT-REQUIRED 956 characters, PAYMENT-SIGNATURE about 1,472, about 3.1 KB
even with 8 offers). 8192 is nginx's default limit for one request header
field and the limit the gateway's MCP client puts on PAYMENT-REQUIRED, so the
gateway never emits a header its own client refuses. The models allow 1024
offers, as the spec does; the header limit binds well before that.

Errors never contain the header value or any decoded value: only the header
name, its length, the failing step and, for validation, the field locations
and pydantic error types. They are raised outside any except block, so no
exception (with its input-bearing text) is chained to them as __cause__ or
__context__. #607 maps `code` to a 400 or 402 answer; these codes
are not sent by any endpoint yet.

Nothing in a request path uses this module yet (#607).
"""
import base64
import binascii
import json
from typing import Any, List, Tuple, Type, TypeVar

from pydantic import BaseModel, ValidationError

from app.x402.models_v2 import PaymentPayloadV2, PaymentRequired, SettleResponseV2
from app.x402.payment_headers import PAYMENT_SIGNATURE_HEADER

PAYMENT_REQUIRED_HEADER = "PAYMENT-REQUIRED"
PAYMENT_RESPONSE_HEADER = "PAYMENT-RESPONSE"
MAX_HEADER_CHARS = 8192
# Deepest nesting of objects and arrays a decoded header may have, the top-level
# object counting as 1. The real shapes are shallow: the deepest fixed path is
# PAYMENT-SIGNATURE payload.authorization.from (depth 3), and free-form
# `extra` / `extensions` values seen from facilitators (bazaar, gas-sponsoring
# data, small JSON schemas) stay under 10. 32 leaves room for those, and is far
# below pydantic's serialiser limit (it fails on a few hundred levels with
# "depth exceeded"), so every model that decodes can be encoded again.
MAX_JSON_DEPTH = 32

M = TypeVar("M", bound=BaseModel)


class X402HeaderError(ValueError):
    """An x402 v2 header that cannot be used. Never carries the header value."""

    code = "PAYMENT_HEADER_INVALID"

    def __init__(self, header_name: str, reason: str, errors: List[Tuple[str, str]] = ()):
        self.header_name = header_name
        self.reason = reason
        # (field location, pydantic error type) pairs, for validation errors.
        self.errors = list(errors)
        super().__init__(f"{header_name} header {reason}")


class PaymentHeaderTooLarge(X402HeaderError):
    code = "PAYMENT_HEADER_TOO_LARGE"


class PaymentHeaderMalformed(X402HeaderError):
    code = "PAYMENT_HEADER_MALFORMED"


class PaymentHeaderInvalid(X402HeaderError):
    code = "PAYMENT_HEADER_INVALID"


class PaymentHeaderVersionMismatch(X402HeaderError):
    code = "X402_VERSION_UNSUPPORTED"


# --- encoding -----------------------------------------------------------------

def _encode(model: BaseModel, cls: Type[BaseModel], header_name: str) -> str:
    if not isinstance(model, cls):
        raise TypeError(f"{header_name} needs a {cls.__name__}, not {type(model).__name__}")
    text = json.dumps(
        model.model_dump(mode="json", by_alias=True, exclude_none=True),
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    value = base64.b64encode(text.encode("utf-8")).decode("ascii")
    if len(value) > MAX_HEADER_CHARS:
        raise PaymentHeaderTooLarge(
            header_name, f"would be {len(value)} characters, over the {MAX_HEADER_CHARS} limit")
    return value


def encode_payment_required(message: PaymentRequired) -> str:
    """The PAYMENT-REQUIRED header value for a v2 Payment Required message."""
    return _encode(message, PaymentRequired, PAYMENT_REQUIRED_HEADER)


def encode_payment_signature(payment: PaymentPayloadV2) -> str:
    """The PAYMENT-SIGNATURE header value for a v2 payment (clients and tests)."""
    return _encode(payment, PaymentPayloadV2, PAYMENT_SIGNATURE_HEADER)


def encode_payment_response(receipt: SettleResponseV2) -> str:
    """The PAYMENT-RESPONSE header value for a v2 settlement result."""
    return _encode(receipt, SettleResponseV2, PAYMENT_RESPONSE_HEADER)


# --- decoding -----------------------------------------------------------------

def _no_duplicates(pairs):
    obj = {}
    for key, val in pairs:
        if key in obj:
            raise ValueError("duplicate key")
        obj[key] = val
    return obj


def _no_constants(_name):
    raise ValueError("NaN or Infinity")


def _finite_float(text):
    # "1e400" parses to inf without going through parse_constant, and an
    # infinite float cannot be written back as JSON.
    value = float(text)
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError("non-finite number")
    return value


def _depth(obj) -> int:
    """Nesting depth of objects and arrays (the outer container is 1), iteratively."""
    deepest = 0
    stack = [(obj, 1)]
    while stack:
        node, level = stack.pop()
        if isinstance(node, (dict, list)):
            deepest = max(deepest, level)
            if level > MAX_JSON_DEPTH:
                break
            children = node.values() if isinstance(node, dict) else node
            stack.extend((child, level + 1) for child in children)
    return deepest


def _decode_json_object(value: Any, header_name: str) -> dict:
    # Each step records its failure and raises after the except block, so the
    # error has no __cause__ and no __context__: a chained binascii, json or
    # Unicode error would carry the header or a decoded value in its text.
    if not isinstance(value, str):
        raise PaymentHeaderMalformed(header_name, "is not text")
    if len(value) > MAX_HEADER_CHARS:
        raise PaymentHeaderTooLarge(
            header_name, f"is {len(value)} characters, over the {MAX_HEADER_CHARS} limit")
    failure = None
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        failure = "is not valid base64"
    if failure is None and base64.b64encode(raw).decode("ascii") != value:
        failure = "is not canonical base64"
    if failure is None:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            failure = "is not base64 of UTF-8 text"
    if failure is None:
        try:
            obj = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constants,
                             parse_float=_finite_float)
        except (ValueError, RecursionError):
            failure = "is not base64 of valid JSON"
    if failure is None and not isinstance(obj, dict):
        failure = "is not base64 of a JSON object"
    if failure is None and _depth(obj) > MAX_JSON_DEPTH:
        # Deeper input decodes, but pydantic's serialiser refuses it later
        # with a bare error, so #607 could not re-encode the payment.
        failure = f"nests objects and arrays deeper than {MAX_JSON_DEPTH} levels"
    if failure is None and not _encodable(obj):
        # A JSON escape such as "\ud800" decodes to a lone surrogate, which
        # no UTF-8 encoder can write back: the model could not be re-encoded.
        failure = "has a string that is not valid Unicode (a lone surrogate escape)"
    if failure is not None:
        raise PaymentHeaderMalformed(header_name, failure)
    return obj


def _encodable(obj: dict) -> bool:
    try:
        json.dumps(obj, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _locations(exc: ValidationError) -> List[Tuple[str, str]]:
    return [(".".join(str(p) for p in e["loc"]) or "(root)", e["type"])
            for e in exc.errors(include_input=False, include_url=False, include_context=False)]


def _decode(value: Any, cls: Type[M], header_name: str, versioned: bool) -> M:
    obj = _decode_json_object(value, header_name)
    if versioned and "x402Version" in obj:
        version = obj["x402Version"]
        if type(version) is int and version != 2:
            # Never the value itself: it is client input, and can be huge.
            raise PaymentHeaderVersionMismatch(header_name, "has an x402Version other than 2")
    errors = None
    try:
        return cls.model_validate(obj)
    except ValidationError as exc:
        errors = _locations(exc)
    # Raised here, outside the except block, so the ValidationError (which
    # holds the input values) is neither __cause__ nor __context__.
    fields = ", ".join(f"{loc} ({kind})" for loc, kind in errors[:5])
    more = f" and {len(errors) - 5} more" if len(errors) > 5 else ""
    raise PaymentHeaderInvalid(header_name, f"fails validation: {fields}{more}", errors)


def decode_payment_required(value: Any) -> PaymentRequired:
    """Parse a PAYMENT-REQUIRED header value. Raises X402HeaderError."""
    return _decode(value, PaymentRequired, PAYMENT_REQUIRED_HEADER, versioned=True)


def decode_payment_signature(value: Any) -> PaymentPayloadV2:
    """Parse a PAYMENT-SIGNATURE header value. Raises X402HeaderError."""
    return _decode(value, PaymentPayloadV2, PAYMENT_SIGNATURE_HEADER, versioned=True)


def decode_payment_response(value: Any) -> SettleResponseV2:
    """Parse a PAYMENT-RESPONSE header value. Raises X402HeaderError."""
    return _decode(value, SettleResponseV2, PAYMENT_RESPONSE_HEADER, versioned=False)
