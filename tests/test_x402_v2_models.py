"""The x402 v2 models follow @x402/core 2.30.0 (#606).

Field rules: docs/adr/x402-v2.md, Question 6. Where the gateway is stricter
than the reference (integer-string amounts, integer maxTimeoutSeconds) or
pins a choice (unknown fields dropped), the test says so.
"""
import copy

import pytest
from pydantic import ValidationError

from app.x402.models_v2 import (
    MAX_PAYMENT_ACCEPTS,
    EIP3009Authorization,
    ExactEvmPayloadV2,
    PaymentPayloadV2,
    PaymentRequired,
    PaymentRequirementsV2,
    ResourceInfo,
    SettleResponseV2,
    VerifyResponseV2,
)

SEPOLIA_USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
PAY_TO = "0x0000000000000000000000000000000000000001"
PAYER = "0x0000000000000000000000000000000000000002"

OFFER = {
    "scheme": "exact",
    "network": "eip155:84532",
    "asset": SEPOLIA_USDC,
    "amount": "20000",
    "payTo": PAY_TO,
    "maxTimeoutSeconds": 300,
    "extra": {"name": "USDC", "version": "2"},
}
RESOURCE = {
    "url": "https://gateway.example/api/v1/stamps/",
    "description": "Postage stamp purchase",
    "mimeType": "application/json",
}
AUTHORIZATION = {
    "from": PAYER,
    "to": PAY_TO,
    "value": "20000",
    "validAfter": "0",
    "validBefore": "1999999999",
    "nonce": "0x" + "11" * 32,
}


def required_msg(**over):
    msg = {"x402Version": 2, "error": "Payment required", "resource": dict(RESOURCE), "accepts": [dict(OFFER)]}
    msg.update(over)
    return msg


def payment_msg(**over):
    msg = {
        "x402Version": 2,
        "resource": dict(RESOURCE),
        "accepted": dict(OFFER),
        "payload": {"signature": "0x" + "ab" * 65, "authorization": dict(AUTHORIZATION)},
    }
    msg.update(over)
    return msg


def dump(model):
    return model.model_dump(by_alias=True, exclude_none=True)


# --- happy paths ------------------------------------------------------------

def test_payment_required_round_trips_the_adr_shape():
    m = PaymentRequired.model_validate(required_msg())
    assert dump(m) == required_msg()


def test_payment_payload_round_trips_and_types_the_exact_payload():
    m = PaymentPayloadV2.model_validate(payment_msg())
    assert isinstance(m.payload, ExactEvmPayloadV2)
    assert m.exact_payload.authorization.from_ == PAYER
    assert dump(m) == payment_msg()


def test_dump_key_order_follows_the_reference_types():
    assert list(dump(PaymentRequired.model_validate(required_msg()))) == [
        "x402Version", "error", "resource", "accepts"]
    assert list(dump(PaymentRequirementsV2.model_validate(OFFER))) == [
        "scheme", "network", "asset", "amount", "payTo", "maxTimeoutSeconds", "extra"]


def test_optional_fields_may_be_left_out():
    m = PaymentRequired.model_validate(
        {"x402Version": 2, "resource": {"url": "https://gateway.example/"}, "accepts": [dict(OFFER)]})
    assert m.error is None and m.extensions is None
    p = PaymentPayloadV2.model_validate(payment_msg(resource=None))
    assert p.resource is None and "resource" not in dump(p)


# --- required fields --------------------------------------------------------

@pytest.mark.parametrize("field", ["scheme", "network", "asset", "amount", "payTo", "maxTimeoutSeconds"])
def test_offer_field_is_required(field):
    offer = dict(OFFER)
    del offer[field]
    with pytest.raises(ValidationError):
        PaymentRequirementsV2.model_validate(offer)


@pytest.mark.parametrize("field", ["scheme", "asset", "payTo", "amount"])
def test_offer_text_field_must_not_be_empty(field):
    with pytest.raises(ValidationError):
        PaymentRequirementsV2.model_validate(dict(OFFER, **{field: ""}))


@pytest.mark.parametrize("field", ["x402Version", "resource", "accepts"])
def test_payment_required_field_is_required(field):
    msg = required_msg()
    del msg[field]
    with pytest.raises(ValidationError):
        PaymentRequired.model_validate(msg)


def test_resource_url_is_required_and_non_empty():
    with pytest.raises(ValidationError):
        ResourceInfo.model_validate({"description": "x"})
    with pytest.raises(ValidationError):
        ResourceInfo.model_validate({"url": ""})


@pytest.mark.parametrize("field", ["x402Version", "accepted", "payload"])
def test_payment_payload_field_is_required(field):
    msg = payment_msg()
    del msg[field]
    with pytest.raises(ValidationError):
        PaymentPayloadV2.model_validate(msg)


@pytest.mark.parametrize("version", [1, 3, "2"])
def test_x402_version_must_be_2(version):
    with pytest.raises(ValidationError):
        PaymentRequired.model_validate(required_msg(x402Version=version))
    with pytest.raises(ValidationError):
        PaymentPayloadV2.model_validate(payment_msg(x402Version=version))


