"""The x402 v2 header codecs: PAYMENT-REQUIRED, PAYMENT-SIGNATURE, PAYMENT-RESPONSE (#606).

Valid values round-trip; anything that is not canonical base64 of a JSON
object that passes the v2 model is refused with a typed error whose code #607
maps to a 400 or 402. No error text may carry the header or a decoded value.
"""
import base64
import json
from pathlib import Path

import pytest

from app.x402.codec_v2 import (
    MAX_HEADER_CHARS,
    PAYMENT_REQUIRED_HEADER,
    PAYMENT_RESPONSE_HEADER,
    PaymentHeaderInvalid,
    PaymentHeaderMalformed,
    PaymentHeaderTooLarge,
    PaymentHeaderVersionMismatch,
    X402HeaderError,
    decode_payment_required,
    decode_payment_response,
    decode_payment_signature,
    encode_payment_required,
    encode_payment_response,
    encode_payment_signature,
)
from app.x402.models_v2 import PaymentPayloadV2, PaymentRequired, SettleResponseV2
from app.x402.payment_headers import PAYMENT_SIGNATURE_HEADER
from tests.test_x402_v2_models import OFFER, PAYER, payment_msg, required_msg

# Appears in every bad input below; must never appear in an error.
MARKER = "SECRETMARKER"

SETTLE = {"success": True, "transaction": "0x" + "00" * 32, "network": "eip155:84532", "payer": PAYER}


def b64(obj_or_text):
    text = obj_or_text if isinstance(obj_or_text, str) else json.dumps(obj_or_text, separators=(",", ":"))
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


DECODERS = [
    (decode_payment_required, PAYMENT_REQUIRED_HEADER, required_msg),
    (decode_payment_signature, PAYMENT_SIGNATURE_HEADER, payment_msg),
    (decode_payment_response, PAYMENT_RESPONSE_HEADER, lambda **o: dict(SETTLE, **o)),
]
DECODER_IDS = ["required", "signature", "response"]


def assert_no_leak(exc, *secrets):
    text = str(exc) + repr(exc) + exc.reason + repr(exc.errors) + repr(exc.args)
    for s in (MARKER,) + secrets:
        assert s not in text


# --- round trips ----------------------------------------------------------------

def test_payment_required_round_trip():
    m = PaymentRequired.model_validate(required_msg())
    value = encode_payment_required(m)
    assert decode_payment_required(value) == m


def test_payment_signature_round_trip():
    m = PaymentPayloadV2.model_validate(payment_msg())
    assert decode_payment_signature(encode_payment_signature(m)) == m


def test_payment_response_round_trip():
    m = SettleResponseV2.model_validate(SETTLE)
    assert decode_payment_response(encode_payment_response(m)) == m


def test_encoding_is_base64_of_compact_json_without_none():
    m = PaymentRequired.model_validate(required_msg())
    value = encode_payment_required(m)
    text = base64.b64decode(value).decode("utf-8")
    assert text == json.dumps(required_msg(), separators=(",", ":"))
    assert " " not in text.replace("Payment required", "").replace("Postage stamp purchase", "")
    assert "null" not in text


def test_encoding_writes_utf8_not_escapes_like_json_stringify():
    m = PaymentRequired.model_validate(required_msg(error="Zahlung erforderlich ü"))
    text = base64.b64decode(encode_payment_required(m)).decode("utf-8")
    assert "ü" in text and "\\u00fc" not in text


@pytest.mark.parametrize("encoder,wrong", [
    (encode_payment_required, SettleResponseV2.model_validate(SETTLE)),
    (encode_payment_signature, PaymentRequired.model_validate(required_msg())),
    (encode_payment_response, dict(SETTLE)),
])
def test_encoder_refuses_the_wrong_type(encoder, wrong):
    with pytest.raises(TypeError):
        encoder(wrong)


# --- size -------------------------------------------------------------------------

def test_header_limit_value():
    assert MAX_HEADER_CHARS == 8192


