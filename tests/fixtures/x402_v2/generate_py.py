"""Generates the Python x402 2.27.0 side of the x402 v2 interop fixtures (#606).

Run it with a throwaway environment, never the repository's (see README.md):

    uv venv /tmp/x402-227 && VIRTUAL_ENV=/tmp/x402-227 uv pip install "x402[evm]==2.27.0"
    /tmp/x402-227/bin/python tests/fixtures/x402_v2/generate_py.py

It reads payment_required.json and gateway_payment_required.b64 next to this
file and writes:
    py_payment_required.b64   PAYMENT-REQUIRED from encode_payment_required_header
    py_payment_signature.b64  PAYMENT-SIGNATURE: a payment for accepts[0], signed
                              with a throwaway key made here
    py_payment_response.b64   PAYMENT-RESPONSE from encode_payment_response_header
                              (a synthetic success; no facilitator is called)
    py_decoded_gateway_payment_required.json
                              what the 2.27 client makes of the gateway's header

The key is generated on each run, used once and never written anywhere.
Nothing here touches a network. It does not import the gateway.
"""
import importlib.metadata
import json
from pathlib import Path

from eth_account import Account
from x402 import x402ClientSync
from x402.http.utils import (
    decode_payment_required_header,
    encode_payment_required_header,
    encode_payment_response_header,
    encode_payment_signature_header,
)
from x402.mechanisms.evm.exact.register import register_exact_evm_client
from x402.mechanisms.evm.signers import EthAccountSigner
from x402.schemas import PaymentRequired, SettleResponse

HERE = Path(__file__).resolve().parent


def write(name, text):
    (HERE / name).write_text(text + "\n")


versions = {p: importlib.metadata.version(p) for p in ("x402", "eth-account", "pydantic")}

# A throwaway signer: random, in memory only.
account = Account.create()
client = x402ClientSync()
register_exact_evm_client(client, EthAccountSigner(account))

# 1. PAYMENT-REQUIRED built by x402 2.27 from the gateway-like offer.
offer = PaymentRequired.model_validate(json.loads((HERE / "payment_required.json").read_text()))
required = encode_payment_required_header(offer)
write("py_payment_required.b64", required)

# 2. PAYMENT-SIGNATURE: what the 2.27 client sends after reading that 402.
payment = client.create_payment_payload(decode_payment_required_header(required))
write("py_payment_signature.b64", encode_payment_signature_header(payment))

# 3. PAYMENT-RESPONSE for a synthetic success (placeholder transaction hash).
write("py_payment_response.b64", encode_payment_response_header(SettleResponse(
    success=True, transaction="0x" + "00" * 32, network="eip155:84532", payer=account.address)))

# 4. The gateway's own PAYMENT-REQUIRED, read the way the 2.27 client reads it.
header = (HERE / "gateway_payment_required.b64").read_text().strip()
result = {"versions": versions, "header": header}
try:
    decoded = decode_payment_required_header(header)
    result["decoded"] = decoded.model_dump(mode="json", by_alias=True, exclude_none=True)
    result["decodedType"] = type(decoded).__name__
    paid = client.create_payment_payload(decoded)
    result["paymentAccepted"] = paid.accepted.model_dump(mode="json", by_alias=True, exclude_none=True)
    result["paymentResource"] = paid.resource.model_dump(mode="json", by_alias=True, exclude_none=True)
    result["ok"] = True
except Exception as exc:  # recorded, so a failure shows in the fixture
    result["ok"] = False
    result["error"] = f"{type(exc).__name__}: {exc}"
write("py_decoded_gateway_payment_required.json", json.dumps(result, indent=2))
print(f"wrote Python fixtures with {versions}; gateway header ok={result['ok']}")