# --- network ----------------------------------------------------------------

@pytest.mark.parametrize("network", ["eip155:84532", "eip155:8453", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp", "abc:1"])
def test_caip2_networks_are_accepted(network):
    assert PaymentRequirementsV2.model_validate(dict(OFFER, network=network)).network == network


@pytest.mark.parametrize("network", [
    "base-sepolia", "base", "EIP155:1", "ei:1", "eip155:" + "1" * 33, "", "eip155:84532\n",
    "eip155:", ":1", "eip155:8453:1", "abcdefghi:1",
])
def test_non_caip2_networks_are_rejected(network):
    with pytest.raises(ValidationError):
        PaymentRequirementsV2.model_validate(dict(OFFER, network=network))


# --- accepts ----------------------------------------------------------------

def test_accepts_must_not_be_empty():
    with pytest.raises(ValidationError):
        PaymentRequired.model_validate(required_msg(accepts=[]))


def test_accepts_holds_up_to_1024_offers():
    assert MAX_PAYMENT_ACCEPTS == 1024
    assert len(PaymentRequired.model_validate(required_msg(accepts=[dict(OFFER)] * 1024)).accepts) == 1024
    with pytest.raises(ValidationError):
        PaymentRequired.model_validate(required_msg(accepts=[dict(OFFER)] * 1025))


# --- ResourceInfo limits ----------------------------------------------------

def test_resource_limits_accept_the_maximum():
    r = ResourceInfo.model_validate({
        "url": "https://gateway.example/",
        "serviceName": "s" * 32,
        "tags": ["t" * 32] * 5,
        "iconUrl": "https://gateway.example/" + "i" * (2048 - 24),
    })
    assert len(r.icon_url) == 2048


@pytest.mark.parametrize("over", [
    {"serviceName": "s" * 33},
    {"serviceName": ""},
    {"serviceName": "café"},
    {"serviceName": "tab\there"},
    {"tags": ["t"] * 6},
    {"tags": ["t" * 33]},
    {"tags": [""]},
    {"tags": ["ü"]},
    {"iconUrl": "i" * 2049},
])
def test_resource_limits_are_enforced(over):
    with pytest.raises(ValidationError):
        ResourceInfo.model_validate(dict({"url": "https://gateway.example/"}, **over))


def test_resource_nulls_count_as_absent():
    r = ResourceInfo.model_validate({"url": "https://gateway.example/", "description": None,
                                     "mimeType": None, "serviceName": None, "tags": None, "iconUrl": None})
    assert dump(r) == {"url": "https://gateway.example/"}


# --- extra ------------------------------------------------------------------

def test_extra_may_be_absent_or_null_and_is_then_left_out():
    offer = dict(OFFER)
    del offer["extra"]
    assert "extra" not in dump(PaymentRequirementsV2.model_validate(offer))
    assert "extra" not in dump(PaymentRequirementsV2.model_validate(dict(OFFER, extra=None)))


@pytest.mark.parametrize("extra", [[], "USDC", 2])
def test_extra_must_be_an_object(extra):
    with pytest.raises(ValidationError):
        PaymentRequirementsV2.model_validate(dict(OFFER, extra=extra))


# --- amounts (stricter than @x402/core: integer strings only) ----------------

@pytest.mark.parametrize("amount", ["20000", "0", "115792089237316195423570985008687907853269984665640564039457584007913129639935"])
def test_integer_string_amounts_are_accepted(amount):
    assert PaymentRequirementsV2.model_validate(dict(OFFER, amount=amount)).amount == amount


@pytest.mark.parametrize("amount", ["", "1.5", "-1", "+1", "1e6", " 1", "1 ", "abc", "0x10", "١", 20000, None])
def test_invalid_amounts_are_rejected(amount):
    with pytest.raises(ValidationError):
        PaymentRequirementsV2.model_validate(dict(OFFER, amount=amount))


@pytest.mark.parametrize("field", ["value", "validAfter", "validBefore"])
@pytest.mark.parametrize("bad", ["1.5", "-1", "abc", "", 5])
def test_authorization_amounts_are_integer_strings(field, bad):
    with pytest.raises(ValidationError):
        EIP3009Authorization.model_validate(dict(AUTHORIZATION, **{field: bad}))


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "300"])
def test_max_timeout_seconds_is_a_positive_integer(bad):
    with pytest.raises(ValidationError):
        PaymentRequirementsV2.model_validate(dict(OFFER, maxTimeoutSeconds=bad))


# --- unknown fields are dropped at every level -------------------------------

def test_unknown_fields_are_dropped_everywhere():
    msg = payment_msg()
    msg["future"] = 1
    msg["accepted"]["assetTransferMethod"] = "permit2"
    msg["resource"]["unknown"] = "x"
    msg["payload"]["permit2Authorization"] = {"a": 1}
    msg["payload"]["authorization"]["memo"] = "y"
    out = dump(PaymentPayloadV2.model_validate(msg))
    assert out == payment_msg()