@pytest.mark.parametrize("decode,name,_", DECODERS, ids=DECODER_IDS)
def test_oversized_value_is_refused_before_decoding(decode, name, _):
    value = "A" * (MAX_HEADER_CHARS + 1)  # valid base64 alphabet, would decode
    with pytest.raises(PaymentHeaderTooLarge) as exc:
        decode(value)
    assert exc.value.code == "PAYMENT_HEADER_TOO_LARGE"
    assert exc.value.header_name == name
    assert str(MAX_HEADER_CHARS + 1) in str(exc.value)


def test_value_at_the_limit_passes_the_size_check():
    # 8192 characters of base64 is not a valid message, but it gets past the
    # size check and fails later, as malformed.
    value = "A" * MAX_HEADER_CHARS
    with pytest.raises(PaymentHeaderMalformed):
        decode_payment_required(value)


def test_real_message_near_the_limit_decodes():
    # Grow the description until the header is just under the limit.
    lo, hi = 0, MAX_HEADER_CHARS
    while lo < hi:
        mid = (lo + hi + 1) // 2
        msg = required_msg(error="e" * mid)
        if len(b64(msg)) <= MAX_HEADER_CHARS:
            lo = mid
        else:
            hi = mid - 1
    value = b64(required_msg(error="e" * lo))
    assert MAX_HEADER_CHARS - 4 < len(value) <= MAX_HEADER_CHARS
    assert decode_payment_required(value).error == "e" * lo
    with pytest.raises(PaymentHeaderTooLarge):
        decode_payment_required(b64(required_msg(error="e" * (lo + 3))))


def test_encoder_refuses_a_header_over_the_limit():
    m = PaymentRequired.model_validate(required_msg(accepts=[dict(OFFER)] * 40))
    with pytest.raises(PaymentHeaderTooLarge) as exc:
        encode_payment_required(m)
    assert exc.value.code == "PAYMENT_HEADER_TOO_LARGE"


# --- bad base64 ---------------------------------------------------------------------

GOOD = b64(required_msg())


@pytest.mark.parametrize("value", [
    "",
    "!!!!",
    MARKER + "$",
    "e30",                                  # "{}" with its padding removed
    GOOD[:10] + " " + GOOD[10:],            # embedded space
    GOOD[:10] + "\n" + GOOD[10:],           # embedded newline
    " " + GOOD,
    GOOD + "\n",
    GOOD.replace("+", "-").replace("/", "_") if ("+" in GOOD or "/" in GOOD) else GOOD[:-4] + "-_-_",
    "QQ==QQ==",                             # data after padding
    "QR==",                                 # non-zero padding bits: not canonical
])
def test_bad_base64_is_malformed(value):
    with pytest.raises(PaymentHeaderMalformed) as exc:
        decode_payment_required(value)
    assert exc.value.code == "PAYMENT_HEADER_MALFORMED"
    assert_no_leak(exc.value)


def test_url_safe_alphabet_is_refused():
    raw = b"\xfb\xff\xfe" + json.dumps(required_msg()).encode()
    with pytest.raises(PaymentHeaderMalformed):
        decode_payment_required(base64.urlsafe_b64encode(raw).decode())


@pytest.mark.parametrize("decode,_name,_msg", DECODERS, ids=DECODER_IDS)
def test_non_text_value_is_malformed(decode, _name, _msg):
    with pytest.raises(PaymentHeaderMalformed):
        decode(GOOD.encode())
    with pytest.raises(PaymentHeaderMalformed):
        decode(None)


