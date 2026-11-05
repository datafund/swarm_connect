"""Write gateway_payment_required.b64: payment_required.json encoded by the gateway (#606).

Run from the repository root with the repository's environment:

    python tests/fixtures/x402_v2/make_gateway_header.py

The output is deterministic. tests/test_x402_v2_interop.py checks that the
gateway still encodes exactly these bytes, so the recorded TypeScript and
Python decodes of it stay valid.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))

from app.x402.codec_v2 import encode_payment_required  # noqa: E402
from app.x402.models_v2 import PaymentRequired  # noqa: E402

message = PaymentRequired.model_validate(json.loads((HERE / "payment_required.json").read_text()))
(HERE / "gateway_payment_required.b64").write_text(encode_payment_required(message) + "\n")
print("wrote gateway_payment_required.b64")
