"""The gateway's own x402 v1 models behave exactly like x402 1.0.0's (#604).

x402==1.0.0 stays installed as a test oracle only. Each check runs the same
input through both and compares: the JSON schema (fields, order, aliases,
required-ness), the dumps the gateway writes to the wire, which inputs are
rejected and with what errors, and which unknown fields are dropped.
"""
import json

import pytest
from pydantic import ValidationError

import x402.encoding as lib_encoding
import x402.types as lib
from app.x402 import encoding as own_encoding
from app.x402 import models_v1 as own

PAIRS = [
    (own.PaymentRequirements, lib.PaymentRequirements),
    (own.PaymentPayload, lib.PaymentPayload),
    (own.ExactPaymentPayload, lib.ExactPaymentPayload),
    (own.EIP3009Authorization, lib.EIP3009Authorization),
    (own.VerifyResponse, lib.VerifyResponse),
    (own.SettleResponse, lib.SettleResponse),
]

REQUIREMENTS = {
    "scheme": "exact", "network": "base-sepolia", "maxAmountRequired": "20000",
    "resource": "https://gateway.example/api/v1/stamps/", "description": "Stamp",
    "mimeType": "application/json", "payTo": "0x" + "22" * 20, "maxTimeoutSeconds": 300,
    "asset": "0x" + "33" * 20, "extra": {"name": "USDC", "version": "2"},
}

PAYLOAD = {
    "x402Version": 1, "scheme": "exact", "network": "base-sepolia",
    "payload": {
        "signature": "0x" + "ab" * 65,
        "authorization": {
            "from": "0x" + "a1" * 20, "to": "0x" + "22" * 20, "value": "20000",
            "validAfter": "0", "validBefore": "9999999999", "nonce": "0x" + "01" * 32,
        },
    },
}


def _with(base, path, value):
    """A deep copy of base with one key set (or removed when value is ...)."""
    out = json.loads(json.dumps(base))
    node = out
    for key in path[:-1]:
        node = node[key]
    if value is ...:
        node.pop(path[-1], None)
    else:
        node[path[-1]] = value
    return out


def _errors(exc):
    return [(e["type"], e["loc"], e["msg"], e.get("input")) for e in exc.errors(include_url=False)]


def _outcome(model, data):
    """('ok', dump by alias, dump by alias without None) or ('error', errors)."""
    try:
        m = model.model_validate(data)
    except ValidationError as e:
        return ("error", _errors(e))
    return ("ok", json.dumps(m.model_dump(by_alias=True)),
            json.dumps(m.model_dump(by_alias=True, exclude_none=True)),
            json.dumps(m.model_dump()))


@pytest.mark.parametrize("own_model,lib_model", PAIRS, ids=[p[0].__name__ for p in PAIRS])
def test_schema_matches(own_model, lib_model):
    assert list(own_model.model_fields) == list(lib_model.model_fields)
    for name, field in own_model.model_fields.items():
        other = lib_model.model_fields[name]
        assert (field.alias, field.is_required(), field.default) == (other.alias, other.is_required(), other.default)
    assert own_model.model_json_schema(by_alias=True) == lib_model.model_json_schema(by_alias=True)
    assert own_model.model_config.get("extra") == lib_model.model_config.get("extra")


REQUIREMENT_CASES = [
    REQUIREMENTS,
    _with(REQUIREMENTS, ["network"], "base"),
    _with(REQUIREMENTS, ["network"], "avalanche"),
    _with(REQUIREMENTS, ["network"], "eip155:84532"),          # not a v1 name: refused
    _with(REQUIREMENTS, ["network"], "polygon"),
    _with(REQUIREMENTS, ["maxAmountRequired"], "1.5"),
    _with(REQUIREMENTS, ["maxAmountRequired"], 20000),
    _with(REQUIREMENTS, ["extra"], ...),
    _with(REQUIREMENTS, ["extra"], None),
    _with(REQUIREMENTS, ["outputSchema"], {"type": "object"}),
    _with(REQUIREMENTS, ["unknownField"], "dropped"),
    _with(REQUIREMENTS, ["payTo"], ...),
    {"scheme": "exact", "network": "base", "max_amount_required": "1", "resource": "r",
     "description": "d", "mime_type": "m", "pay_to": "p", "max_timeout_seconds": 1, "asset": "a"},
]


@pytest.mark.parametrize("data", REQUIREMENT_CASES)
def test_requirements_parse_and_dump_like_the_library(data):
    assert _outcome(own.PaymentRequirements, data) == _outcome(lib.PaymentRequirements, data)