# --- not a JSON object ----------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    b"\xff\xfe" + MARKER.encode(),           # not UTF-8
    MARKER.encode(),                          # not JSON
    b'{"x402Version": 2,',                    # truncated JSON
    b"\xef\xbb\xbf{}",                        # UTF-8 BOM
    b"[]", b"[" + json.dumps(MARKER).encode() + b"]", b'"' + MARKER.encode() + b'"', b"2", b"null", b"true",
    b'{"x402Version":2,"error":"' + MARKER.encode() + b'","error":"x"}',  # duplicate key
    b'{"a":NaN}', b'{"a":Infinity}', b'{"a":-Infinity}',
    b"[" * 100000,                           # deep nesting (also over the size limit once encoded)
])
@pytest.mark.parametrize("decode,_name,_msg", DECODERS, ids=DECODER_IDS)
def test_not_a_json_object_is_malformed(decode, _name, _msg, raw):
    value = base64.b64encode(raw).decode()
    with pytest.raises((PaymentHeaderMalformed, PaymentHeaderTooLarge)) as exc:
        decode(value)
    assert_no_leak(exc.value)


def test_deep_nesting_within_the_size_limit_is_malformed():
    value = base64.b64encode(b"[" * 6000).decode()
    assert len(value) <= MAX_HEADER_CHARS
    with pytest.raises(PaymentHeaderMalformed):
        decode_payment_signature(value)


# --- validation -------------------------------------------------------------------------

def _invalid(decode, msg):
    with pytest.raises(PaymentHeaderInvalid) as exc:
        decode(b64(msg))
    assert exc.value.code == "PAYMENT_HEADER_INVALID"
    return exc.value


def test_missing_required_field_is_invalid_and_names_it():
    msg = required_msg()
    del msg["accepts"][0]["payTo"]
    err = _invalid(decode_payment_required, msg)
    assert err.errors == [("accepts.0.payTo", "missing")]
    assert "accepts.0.payTo (missing)" in str(err)


def test_missing_payload_field_is_invalid():
    msg = payment_msg()
    del msg["payload"]["authorization"]["nonce"]
    err = _invalid(decode_payment_signature, msg)
    assert err.errors == [("payload.authorization.nonce", "missing")]


def test_settle_response_without_network_is_invalid():
    msg = dict(SETTLE)
    del msg["network"]
    assert _invalid(decode_payment_response, msg).errors == [("network", "missing")]


def test_bad_caip2_network_is_invalid_without_echoing_it():
    err = _invalid(decode_payment_required, required_msg(accepts=[dict(OFFER, network="base-" + MARKER)]))
    assert err.errors == [("accepts.0.network", "value_error")]
    assert_no_leak(err)
    err = _invalid(decode_payment_signature, payment_msg(accepted=dict(OFFER, network=MARKER.lower())))
    assert err.errors[0][0] == "accepted.network"
    assert_no_leak(err, MARKER.lower())


@pytest.mark.parametrize("accepts", [[], [dict(OFFER)] * 1025])
def test_accepts_out_of_range_is_invalid(accepts):
    # 1025 offers do not fit in 8192 characters: the size limit answers first.
    with pytest.raises((PaymentHeaderInvalid, PaymentHeaderTooLarge)) as exc:
        decode_payment_required(b64(required_msg(accepts=accepts)))
    if not accepts:
        assert exc.value.errors == [("accepts", "too_short")]
    else:
        assert isinstance(exc.value, PaymentHeaderTooLarge)


def test_accepts_over_1024_is_invalid_at_the_model_even_without_the_header_limit():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        PaymentRequired.model_validate(required_msg(accepts=[dict(OFFER)] * 1025))


@pytest.mark.parametrize("amount", ["1.5", "-1", "1e6", "abc", "", MARKER])
def test_invalid_amount_string_is_invalid_without_echoing_it(amount):
    err = _invalid(decode_payment_required, required_msg(accepts=[dict(OFFER, amount=amount)]))
    assert err.errors == [("accepts.0.amount", "value_error")]
    assert_no_leak(err)
    err = _invalid(decode_payment_signature, payment_msg(accepted=dict(OFFER, amount=amount)))
    assert err.errors == [("accepted.amount", "value_error")]


