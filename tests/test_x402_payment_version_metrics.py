"""The x402 payment metrics and audit records carry the protocol version (#601).

To decide when v1 can be removed, operators need to see how many paid requests
use each version. gateway_x402_payments_total and gateway_x402_settlements_total
have a `version` label: "1" (X-PAYMENT), "2" (PAYMENT-SIGNATURE, also together
with X-PAYMENT) or "none" (no payment header). The payment audit records
(payment_settled, payment_failed, payment_delivered) have data.x402_version.
"""
import json
import logging
from unittest.mock import patch

import pytest

from app.core.config import settings
from app.services.metrics import x402_payments_total, x402_settlements_total
from app.x402.payment_headers import paid_version_label, payment_version_label
from tests.test_x402_integration import create_valid_payment_header
from tests.test_x402_v2_payment_header import RAN, V2, gw  # noqa: F401  (fixture)

X_PAYMENT = {"X-PAYMENT": create_valid_payment_header()}
SIGNATURE = {"PAYMENT-SIGNATURE": V2}


def _payments(mode, version):
    return x402_payments_total.labels(mode=mode, version=version)._value.get()


def _settlements(result, version):
    return x402_settlements_total.labels(result=result, version=version)._value.get()


@pytest.mark.parametrize("versions,label", [
    (set(), "none"), ({1}, "1"), ({2}, "2"), ({1, 2}, "2"),
])
def test_version_label(versions, label):
    assert payment_version_label(versions) == label


def test_paid_version_label_defaults_to_1():
    class State:
        pass

    class Req:
        state = State()

    assert paid_version_label(Req()) == "1"
    Req.state.x402_version = 2
    assert paid_version_label(Req()) == "2"
    assert paid_version_label(object()) == "1"


@pytest.mark.parametrize("headers,mode,version", [
    ({}, "rejected", "none"),
    ({"X-Payment-Mode": "free"}, "free", "none"),
    (SIGNATURE, "version_unsupported", "2"),
    ({**SIGNATURE, "X-Payment-Mode": "free"}, "version_unsupported", "2"),
    ({**X_PAYMENT, **SIGNATURE}, "header_conflict", "2"),
    (X_PAYMENT, "paid", "1"),
], ids=["no-header", "free", "v2", "v2-free", "conflict", "v1-paid"])
def test_payment_attempts_are_counted_with_their_version(gw, headers, mode, version):  # noqa: F811
    client, _, _, _ = gw
    before = _payments(mode, version)
    others = {v: _payments(mode, v) for v in ("1", "2", "none") if v != version}
    client.post("/api/v1/stamps/", headers=headers)
    assert _payments(mode, version) == before + 1
    assert {v: _payments(mode, v) for v in others} == others


@pytest.mark.parametrize("headers,version", [
    (X_PAYMENT, "1"), (SIGNATURE, "2"), ({}, "none"),
], ids=["v1", "v2", "no-header"])
def test_payments_unavailable_are_counted_with_their_version(gw, headers, version):  # noqa: F811
    client, _, _, _ = gw
    before = _payments("unavailable", version)
    with patch("app.x402.accept_gate.payments_refusal", return_value="balances_unreadable"):
        r = client.post("/api/v1/stamps/", headers=headers)
    assert r.status_code == 503
    assert _payments("unavailable", version) == before + 1


def test_settlement_is_counted_as_version_1(gw):  # noqa: F811
    client, _, _, _ = gw
    before = _settlements("settled", "1")
    r = client.post("/api/v1/stamps/", headers=X_PAYMENT)
    assert r.status_code == 200, r.text
    assert _settlements("settled", "1") == before + 1


def test_settlement_refused_is_counted_as_version_1(gw):  # noqa: F811
    from x402.types import SettleResponse
    client, fac, _, _ = gw
    fac.settle.return_value = SettleResponse(success=False, errorReason="invalid_transaction_state",
                                             transaction="", network="base-sepolia")
    before = _settlements("refused", "1")
    r = client.post("/api/v1/stamps/", headers=X_PAYMENT)
    assert r.status_code == 402
    assert _settlements("refused", "1") == before + 1


def _audit_events():
    with open(settings.X402_AUDIT_LOG_PATH) as f:
        return [json.loads(line) for line in f if line.strip()]


def test_audit_records_carry_the_version(gw):  # noqa: F811
    client, _, _, _ = gw
    start = len(_audit_events())
    # Settled before delivery (settlement.py) on /stamps/, and by the middleware
    # after the response on /data/, which also writes payment_delivered.
    assert client.post("/api/v1/stamps/", headers=X_PAYMENT).status_code == 200
    other = {"X-PAYMENT": create_valid_payment_header(payer="0x" + "c3" * 20)}
    assert client.post("/api/v1/data/", headers=other).status_code == 200
    events = _audit_events()[start:]
    kinds = [e["event_type"] for e in events]
    assert kinds.count("payment_settled") == 2
    assert "payment_delivered" in kinds
    for e in events:
        if e["event_type"] in ("payment_settled", "payment_delivered"):
            assert e["data"]["x402_version"] == 1


def test_payment_failed_record_carries_the_version():
    from app.x402.audit import log_payment_failed
    start = len(_audit_events())
    log_payment_failed(client_ip="testclient", reason="r", stage="settle")
    (event,) = _audit_events()[start:]
    assert event["event_type"] == "payment_failed"
    assert event["data"]["x402_version"] == 1


@pytest.mark.parametrize("headers", [SIGNATURE, {**X_PAYMENT, **SIGNATURE}], ids=["v2", "conflict"])
def test_refusal_log_lines_say_version_2(gw, caplog, headers):  # noqa: F811
    client, _, _, _ = gw
    with caplog.at_level(logging.WARNING, logger="app.x402.dependency"):
        client.post("/api/v1/stamps/", headers=headers)
    assert any("refusing" in r.getMessage() and "version=2" in r.getMessage() for r in caplog.records)