PAYLOAD_CASES = [
    PAYLOAD,
    _with(PAYLOAD, ["extraTopLevel"], "dropped"),
    _with(PAYLOAD, ["payload", "extraInPayload"], "dropped"),
    _with(PAYLOAD, ["payload", "authorization", "extraInAuthorization"], "dropped"),
    _with(PAYLOAD, ["network"], "eip155:84532"),                 # payload network is any string
    _with(PAYLOAD, ["payload", "authorization", "value"], "1e5"),
    _with(PAYLOAD, ["payload", "authorization", "from"], ...),
    _with(PAYLOAD, ["payload", "authorization", "from_"], "0x" + "a1" * 20),
    _with(PAYLOAD, ["payload", "signature"], ...),
    _with(PAYLOAD, ["x402Version"], "1"),
    _with(PAYLOAD, ["x402Version"], 2),
    _with(PAYLOAD, ["payload"], "not an object"),
    {"x402Version": 1},
]


@pytest.mark.parametrize("data", PAYLOAD_CASES)
def test_payload_parse_and_dump_like_the_library(data):
    assert _outcome(own.PaymentPayload, data) == _outcome(lib.PaymentPayload, data)


ANSWER_CASES = [
    {"isValid": True, "payer": "0x" + "a1" * 20},
    {"isValid": False, "invalidReason": "insufficient_funds", "payer": "0x" + "a1" * 20},
    {"isValid": True},                                           # payer missing: refused by both
    {"isValid": True, "payer": None, "extra": 1},
    {"is_valid": True, "payer": None},
    {"success": True, "transaction": "0x" + "ab" * 32, "network": "base-sepolia", "payer": "0x1"},
    {"success": False, "errorReason": "invalid_transaction_state"},   # refusal without transaction
    {"success": False, "errorReason": "x", "transaction": "", "network": "base", "payer": "0x1",
     "errorMessage": "dropped", "amount": "1", "extensions": {}},
    {"error": "internal"},
    {"success": "yes"},
]


@pytest.mark.parametrize("data", ANSWER_CASES)
@pytest.mark.parametrize("name", ["VerifyResponse", "SettleResponse"])
def test_facilitator_answers_parse_like_the_library(name, data):
    assert _outcome(getattr(own, name), data) == _outcome(getattr(lib, name), data)


def test_keyword_construction_matches():
    """The gateway builds requirements by field name (create_payment_requirements)."""
    kwargs = dict(scheme="exact", network="base", max_amount_required="10000", resource="r",
                  description="d", mime_type="application/json", pay_to="0x" + "22" * 20,
                  max_timeout_seconds=300, asset="0x" + "33" * 20, extra={"name": "USD Coin", "version": "2"})
    a, b = own.PaymentRequirements(**kwargs), lib.PaymentRequirements(**kwargs)
    assert json.dumps(a.model_dump(by_alias=True)) == json.dumps(b.model_dump(by_alias=True))
    s = dict(success=True, transaction="0x" + "ab" * 32, network="base", payer="0x1")
    assert (json.dumps(own.SettleResponse(**s).model_dump(by_alias=True))
            == json.dumps(lib.SettleResponse(**s).model_dump(by_alias=True)))


def test_models_round_trip_into_each_other():
    """A library object (still built by some tests) dumps into the gateway model unchanged."""
    lib_obj = lib.PaymentPayload.model_validate(PAYLOAD)
    own_obj = own.PaymentPayload.model_validate(lib_obj.model_dump(by_alias=True))
    assert own_obj.model_dump(by_alias=True) == lib_obj.model_dump(by_alias=True)


@pytest.mark.parametrize("data", ["", "abc", "héllo ✓", b"\x00\xffbytes", json.dumps(PAYLOAD)])
def test_base64_encode_matches(data):
    assert own_encoding.safe_base64_encode(data) == lib_encoding.safe_base64_encode(data)


@pytest.mark.parametrize("data", ["", "YWJj", "aMOpbGxvIOKckw==", "YWJj\n", "not base64 !!", "YWJ", "/w=="])
def test_base64_decode_matches(data):
    def run(fn):
        try:
            return ("ok", fn(data))
        except Exception as e:
            return ("error", type(e), str(e))
    assert run(own_encoding.safe_base64_decode) == run(lib_encoding.safe_base64_decode)


def test_supported_response_parses_the_published_shape():
    data = {"kinds": [{"x402Version": 1, "scheme": "exact", "network": "base-sepolia"},
                      {"x402Version": 2, "scheme": "exact", "network": "eip155:84532", "extra": {"a": 1},
                       "unknown": "dropped"}],
            "extensions": [], "signers": {}}
    got = own.SupportedResponse.model_validate(data)
    assert got.model_dump(by_alias=True, exclude_none=True) == {
        "kinds": [{"x402Version": 1, "scheme": "exact", "network": "base-sepolia"},
                  {"x402Version": 2, "scheme": "exact", "network": "eip155:84532", "extra": {"a": 1}}]}
    with pytest.raises(ValidationError):
        own.SupportedResponse.model_validate({"error": "unauthorized"})