def test_many_errors_are_summarised():
    msg = payment_msg()
    msg["payload"]["authorization"] = {}
    err = _invalid(decode_payment_signature, msg)
    assert len(err.errors) == 6
    assert "and 1 more" in str(err)


def test_invalid_error_never_echoes_values():
    msg = payment_msg()
    msg["payload"]["signature"] = 12345678  # wrong type
    msg["payload"]["authorization"]["value"] = MARKER
    msg["accepted"]["payTo"] = ["0x" + MARKER]
    err = _invalid(decode_payment_signature, msg)
    assert_no_leak(err, "12345678")


# --- version ----------------------------------------------------------------------------

@pytest.mark.parametrize("decode,_name,make", DECODERS[:2], ids=DECODER_IDS[:2])
@pytest.mark.parametrize("version", [1, 3, 0, -7, 10 ** 3999])
def test_other_x402_version_is_a_version_mismatch(decode, _name, make, version):
    value = b64(make(x402Version=version))
    assert len(value) <= MAX_HEADER_CHARS
    with pytest.raises(PaymentHeaderVersionMismatch) as exc:
        decode(value)
    assert exc.value.code == "X402_VERSION_UNSUPPORTED"
    assert isinstance(exc.value, X402HeaderError)
    assert "x402Version other than 2" in str(exc.value)
    # The version is client input: never echoed, however long.
    assert_no_leak(exc.value, str(version) if abs(version) > 9 else "x402Version " + str(version))


def test_huge_version_message_has_a_fixed_length():
    value = b64(payment_msg(x402Version=10 ** 3999))
    with pytest.raises(PaymentHeaderVersionMismatch) as exc:
        decode_payment_signature(value)
    assert "0000000000" not in str(exc.value)
    assert len(str(exc.value)) < 100


@pytest.mark.parametrize("version", ["2", True, None, 2.5])
def test_non_integer_version_is_invalid(version):
    with pytest.raises(PaymentHeaderInvalid):
        decode_payment_signature(b64(payment_msg(x402Version=version)))


def test_version_written_as_2_point_0_is_2():
    # JSON.parse reads 2.0 as the number 2, so @x402/core accepts it; so do we.
    m = decode_payment_signature(base64.b64encode(
        json.dumps(payment_msg()).replace('"x402Version": 2', '"x402Version": 2.0').encode()).decode())
    assert m.x402_version == 2 and type(m.x402_version) is int


def test_missing_version_is_invalid():
    msg = payment_msg()
    del msg["x402Version"]
    assert _invalid(decode_payment_signature, msg).errors == [("x402Version", "missing")]


def test_v1_payment_in_the_v2_header_is_a_version_mismatch():
    v1 = {"x402Version": 1, "scheme": "exact", "network": "base-sepolia",
          "payload": payment_msg()["payload"]}
    with pytest.raises(PaymentHeaderVersionMismatch):
        decode_payment_signature(b64(v1))


# --- the error type -------------------------------------------------------------------------

def test_every_error_is_a_value_error_with_a_code_and_header_name():
    for cls, code in [(PaymentHeaderTooLarge, "PAYMENT_HEADER_TOO_LARGE"),
                      (PaymentHeaderMalformed, "PAYMENT_HEADER_MALFORMED"),
                      (PaymentHeaderInvalid, "PAYMENT_HEADER_INVALID"),
                      (PaymentHeaderVersionMismatch, "X402_VERSION_UNSUPPORTED")]:
        assert issubclass(cls, X402HeaderError) and issubclass(cls, ValueError)
        assert cls.code == code


CHAIN_CASES = {
    "bad base64": "!!",
    "not canonical": "QR==",
    "not utf-8": base64.b64encode(b"\xff" + MARKER.encode()).decode(),
    "not json": b64(MARKER),
    "not an object": b64([MARKER]),
    "lone surrogate": b64('{"x402Version":2,"error":"\\ud800' + MARKER + '"}'),
    "validation": b64(payment_msg(accepted=dict(OFFER, network=MARKER))),
    "version": b64(payment_msg(x402Version=10 ** 50)),
    "too large": "A" * (MAX_HEADER_CHARS + 1),
}


