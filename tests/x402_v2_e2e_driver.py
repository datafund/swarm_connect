"""Driver for tests/test_x402_v2_e2e.py: run in a fresh process, never imported by pytest.

app.main attaches the payment dependencies to its routers at import time, and
only when X402_ENABLED is true, so the real app is built in a subprocess with
the environment the test sets (as tests/test_client_facing_headers.py does).

Here only the outside world is replaced:
- the facilitator, at the HTTP layer: the real FacilitatorClient with an
  httpx.MockTransport that records each request body;
- Bee: the chain state, the funds check and the purchase call;
- the price: the stamp purchase is priced at $0.02, the amount the recorded
  @x402/evm payment signed for.

It sends the recorded PAYMENT-SIGNATURE (tests/fixtures/x402_v2/
ts_payment_signature.b64) byte for byte, twice, and prints one JSON line:
the answers and the facilitator request bodies.
"""
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "x402_v2"
SIGNATURE = (FIXTURES / "ts_payment_signature.b64").read_text().strip()
PAYER = "0x3FC695516C7E105E9c95f353cdaC3347045b4e1c"   # the fixture's throwaway signer
TX = "0x" + "00" * 32
BATCH = "f" * 64


def main() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.rsplit("/", 1)[-1]
        calls.append({"path": path, "body": json.loads(request.content)})
        if path == "verify":
            return httpx.Response(200, json={"isValid": True, "payer": PAYER})
        return httpx.Response(200, json={"success": True, "transaction": TX,
                                         "network": "eip155:84532", "payer": PAYER})

    from app.x402.facilitator_client import FacilitatorClient
    client = FacilitatorClient({"url": "https://facilitator.example"},
                               transport=httpx.MockTransport(handler))
    with patch("app.x402.facilitator.get_facilitator_client", return_value=client), \
         patch("app.x402.dependency._get_facilitator_client", return_value=client), \
         patch("app.x402.dependency._calculate_price_for_request",
               new=AsyncMock(return_value={"price_usd": 0.02,
                                           "description": "Postage stamp purchase (depth 17, 25h)"})), \
         patch("app.services.swarm_api.get_chainstate",
               new=AsyncMock(return_value={"currentPrice": "24000", "minimumValidityBlocks": 17280,
                                           "block": 180000})), \
         patch("app.services.swarm_api.check_sufficient_funds",
               new=AsyncMock(return_value={"sufficient": True, "wallet_balance_bzz": 10.0,
                                           "required_bzz": 0.01, "shortfall_bzz": 0.0})), \
         patch("app.services.swarm_api.purchase_postage_stamp", new=AsyncMock(return_value=BATCH)):
        from fastapi.testclient import TestClient
        from app.main import app
        c = TestClient(app, base_url="https://gateway.example")
        body = {"duration_hours": 25, "depth": 17}
        offer = c.post("/api/v1/stamps/", json=body)
        first = c.post("/api/v1/stamps/", json=body, headers={"PAYMENT-SIGNATURE": SIGNATURE})
        second = c.post("/api/v1/stamps/", json=body, headers={"PAYMENT-SIGNATURE": SIGNATURE})

    def answer(r):
        return {"status": r.status_code, "headers": {k.lower(): v for k, v in r.headers.items()},
                "body": r.json()}

    print(json.dumps({"offer": answer(offer), "first": answer(first), "second": answer(second),
                      "calls": calls}))


if __name__ == "__main__":
    sys.exit(main())
