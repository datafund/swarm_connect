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
    assert paid_version_label(None) == "1"


def test_paid_version_label_reads_the_headers_before_verification():
    """Before the dependency records the verified version, the headers decide."""
    from starlette.datastructures import Headers

    class State:
        pass

    def req(headers, version=None):
        r = type("Req", (), {})()
        r.state = State()
        if version is not None:
            r.state.x402_version = version
        r.headers = Headers(headers)
        return r

    assert paid_version_label(req({"X-PAYMENT": "x"})) == "1"
    assert paid_version_label(req({"PAYMENT-SIGNATURE": "x"})) == "2"
    assert paid_version_label(req({"X-PAYMENT": "x", "PAYMENT-SIGNATURE": "x"})) == "2"
    assert paid_version_label(req({})) == "1"
    assert paid_version_label(req({"PAYMENT-SIGNATURE": "x"}, version=1)) == "1"   # recorded wins


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
    # The audit log is created on its first event: absent when a test runs alone.
    try:
        with open(settings.X402_AUDIT_LOG_PATH) as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


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


def _refused_after_unknown_at_verify(client, fac):
    from x402.types import VerifyResponse
    fac.verify.return_value = VerifyResponse(isValid=False, invalidReason="invalid_nonce",
                                             payer="0x" + "a1" * 20)
    with patch("app.x402.settlement.outcome_unknown.state", return_value=(False, "before_work")):
        return client.post("/api/v1/stamps/", headers=X_PAYMENT)


def test_refused_after_unknown_at_verify_is_counted_as_version_1(gw):  # noqa: F811
    client, fac, _, _ = gw
    before = _settlements("refused_after_unknown", "1")
    r = _refused_after_unknown_at_verify(client, fac)
    assert r.status_code == 409, r.text
    assert _settlements("refused_after_unknown", "1") == before + 1


def test_refused_after_unknown_at_verify_takes_the_version_from_the_headers(gw):  # noqa: F811
    """Not a hard-coded 1: the label is the one computed from the request's headers.

    Only v1 reaches verify today, so the headers are made to read as v2 here.
    """
    client, fac, _, _ = gw
    v1, v2 = _settlements("refused_after_unknown", "1"), _settlements("refused_after_unknown", "2")
    with patch("app.x402.dependency.payment_version_label", return_value="2"):
        r = _refused_after_unknown_at_verify(client, fac)
    assert r.status_code == 409, r.text
    assert _settlements("refused_after_unknown", "2") == v2 + 1
    assert _settlements("refused_after_unknown", "1") == v1


def test_audit_records_take_the_version_from_the_request(gw):  # noqa: F811
    """Every payment_settled / payment_failed / payment_delivered record is written
    with the request's version, not the audit module's default."""
    client, fac, _, _ = gw
    from x402.types import SettleResponse
    start = len(_audit_events())
    with patch("app.x402.payment_headers.paid_version_label", return_value="2"):
        assert client.post("/api/v1/stamps/", headers=X_PAYMENT).status_code == 200
        other = {"X-PAYMENT": create_valid_payment_header(payer="0x" + "c3" * 20)}
        assert client.post("/api/v1/data/", headers=other).status_code == 200
        fac.settle.return_value = SettleResponse(success=False, errorReason="invalid_transaction_state",
                                                 transaction="", network="base-sepolia")
        third = {"X-PAYMENT": create_valid_payment_header(payer="0x" + "d4" * 20)}
        assert client.post("/api/v1/stamps/", headers=third).status_code == 402
    events = [e for e in _audit_events()[start:]
              if e["event_type"] in ("payment_settled", "payment_failed", "payment_delivered")]
    assert {e["event_type"] for e in events} >= {"payment_settled", "payment_delivered"}
    assert [e["data"]["x402_version"] for e in events] == [2] * len(events)


def test_every_payment_audit_call_passes_the_version():
    """No call site in app/ relies on the audit default (PAID_X402_VERSION)."""
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "app"
    missing, found = [], 0
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) in (
                    "log_payment_settled", "log_payment_failed"):
                found += 1
                if "x402_version" not in {k.arg for k in node.keywords}:
                    missing.append(f"{path.relative_to(root.parent)}:{node.lineno}")
    assert found >= 14
    assert missing == []


def _v2_payload_header(payer="0x" + "e5" * 20):
    """An X-PAYMENT (v1 header) whose payload claims x402Version 2."""
    from x402.encoding import safe_base64_encode
    payload = {
        "x402Version": 2, "scheme": "exact", "network": "base-sepolia",
        "payload": {"signature": "0x" + "ab" * 65, "authorization": {
            "from": payer, "to": "0xpayee", "value": "100000", "validAfter": "0",
            "validBefore": "9999999999", "nonce": "0x" + "0e" * 32}},
    }
    return {"X-PAYMENT": safe_base64_encode(json.dumps(payload).encode("utf-8"))}


def _paid_request(client, headers, **patches):
    """Send a paid request; return (request.state.x402_version, new payment_settled records)."""
    from app.x402 import idempotency
    seen = []
    real = idempotency.record_settlement

    def spy(request, result):
        seen.append(request.state.x402_version)
        return real(request, result)

    start = len(_audit_events())
    with patch("app.x402.idempotency.record_settlement", side_effect=spy):
        if patches:
            with patch("app.x402.dependency.payment_version_label", **patches):
                r = client.post("/api/v1/stamps/", headers=headers)
        else:
            r = client.post("/api/v1/stamps/", headers=headers)
    assert r.status_code == 200, r.text
    settled = [e for e in _audit_events()[start:] if e["event_type"] == "payment_settled"]
    return seen, settled


def test_payload_cannot_claim_another_version(gw):  # noqa: F811
    """The version is the header the request was dispatched on, not the payload's claim.

    An X-PAYMENT is v1 (D2/D7 in #596): one whose payload says x402Version 2
    is still labelled and audited as 1.
    """
    client, fac, _, _ = gw
    fac.verify.return_value.payer = "0x" + "e5" * 20
    paid1, settled1, settled2 = _payments("paid", "1"), _settlements("settled", "1"), _settlements("settled", "2")
    seen, settled = _paid_request(client, _v2_payload_header())
    assert seen == [1]
    assert _payments("paid", "1") == paid1 + 1
    assert _settlements("settled", "1") == settled1 + 1
    assert _settlements("settled", "2") == settled2
    assert [e["data"]["x402_version"] for e in settled] == [1]


def test_version_follows_the_dispatch_not_a_constant(gw):  # noqa: F811
    """With the header-derived version made 2, the state, labels and audit say 2."""
    client, _, _, _ = gw
    paid2, settled2, settled1 = _payments("paid", "2"), _settlements("settled", "2"), _settlements("settled", "1")
    seen, settled = _paid_request(client, X_PAYMENT, return_value="2")
    assert seen == [2]
    assert _payments("paid", "2") == paid2 + 1
    assert _settlements("settled", "2") == settled2 + 1
    assert _settlements("settled", "1") == settled1
    assert [e["data"]["x402_version"] for e in settled] == [2]


def test_v1_payment_is_version_1(gw):  # noqa: F811
    client, _, _, _ = gw
    seen, settled = _paid_request(client, X_PAYMENT)
    assert seen == [1]
    assert [e["data"]["x402_version"] for e in settled] == [1]