@pytest.mark.parametrize("value", list(CHAIN_CASES.values()), ids=list(CHAIN_CASES))
def test_errors_carry_no_exception_chain(value):
    # A chained pydantic, json, binascii or Unicode error would carry the
    # input in its text: neither __cause__ nor __context__ may be set, so a
    # logged traceback shows nothing but the X402HeaderError itself.
    with pytest.raises(X402HeaderError) as exc:
        decode_payment_signature(value)
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None
    assert_no_leak(exc.value)


# --- lone surrogates (refused at decode) ------------------------------------------------------

@pytest.mark.parametrize("escape", ["\\ud800", "\\udfff", "\\udc00x", "a\\ud83d"])
@pytest.mark.parametrize("where", ["error", "extra", "extensions", "key"])
def test_lone_surrogate_is_malformed(escape, where):
    # json.loads turns the escape into a lone surrogate, which decodes but can
    # never be encoded back to UTF-8; the decoder refuses it up front.
    offer = json.dumps(OFFER, separators=(",", ":"))
    if where == "error":
        text = '{"x402Version":2,"error":"' + escape + '","resource":{"url":"u"},"accepts":[' + offer + "]}"
    elif where == "extra":
        bare = {k: v for k, v in OFFER.items() if k != "extra"}
        text = ('{"x402Version":2,"resource":{"url":"u"},"accepts":[' + json.dumps(bare)[:-1]
                + ',"extra":{"name":"' + escape + '"}}]}')
    elif where == "extensions":
        text = '{"x402Version":2,"resource":{"url":"u"},"accepts":[' + offer + '],"extensions":{"x":["' + escape + '"]}}'
    else:
        text = '{"x402Version":2,"resource":{"url":"u"},"accepts":[' + offer + '],"extensions":{"' + escape + '":1}}'
    json.loads(text)  # the text itself is valid JSON
    with pytest.raises(PaymentHeaderMalformed) as exc:
        decode_payment_required(b64(text))
    assert "lone surrogate" in str(exc.value)


def test_a_valid_surrogate_pair_escape_is_fine():
    text = '{"x402Version":2,"error":"\\ud83d\\ude00","resource":{"url":"u"},"accepts":[' + json.dumps(OFFER) + "]}"
    m = decode_payment_required(b64(text))
    assert m.error == "\U0001F600"
    assert decode_payment_required(encode_payment_required(m)) == m


@pytest.mark.parametrize("value", ["yes", "1", 1])
def test_payment_response_success_must_be_a_json_boolean(value):
    err = _invalid(decode_payment_response, dict(SETTLE, success=value))
    assert err.errors == [("success", "bool_type")]


# --- nesting depth and the re-encode invariant ----------------------------------------------

def nested(levels):
    """`levels` arrays inside each other, with 1 innermost: depth `levels`."""
    value = 1
    for _ in range(levels):
        value = [value]
    return value


def deep_signature(depth, where):
    """A PAYMENT-SIGNATURE message whose whole-document depth is exactly `depth`."""
    msg = payment_msg()
    if where == "extensions":      # top(1) > extensions(2) > arrays
        msg["extensions"] = {"x": nested(depth - 2)}
    else:                          # top(1) > accepted(2) > extra(3) > arrays
        msg["accepted"]["extra"] = {"name": "USDC", "version": "2", "x": nested(depth - 3)}
    return msg


def every_encoding(model, encoder):
    """Every way #607 may serialise a decoded model."""
    encoder(model)
    model.model_dump_json(by_alias=True, exclude_none=True)
    json.dumps(model.model_dump(mode="json", by_alias=True, exclude_none=True),
               ensure_ascii=False, allow_nan=False).encode("utf-8")


