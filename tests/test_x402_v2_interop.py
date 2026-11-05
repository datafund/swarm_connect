"""x402 v2 interop with the official libraries, from recorded fixtures (#606).

tests/fixtures/x402_v2/ holds header values produced by @x402/core and
@x402/evm 2.30.0 and by Python x402 2.27.0 (README.md there says how). CI
has neither node nor x402 2.x, so the reverse direction is recorded too: each
library decoded the gateway's PAYMENT-REQUIRED and built a payment for it.
That record holds only while the gateway still emits the same bytes, and the
first test below checks exactly that.
"""
import base64
import json
from pathlib import Path

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from app.x402.codec_v2 import (
    decode_payment_required,
    decode_payment_response,
    decode_payment_signature,
    encode_payment_required,
)
from app.x402.models_v2 import ExactEvmPayloadV2, PaymentRequired

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "x402_v2"
LIBRARIES = ["ts", "py"]
SEPOLIA_USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"


def header(name):
    return (FIXTURES / name).read_text().strip()


def recorded(name):
    return json.loads((FIXTURES / name).read_text())


OFFER_JSON = recorded("payment_required.json")
OFFER = PaymentRequired.model_validate(OFFER_JSON)


def dump(model):
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)


def recover_eip3009_signer(accepted, exact: ExactEvmPayloadV2):
    """The address that signed the EIP-3009 TransferWithAuthorization."""
    auth = exact.authorization
    typed = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "TransferWithAuthorization": [
                {"name": "from", "type": "address"},
                {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "validAfter", "type": "uint256"},
                {"name": "validBefore", "type": "uint256"},
                {"name": "nonce", "type": "bytes32"},
            ],
        },
        "primaryType": "TransferWithAuthorization",
        "domain": {
            "name": accepted.extra["name"],
            "version": accepted.extra["version"],
            "chainId": int(accepted.network.split(":")[1]),
            "verifyingContract": accepted.asset,
        },
        "message": {
            "from": auth.from_,
            "to": auth.to,
            "value": int(auth.value),
            "validAfter": int(auth.valid_after),
            "validBefore": int(auth.valid_before),
            "nonce": bytes.fromhex(auth.nonce[2:]),
        },
    }
    return Account.recover_message(encode_typed_data(full_message=typed), signature=exact.signature)


# --- gateway -> libraries (recorded) -------------------------------------------

def test_gateway_still_encodes_the_recorded_payment_required_bytes():
    # If this fails, the codec's output changed: regenerate the fixtures
    # (README.md) so the recorded library decodes below stay true.
    assert encode_payment_required(OFFER) == header("gateway_payment_required.b64")


def test_gateway_payment_required_is_byte_identical_to_python_x402_227():
    assert header("gateway_payment_required.b64") == header("py_payment_required.b64")


@pytest.mark.parametrize("lib", LIBRARIES)
def test_library_accepted_the_gateway_payment_required(lib):
    rec = recorded(f"{lib}_decoded_gateway_payment_required.json")
    assert rec["ok"] is True, rec.get("error")
    assert rec["header"] == header("gateway_payment_required.b64")
    assert rec["decoded"] == dump(OFFER)
    # ... and could pay it: the payment it built is for the gateway's offer.
    assert rec["paymentAccepted"] == dump(OFFER.accepts[0])
    assert rec["paymentResource"] == dump(OFFER.resource)


def test_typescript_parsed_the_gateway_header_the_way_a_client_does():
    rec = recorded("ts_decoded_gateway_payment_required.json")
    assert rec["versions"]["@x402/core"] == "2.30.0"
    assert rec["versions"]["@x402/evm"] == "2.30.0"
    # decodePaymentRequiredHeader, PaymentRequiredV2Schema.parse and
    # x402HTTPClient.getPaymentRequiredResponse all give the same message.
    assert rec["schemaParsed"] == dump(OFFER)
    assert rec["clientParsed"] == dump(OFFER)


def test_python_227_parsed_the_gateway_header_as_v2():
    rec = recorded("py_decoded_gateway_payment_required.json")
    assert rec["versions"]["x402"] == "2.27.0"
    assert rec["decodedType"] == "PaymentRequired"


# --- libraries -> gateway ---------------------------------------------------------

@pytest.mark.parametrize("lib", LIBRARIES)
def test_gateway_decodes_the_library_payment_required(lib):
    assert decode_payment_required(header(f"{lib}_payment_required.b64")) == OFFER


@pytest.mark.parametrize("lib", LIBRARIES)
def test_gateway_decodes_the_library_payment_signature(lib):
    payment = decode_payment_signature(header(f"{lib}_payment_signature.b64"))
    assert payment.x402_version == 2
    assert payment.accepted == OFFER.accepts[0]
    assert payment.resource == OFFER.resource
    exact = payment.exact_payload
    assert isinstance(exact, ExactEvmPayloadV2)
    auth = exact.authorization
    assert auth.to == OFFER.accepts[0].pay_to
    assert auth.value == OFFER.accepts[0].amount
    assert int(auth.valid_before) > int(auth.valid_after)
    assert len(bytes.fromhex(auth.nonce[2:])) == 32
    assert len(bytes.fromhex(exact.signature[2:])) == 65


@pytest.mark.parametrize("lib", LIBRARIES)
def test_library_signature_is_a_real_eip3009_signature_by_the_payer(lib):
    payment = decode_payment_signature(header(f"{lib}_payment_signature.b64"))
    assert payment.accepted.asset == SEPOLIA_USDC
    signer = recover_eip3009_signer(payment.accepted, payment.exact_payload)
    assert signer == payment.exact_payload.authorization.from_


@pytest.mark.parametrize("lib", LIBRARIES)
def test_gateway_decodes_the_library_payment_response(lib):
    receipt = decode_payment_response(header(f"{lib}_payment_response.b64"))
    assert receipt.success is True
    assert receipt.network == "eip155:84532"
    assert receipt.transaction == "0x" + "00" * 32
    payment = decode_payment_signature(header(f"{lib}_payment_signature.b64"))
    assert receipt.payer == payment.exact_payload.authorization.from_


def test_python_227_payment_response_has_spaced_json_and_still_decodes():
    # x402 2.27 encode_payment_response_header uses json.dumps defaults, with
    # spaces; any JSON object is fine for the decoder.
    assert b", " in base64.b64decode(header("py_payment_response.b64"))


def test_reencoding_a_library_payment_keeps_every_field_the_gateway_knows():
    for lib in LIBRARIES:
        value = header(f"{lib}_payment_signature.b64")
        original = json.loads(base64.b64decode(value))
        assert dump(decode_payment_signature(value)) == original


def test_signer_recovery_detects_a_changed_authorization():
    # Guards the check above: a changed amount no longer recovers the payer.
    payment = decode_payment_signature(header("ts_payment_signature.b64"))
    exact = payment.exact_payload.model_copy(deep=True)
    exact.authorization.value = "20001"
    assert recover_eip3009_signer(payment.accepted, exact) != exact.authorization.from_