def test_unknown_fields_are_dropped_from_payment_required():
    msg = required_msg()
    msg["future"] = 1
    msg["accepts"][0]["unknown"] = 2
    assert dump(PaymentRequired.model_validate(msg)) == required_msg()


def test_free_form_records_keep_their_contents():
    extensions = {"bazaar": {"discoverable": True}}
    m = PaymentRequired.model_validate(required_msg(extensions=extensions,
                                                    accepts=[dict(OFFER, extra={"name": "USDC", "x": [1]})]))
    assert dump(m)["extensions"] == extensions
    assert dump(m)["accepts"][0]["extra"] == {"name": "USDC", "x": [1]}


# --- the exact payload --------------------------------------------------------

def test_malformed_exact_payload_is_invalid_under_payload():
    msg = payment_msg()
    del msg["payload"]["authorization"]["nonce"]
    with pytest.raises(ValidationError) as exc:
        PaymentPayloadV2.model_validate(msg)
    assert [e["loc"] for e in exc.value.errors()] == [("payload", "authorization", "nonce")]


def test_exact_payload_without_signature_is_invalid():
    msg = payment_msg()
    del msg["payload"]["signature"]
    with pytest.raises(ValidationError):
        PaymentPayloadV2.model_validate(msg)


@pytest.mark.parametrize("payload", [[], "x", None])
def test_payload_must_be_an_object(payload):
    with pytest.raises(ValidationError):
        PaymentPayloadV2.model_validate(payment_msg(payload=payload))


def test_another_scheme_keeps_payload_as_a_plain_object():
    msg = payment_msg(accepted=dict(OFFER, scheme="upto"))
    msg["payload"]["anything"] = 1
    m = PaymentPayloadV2.model_validate(msg)
    assert isinstance(m.payload, dict) and m.exact_payload is None
    assert m.payload["anything"] == 1


def test_model_construction_by_field_name():
    m = PaymentPayloadV2(
        x402_version=2,
        accepted=PaymentRequirementsV2.model_validate(OFFER),
        payload=ExactEvmPayloadV2.model_validate(payment_msg()["payload"]),
    )
    assert dump(m)["payload"]["authorization"]["from"] == PAYER


# --- facilitator answers ------------------------------------------------------

def test_verify_response_minimal_and_full():
    assert dump(VerifyResponseV2.model_validate({"isValid": True})) == {"isValid": True}
    full = {"isValid": False, "invalidReason": "r", "invalidMessage": "m", "payer": PAYER,
            "extensions": {}, "extensionResponses": {"e": 1}, "extra": {"k": "v"}}
    assert dump(VerifyResponseV2.model_validate(full)) == full
    with pytest.raises(ValidationError):
        VerifyResponseV2.model_validate({"payer": PAYER})


def test_settle_response_success_refusal_and_full():
    ok = {"success": True, "transaction": "0x" + "00" * 32, "network": "eip155:84532", "payer": PAYER}
    assert dump(SettleResponseV2.model_validate(ok)) == ok
    refusal = {"success": False, "errorReason": "invalid_exact_evm_non_canonical_signature",
               "transaction": "", "network": "eip155:84532", "payer": PAYER}
    assert dump(SettleResponseV2.model_validate(refusal)) == {
        "success": False, "transaction": "", "network": "eip155:84532",
        "errorReason": "invalid_exact_evm_non_canonical_signature", "payer": PAYER}
    full = dict(ok, errorReason="r", errorMessage="m", amount="1", extensions={}, extensionResponses={}, extra={})
    assert dump(SettleResponseV2.model_validate(full)) == full


@pytest.mark.parametrize("field", ["success", "transaction", "network"])
def test_settle_response_required_fields(field):
    ok = {"success": True, "transaction": "0x1", "network": "eip155:84532"}
    del ok[field]
    with pytest.raises(ValidationError):
        SettleResponseV2.model_validate(ok)


def test_inputs_are_not_mutated():
    msg = payment_msg()
    before = copy.deepcopy(msg)
    PaymentPayloadV2.model_validate(msg)
    assert msg == before


# --- strict booleans on the facilitator answers --------------------------------

@pytest.mark.parametrize("value", ["yes", "true", "1", 1, 0, "false", None, 1.0])
def test_verify_is_valid_is_a_strict_boolean(value):
    with pytest.raises(ValidationError):
        VerifyResponseV2.model_validate({"isValid": value})


@pytest.mark.parametrize("value", ["yes", "true", "1", 1, 0, "false", None, 1.0])
def test_settle_success_is_a_strict_boolean(value):
    with pytest.raises(ValidationError):
        SettleResponseV2.model_validate({"success": value, "transaction": "", "network": "eip155:84532"})


@pytest.mark.parametrize("value", [True, False])
def test_json_booleans_are_accepted(value):
    assert VerifyResponseV2.model_validate({"isValid": value}).is_valid is value
    assert SettleResponseV2.model_validate(
        {"success": value, "transaction": "", "network": "eip155:84532"}).success is value