def test_depth_limit_value():
    from app.x402.codec_v2 import MAX_JSON_DEPTH, _depth
    assert MAX_JSON_DEPTH == 32
    assert _depth({}) == 1 and _depth({"a": [1]}) == 2 and _depth([[{}]]) == 3
    # The deepest real shape is far below the limit.
    assert _depth(payment_msg()) == 3


@pytest.mark.parametrize("where", ["extensions", "extra"])
@pytest.mark.parametrize("depth", [31, 32])
def test_nesting_up_to_the_limit_decodes_and_re_encodes(where, depth):
    from app.x402.codec_v2 import _depth
    msg = deep_signature(depth, where)
    assert _depth(msg) == depth
    value = b64(msg)
    model = decode_payment_signature(value)
    every_encoding(model, encode_payment_signature)
    # The exact header round-trips: the gateway re-encodes the same bytes.
    assert encode_payment_signature(model) == value
    assert decode_payment_signature(encode_payment_signature(model)) == model


@pytest.mark.parametrize("where", ["extensions", "extra"])
@pytest.mark.parametrize("depth", [33, 256, 300])
def test_nesting_over_the_limit_is_malformed(where, depth):
    value = b64(deep_signature(depth, where))
    assert len(value) <= MAX_HEADER_CHARS  # even 300 levels is a small header
    with pytest.raises(PaymentHeaderMalformed) as exc:
        decode_payment_signature(value)
    assert exc.value.code == "PAYMENT_HEADER_MALFORMED"
    assert "deeper than 32" in str(exc.value)
    assert exc.value.__context__ is None


def test_nesting_over_the_limit_is_malformed_in_every_header():
    for decode, _name, make in DECODERS:
        with pytest.raises(PaymentHeaderMalformed):
            decode(b64(make(extensions={"x": nested(40)})))


def test_deep_nesting_without_the_limit_would_break_re_encoding():
    # Guards the reason for the limit: pydantic's serialiser cannot write a
    # model nested a few hundred levels deep (300 here), though it validates one.
    m = PaymentPayloadV2.model_validate(deep_signature(300, "extensions"))
    with pytest.raises(Exception) as exc:
        m.model_dump_json(by_alias=True, exclude_none=True)
    assert not isinstance(exc.value, X402HeaderError)


@pytest.mark.parametrize("number", ["1e400", "-1e400", "1E999"])
def test_number_overflowing_to_infinity_is_malformed(number):
    text = json.dumps(payment_msg(extensions={"n": 0})).replace('"n": 0', '"n": ' + number)
    with pytest.raises(PaymentHeaderMalformed):
        decode_payment_signature(b64(text))


FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "x402_v2"


def _edge_values():
    """Headers that decode, for the re-encode property below."""
    cases = []
    for name in sorted(FIXTURE_DIR.glob("*.b64")):
        kind = name.name.split("_", 1)[1].rsplit(".", 1)[0]  # payment_required, ...
        cases.append((f"fixture {name.name}", kind, name.read_text().strip()))
    pair = '"\\ud83d\\ude00"'
    for label, ext in [
        ("depth 32", {"x": nested(30)}),
        ("free-form scalars", {"i": 10 ** 300, "f": 1e308, "z": -0.0, "tiny": 5e-324, "s": "", "n": None,
                               "b": False, "u": "café   \u0000"}),
        ("empty containers", {"o": {}, "a": [], "aa": [[], {}]}),
    ]:
        cases.append((label + " (signature)", "payment_signature", b64(payment_msg(extensions=ext))))
        cases.append((label + " (required)", "payment_required", b64(required_msg(extensions=ext))))
    cases.append(("surrogate pair", "payment_required", b64(
        json.dumps(required_msg(error="x")).replace('"error": "x"', '"error": ' + pair))))
    cases.append(("underflow and exponent floats", "payment_signature", b64(
        json.dumps(payment_msg(extensions={"a": 0})).replace('"a": 0', '"a": 1e-400, "b": 1E2'))))
    # Near the size limit: re-encoding must not push it over.
    near = required_msg(extensions={"f": [1e2] * 10})
    pad = MAX_HEADER_CHARS
    while len(b64(dict(near, error="e" * pad))) > MAX_HEADER_CHARS:
        pad -= 1
    cases.append(("near the size limit", "payment_required", b64(dict(near, error="e" * pad))))
    return cases


