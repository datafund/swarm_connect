"""End to end: a recorded @x402/evm v2 payment, through the real app.main (#607).

The payment is tests/fixtures/x402_v2/ts_payment_signature.b64, made by
@x402/evm 2.30.0's x402Client for the fixture offer (payment_required.json):
exact, eip155:84532, Base Sepolia USDC, amount 20000, payTo
0x0000000000000000000000000000000000000001, maxTimeoutSeconds 300, extra
{"name": "USDC", "version": "2"}. It is sent unchanged. It is not rebuilt:
the gateway is configured so that its own offer for POST /api/v1/stamps/ is
exactly that offer (X402_NETWORK=base-sepolia, X402_PAY_TO_ADDRESS the
fixture's placeholder, the price $0.02). If the gateway's offer for these
settings ever changes (another field, other token metadata), the payment must
be signed again for the new offer: regenerate the fixtures as
tests/fixtures/x402_v2/README.md says, from the gateway's PAYMENT-REQUIRED.

The clock does not matter. The fixture's validBefore is a fixed time set
when it was recorded, but the gateway checks no validity window itself
(nothing in app/ reads validAfter or validBefore): that is the
facilitator's job, and the facilitator is mocked here. A test below pins
that, so a future expiry check in the gateway would fail it loudly rather
than make this test depend on the date.

The real app is built in a subprocess (tests/x402_v2_e2e_driver.py), because
app.main attaches the payment dependencies at import time. Only the
facilitator (at the HTTP layer), Bee and the price are replaced.
"""
import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures" / "x402_v2"
SIGNED = json.loads(base64.b64decode((FIXTURES / "ts_payment_signature.b64").read_text().strip()))
OFFER = json.loads((FIXTURES / "payment_required.json").read_text())["accepts"][0]
PAY_TO = OFFER["payTo"]


@pytest.fixture(scope="module")
def run():
    env = {**os.environ, "X402_ENABLED": "true", "X402_PROTOCOL_VERSIONS": "1,2",
           "X402_NETWORK": "base-sepolia", "X402_PAY_TO_ADDRESS": PAY_TO,
           "X402_FREE_TIER_ENABLED": "false", "RATE_LIMIT_ENABLED": "false",
           "STAMP_POOL_ENABLED": "false", "METRICS_ENABLED": "false",
           "TRUSTED_PROXY_HOPS": "0", "SWARM_BEE_API_URL": "http://localhost:1"}
    out = subprocess.run([sys.executable, "-m", "tests.x402_v2_e2e_driver"], cwd=REPO,
                         env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-3000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def _typed(obj):
    """The fixture payment as the gateway forwards it: the typed dump, by alias,
    without None fields (the recorded payment has none) and in model order."""
    return {
        "x402Version": 2,
        "resource": SIGNED["resource"],
        "accepted": {k: OFFER[k] for k in ("scheme", "network", "asset", "amount", "payTo",
                                            "maxTimeoutSeconds", "extra")},
        "payload": {"signature": SIGNED["payload"]["signature"],
                    "authorization": SIGNED["payload"]["authorization"]},
    }


def test_the_gateway_offer_is_the_one_the_fixture_signed(run):
    offer = run["offer"]
    assert offer["status"] == 402
    v1 = offer["body"]["accepts"][0]
    assert (v1["network"], v1["asset"], v1["maxAmountRequired"], v1["payTo"], v1["maxTimeoutSeconds"],
            v1["extra"]) == ("base-sepolia", OFFER["asset"], OFFER["amount"], OFFER["payTo"],
                             OFFER["maxTimeoutSeconds"], OFFER["extra"])
    assert SIGNED["accepted"] == OFFER


def test_recorded_v2_payment_is_verified_settled_and_delivered(run):
    first = run["first"]
    assert first["status"] == 201, first
    assert first["body"]["batchID"] == "f" * 64
    assert first["headers"]["x-payment-mode"] == "paid"
    assert "x-payment-response" not in first["headers"]
    receipt = json.loads(base64.b64decode(first["headers"]["payment-response"]))
    assert receipt == {"success": True, "transaction": "0x" + "00" * 32, "network": "eip155:84532",
                       "payer": SIGNED["payload"]["authorization"]["from"]}


def test_exact_verify_and_settle_bodies(run):
    expected = {
        "x402Version": 2,
        "paymentPayload": _typed(SIGNED),
        "paymentRequirements": OFFER,
    }
    assert [c["path"] for c in run["calls"]][:2] == ["verify", "settle"]
    for call in run["calls"][:2]:
        assert call["body"] == expected
        # Key order too: what the typed models dump, compact.
        assert list(call["body"]["paymentPayload"]) == ["x402Version", "resource", "accepted", "payload"]
        assert list(call["body"]["paymentRequirements"]) == ["scheme", "network", "asset", "amount", "payTo",
                                                             "maxTimeoutSeconds", "extra"]


def test_the_same_signature_again_is_refused_unpaid(run):
    second = run["second"]
    assert second["status"] == 402
    assert "already been used" in second["body"]["error"]
    assert [c["path"] for c in run["calls"]] == ["verify", "settle", "verify"]


def test_the_gateway_checks_no_validity_window_itself():
    """Why the recorded validBefore cannot make this test depend on the date."""
    hits = []
    for path in (REPO / "app").rglob("*.py"):
        text = path.read_text()
        for name in ("valid_before", "validBefore", "valid_after", "validAfter"):
            for line in text.splitlines():
                if name in line and not path.name.startswith("models_v"):
                    hits.append((path.name, line.strip()))
    # Only advice text to clients mentions validBefore; no code compares it.
    assert all("validBefore" in line and ("wait" in line or "passed" in line) for _, line in hits), hits