CODECS = {
    "payment_required": (decode_payment_required, encode_payment_required),
    "payment_signature": (decode_payment_signature, encode_payment_signature),
    "payment_response": (decode_payment_response, encode_payment_response),
}
EDGE_CASES = _edge_values()


@pytest.mark.parametrize("label,kind,value", EDGE_CASES, ids=[c[0] for c in EDGE_CASES])
def test_every_decoded_model_re_encodes_without_a_bare_error(label, kind, value):
    decode, encode = CODECS[kind]
    model = decode(value)
    try:
        every_encoding(model, encode)
    except X402HeaderError:
        return  # allowed (for example TooLarge if re-encoding grew the header); nothing to round-trip
    # anything else propagates and fails the test
    again = decode(encode(model))
    assert again == model


def test_compact_header_whose_floats_grow_on_re_encode_is_too_large_not_a_bare_error():
    # Compact JSON, so re-encoding cannot shrink it: every 1e15 is written back
    # as 1000000000000000.0 (5 bytes become 18), pushing it over the limit.
    def header_with(count):
        text = json.dumps(payment_msg(extensions={"a": [1e15] * count}), separators=(",", ":"))
        return base64.b64encode(text.replace("1000000000000000.0", "1e15").encode()).decode()

    count = 1
    while len(header_with(count + 1)) <= MAX_HEADER_CHARS:
        count += 1
    value = header_with(count)
    assert MAX_HEADER_CHARS - 8 < len(value) <= MAX_HEADER_CHARS
    raw = base64.b64decode(value)
    assert b", " not in raw and b": " not in raw  # compact separators: nothing to shrink
    model = decode_payment_signature(value)
    assert model.extensions["a"] == [1e15] * count
    with pytest.raises(PaymentHeaderTooLarge) as exc:
        encode_payment_signature(model)
    assert exc.value.code == "PAYMENT_HEADER_TOO_LARGE"
    # The other serialisations work: only the header encoder has a size limit.
    model.model_dump_json(by_alias=True, exclude_none=True)
    json.dumps(model.model_dump(mode="json", by_alias=True, exclude_none=True),
               ensure_ascii=False, allow_nan=False).encode("utf-8")


ADVERSARIAL = {
    "depth 256 in extensions": ("payment_signature", b64(deep_signature(256, "extensions"))),
    "depth 300 in extensions": ("payment_signature", b64(deep_signature(300, "extensions"))),
    "depth 300 in accepted.extra": ("payment_signature", b64(deep_signature(300, "extra"))),
    "depth 300 in PAYMENT-REQUIRED": ("payment_required", b64(required_msg(extensions={"x": nested(300)}))),
    "depth 300 in PAYMENT-RESPONSE": ("payment_response", b64(dict(SETTLE, extra={"x": nested(300)}))),
    "1e400": ("payment_signature", b64(json.dumps(payment_msg(extensions={"n": 0})).replace('"n": 0', '"n": 1e400'))),
    "lone surrogate": ("payment_required", b64(
        json.dumps(required_msg(error="x")).replace('"error": "x"', '"error": "\\ud800"'))),
}


@pytest.mark.parametrize("kind,value", list(ADVERSARIAL.values()), ids=list(ADVERSARIAL))
def test_a_header_is_either_refused_or_re_encodable(kind, value):
    # The invariant #607 relies on: decoding raises X402HeaderError, or the
    # model it returns can be serialised every way without a bare error.
    decode, encode = CODECS[kind]
    try:
        model = decode(value)
    except X402HeaderError:
        return
    try:
        every_encoding(model, encode)
    except X402HeaderError:
        pass
