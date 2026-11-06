"""A settle refusal whose reason means "the transaction may have been sent" (#660).

In @x402/evm 2.5.0 the exact EIP-3009 settle path answers `success: false`
with plain `transaction_failed` (v1 and v2) and an empty transaction from one
catch around both writeContract and waitForTransactionReceipt (its Permit2
path: `transaction_failed: <message>`). In 2.30.0, which matches what the
staging facilitator answered, a receipt wait that throws after the broadcast
answers `settlement_pending` with the transaction, and a send or simulation
failure answers `invalid_exact_evm_transaction_failed` (also used for a
reverted receipt; kept in the family anyway). `unexpected_settle_error` is in
neither version and is kept defensively. Either way the transfer can still be
mined until validBefore has passed. Such an answer is an unknown
outcome, not a refusal: at both settle sites (settle_payment before the work,
the middleware fallback after it), in both x402 versions, it must take the
path a facilitator exception takes. Definite refusals stay byte for byte as
they were (tests/golden/x402_settle_refusals.json, captured on the code before
#660; regenerated only with X402_GOLDEN_REGENERATE=1, never to make a change
pass).

The harness is tests/test_x402_v2_accept.py's: the gateway's own facilitator
client on a mocked HTTP transport, v1 and v2 enabled.
"""
import json
import os
from pathlib import Path

import pytest

from app.services.metrics import x402_settlements_total
from app.x402 import settlement as st
from tests import test_x402_v2_payment_header as v2h
from tests.test_x402_v2_accept import FALLBACK, _offer, gw  # noqa: F401  (gw is a fixture)
from tests.x402_v2_builders import PAYER, nonce_hex, other_receipt_header, pay, receipt_header

GOLDEN = Path(__file__).parent / "golden" / "x402_settle_refusals.json"
REGENERATE = os.environ.get("X402_GOLDEN_REGENERATE") == "1"

BEFORE = ("POST", "/api/v1/stamps/")    # settles in the handler, before the work
AFTER = FALLBACK                        # settled by the middleware, after the work
SITES = {"before_work": BEFORE, "after_work": AFTER}
VERSIONS = [1, 2]
RESULTS = ("settled", "refused", "error", "settled_not_delivered", "refused_after_unknown")

UNKNOWN_REASONS = [
    "transaction_failed",
    "invalid_exact_evm_transaction_failed",
    "unexpected_settle_error",
    "transaction_failed: timed out waiting for the receipt",
]


def _network(version):
    return "base-sepolia" if version == 1 else "eip155:84532"


def _answer(version, reason="__none__"):
    body = {"success": False, "transaction": "", "network": _network(version), "payer": PAYER}
    if reason != "__none__":
        body["errorReason"] = reason
    return body


def _counts(version):
    return {r: x402_settlements_total.labels(result=r, version=str(version))._value.get() for r in RESULTS}


def _moved(before, after):
    return {r: after[r] - before[r] for r in RESULTS if after[r] != before[r]}


def _send(client, route, headers):
    return client.request(route[0], route[1], headers=headers)


def _key(nonce):
    return (PAYER.lower(), nonce)


def _audit_lines():
    from app.x402.audit import get_audit_log_path
    path = get_audit_log_path()
    return path.read_text().splitlines() if path.exists() else []


def _new_audit(before_lines):
    """The audit records written since `before_lines`, without the fields that
    differ on every run (timestamp, request id)."""
    out = []
    for line in _audit_lines()[len(before_lines):]:
        if line.strip():
            event = json.loads(line)
            event.pop("timestamp", None)
            event.pop("request_id", None)
            out.append(event)
    return out


def _unknown_audit(reason):
    """The audit reason the #660 path writes; the runbook's refund procedure
    matches on it (docs/mainnet-runbook.md)."""
    return f"facilitator reported {reason} (the transaction may have been sent)"


def _assert_unknown_audited(events, reason, nonce, version):
    failed = [e for e in events if e["event_type"] == "payment_failed"]
    assert [e["data"] for e in failed] == [
        {"reason": _unknown_audit(reason), "stage": "settle", "nonce": nonce, "x402_version": version}]
    assert failed[0]["wallet_address"].lower() == PAYER.lower()
    # Not a refusal: no payment_settled record (success false) is written.
    assert [e for e in events if e["event_type"] == "payment_settled"] == []


def _unknown_body(r):
    """The JSON body without the per-request nonce, to compare two answers."""
    body = r.json()
    for d in (body, body.get("detail") if isinstance(body.get("detail"), dict) else {}):
        d.pop("nonce", None)
    return body


# --------------------------------------------------------------------------
# The classifier
# --------------------------------------------------------------------------

@pytest.mark.parametrize("reason", UNKNOWN_REASONS + [
    "TRANSACTION_FAILED", "  invalid_exact_evm_transaction_failed\n", "Unexpected_Settle_Error",
    "invalid_exact_svm_transaction_failed",
    "transaction_failed:" + "x" * 100000,
    "Transaction_Failed: multi\nline\ntext",
])
def test_classifier_family(reason):
    assert st.settle_outcome_unknown(reason) is True


@pytest.mark.parametrize("reason", [
    "insufficient_funds", "invalid_exact_evm_nonce_already_used", "invalid_transaction_state",
    "invalid_signature", "invalid_exact_evm_non_canonical_signature", "expired", "unknown",
    "", "   ", "xtransaction_failed", "transaction_failed_later", "transaction_failedx",
    "_transaction_failed", "a__transaction_failed", "transaction failed",
    "the transaction_failed because of a reason", "rpc error: transaction_failed",
    "transaction_failed;", "a" * 60 + "_transaction_failed",
    "x" * 100000, "transaction_failed" * 10000,
])
def test_classifier_definite(reason):
    assert st.settle_outcome_unknown(reason) is False


@pytest.mark.parametrize("reason", [None, 0, 1, 3.5, b"transaction_failed", bytearray(b"x"),
                                    ["transaction_failed"], {"transaction_failed": 1}, object(), True])
def test_classifier_is_total(reason):
    assert st.settle_outcome_unknown(reason) is False


# --------------------------------------------------------------------------
# settle_payment (before the work)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("version,status", [(1, 200), (2, 200), (2, 400)])
@pytest.mark.parametrize("reason", UNKNOWN_REASONS)
def test_before_work_unknown_reason_is_an_unknown_outcome(gw, version, status, reason):
    client, fac = gw("1,2")
    offer = _offer(client)
    # What a facilitator exception gets here today: the reference answer.
    fac.settle_status, fac.settle = 500, {"oops": True}
    reference = _send(client, BEFORE, pay(version, offer, nonce=nonce_hex("0e")))
    assert reference.status_code == 502, reference.text

    fac.settle_status, fac.settle = status, _answer(version, reason)
    before, audit = _counts(version), _audit_lines()
    r = _send(client, BEFORE, pay(version, offer, nonce=nonce_hex("01")))
    assert r.status_code == 502, r.text
    _assert_unknown_audited(_new_audit(audit), reason, nonce_hex("01"), version)
    assert r.json()["detail"]["code"] == "PAYMENT_SETTLEMENT_UNAVAILABLE"
    assert r.json()["detail"]["nonce"] == nonce_hex("01")
    assert r.headers["X-Payment-Mode"] == "failed"
    assert r.headers["X-Payment-Status"] == "settlement_unknown"
    assert "reason" not in r.json()["detail"] and "accepts" not in r.text
    assert reason not in r.text                      # no facilitator text reaches the caller
    assert _unknown_body(r) == _unknown_body(reference)
    assert sorted(k for k in r.headers if k != "content-length") == \
        sorted(k for k in reference.headers if k != "content-length")
    assert _moved(before, _counts(version)) == {"error": 1}
    assert st.outcome_unknown.kind(_key(nonce_hex("01"))) == st.BEFORE_WORK
    assert v2h.RAN == []


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("retry_refused_at", ["verify", "settle"])
def test_before_work_retry_of_the_same_authorization_gets_409(gw, version, retry_refused_at):
    client, fac = gw("1,2")
    offer = _offer(client)
    fac.settle = _answer(version, "invalid_exact_evm_transaction_failed")
    headers = pay(version, offer, nonce=nonce_hex("02"))
    assert _send(client, BEFORE, headers).status_code == 502
    if retry_refused_at == "verify":
        fac.verify = {"isValid": False, "invalidReason": "invalid_exact_evm_nonce_already_used",
                      "payer": PAYER}
    else:
        fac.settle = _answer(version, "invalid_exact_evm_nonce_already_used")
    before = _counts(version)
    r = _send(client, BEFORE, headers)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "PAYMENT_AUTHORIZATION_OUTCOME_UNKNOWN"
    assert r.headers["X-Payment-Status"] == "settlement_unknown"
    assert "accepts" not in r.text
    assert _moved(before, _counts(version)) == {"refused_after_unknown": 1}
    assert v2h.RAN == []


@pytest.mark.parametrize("version", VERSIONS)
def test_before_work_retry_that_settles_is_delivered_once(gw, version):
    """The documented recovery still works: the same authorization, settled."""
    client, fac = gw("1,2")
    offer = _offer(client)
    fac.settle = _answer(version, "transaction_failed")
    headers = pay(version, offer, nonce=nonce_hex("03"))
    assert _send(client, BEFORE, headers).status_code == 502
    fac.settle = None
    assert _send(client, BEFORE, headers).status_code == 200
    assert v2h.RAN == ["paid"]
    assert st.outcome_unknown.kind(_key(nonce_hex("03"))) is None


# --------------------------------------------------------------------------
# The middleware fallback (after the work)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("version,status", [(1, 200), (2, 200), (2, 400)])
@pytest.mark.parametrize("reason", UNKNOWN_REASONS)
def test_after_work_unknown_reason_is_an_unknown_outcome(gw, version, status, reason):
    client, fac = gw("1,2")
    offer = _offer(client)
    fac.settle_status, fac.settle = 500, {"oops": True}
    reference = _send(client, AFTER, pay(version, offer, nonce=nonce_hex("0e")))
    assert reference.status_code == 502, reference.text

    fac.settle_status, fac.settle = status, _answer(version, reason)
    before, audit = _counts(version), _audit_lines()
    r = _send(client, AFTER, pay(version, offer, nonce=nonce_hex("04")))
    assert r.status_code == 502, r.text
    _assert_unknown_audited(_new_audit(audit), reason, nonce_hex("04"), version)
    body = r.json()
    assert body["code"] == body["detail"]["code"] == "PAYMENT_SETTLEMENT_UNKNOWN"
    assert body["nonce"] == nonce_hex("04")
    assert r.headers["X-Payment-Mode"] == "failed"
    assert r.headers["X-Payment-Status"] == "settlement_unknown"
    assert receipt_header(version) not in r.headers and other_receipt_header(version) not in r.headers
    assert "reference" not in body                   # the unpaid result is withheld
    assert reason not in r.text
    assert _unknown_body(r) == _unknown_body(reference)
    assert sorted(k for k in r.headers if k != "content-length") == \
        sorted(k for k in reference.headers if k != "content-length")
    assert _moved(before, _counts(version)) == {"error": 1}
    assert st.outcome_unknown.kind(_key(nonce_hex("04"))) == st.AFTER_WORK


@pytest.mark.parametrize("version", VERSIONS)
def test_after_work_retry_is_stopped_before_the_work_runs_again(gw, version):
    client, fac = gw("1,2")
    offer = _offer(client)
    fac.settle = _answer(version, "invalid_exact_evm_transaction_failed")
    headers = pay(version, offer, nonce=nonce_hex("05"))
    assert _send(client, AFTER, headers).status_code == 502
    ran = list(v2h.RAN)
    st.replay_guard.reset()          # also once the reservation has lapsed
    fac.settle = None
    r = _send(client, AFTER, headers)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "PAYMENT_AUTHORIZATION_OUTCOME_UNKNOWN"
    assert r.headers["X-Payment-Status"] == "settlement_unknown"
    assert v2h.RAN == ran
    assert [c["path"] for c in fac.calls].count("settle") == 1


# --------------------------------------------------------------------------
# settlement_pending (@x402/evm 2.30.0): broadcast, receipt not awaited
# --------------------------------------------------------------------------

@pytest.mark.parametrize("reason", ["settlement_pending", "SETTLEMENT_PENDING ",
                                    "settlement_pending: request timed out"])
def test_classifier_settlement_pending(reason):
    assert st.settle_outcome_unknown(reason) is True


@pytest.mark.parametrize("reason", ["settlement_pendingx", "xsettlement_pending", "pending",
                                    "a_settlement_pending", "settlement_pending_later"])
def test_classifier_settlement_pending_near_misses(reason):
    assert st.settle_outcome_unknown(reason) is False


def _pending_answer(version, with_tx):
    """What @x402/evm 2.30.0 answers when waitForTransactionReceipt throws
    after writeContract broadcast (settlementPendingResponse)."""
    body = _answer(version, "settlement_pending")
    body["errorMessage"] = "timed out waiting for the receipt"
    if with_tx:
        body["transaction"] = "0x" + "cd" * 32
    return body


@pytest.mark.parametrize("site", sorted(SITES))
@pytest.mark.parametrize("version,status", [(1, 200), (2, 200), (2, 400)])
@pytest.mark.parametrize("with_tx", [True, False])
def test_settlement_pending_is_an_unknown_outcome(gw, site, version, status, with_tx):
    client, fac = gw("1,2")
    offer = _offer(client)
    route = SITES[site]
    fac.settle_status, fac.settle = 500, {"oops": True}
    reference = _send(client, route, pay(version, offer, nonce=nonce_hex("0e")))
    assert reference.status_code == 502, reference.text

    fac.settle_status, fac.settle = status, _pending_answer(version, with_tx)
    key = {"Idempotency-Key": f"k-pending-{site}-{version}-{status}-{with_tx}"}
    before, audit = _counts(version), _audit_lines()
    r = _send(client, route, {**pay(version, offer, nonce=nonce_hex("0a")), **key})
    assert r.status_code == 502, r.text
    assert r.headers["X-Payment-Mode"] == "failed"
    assert r.headers["X-Payment-Status"] == "settlement_unknown"
    assert receipt_header(version) not in r.headers and other_receipt_header(version) not in r.headers
    assert "settlement_pending" not in r.text and "cd" * 32 not in r.text
    assert _unknown_body(r) == _unknown_body(reference)
    assert sorted(k for k in r.headers if k != "content-length") == \
        sorted(k for k in reference.headers if k != "content-length")
    assert _moved(before, _counts(version)) == {"error": 1}
    _assert_unknown_audited(_new_audit(audit), "settlement_pending", nonce_hex("0a"), version)
    expected_kind = st.BEFORE_WORK if site == "before_work" else st.AFTER_WORK
    assert st.outcome_unknown.kind(_key(nonce_hex("0a"))) == expected_kind
    # The Idempotency-Key stays taken: a new payment under it is not settled.
    fac.settle_status, fac.settle = 200, None
    settles = [c["path"] for c in fac.calls].count("settle")
    again = _send(client, route, {**pay(version, offer, nonce=nonce_hex("0b")), **key})
    assert again.status_code == 409, again.text
    assert again.json()["detail"]["code"] == "IDEMPOTENCY_KEY_SETTLEMENT_UNKNOWN"
    assert [c["path"] for c in fac.calls].count("settle") == settles


# --------------------------------------------------------------------------
# An already-unknown authorization answered transaction_failed again
# --------------------------------------------------------------------------

@pytest.mark.parametrize("version", VERSIONS)
def test_retry_answered_transaction_failed_again_is_unknown_again(gw, version):
    """The retry's own transfer may have been sent too: 502 again, the entry
    stays, never a 409 that treats it as a refusal."""
    client, fac = gw("1,2")
    offer = _offer(client)
    fac.settle = _answer(version, "invalid_exact_evm_transaction_failed")
    headers = pay(version, offer, nonce=nonce_hex("08"))
    assert _send(client, BEFORE, headers).status_code == 502
    before, audit = _counts(version), _audit_lines()
    r = _send(client, BEFORE, headers)
    assert r.status_code == 502, r.text
    assert r.json()["detail"]["code"] == "PAYMENT_SETTLEMENT_UNAVAILABLE"
    assert r.headers["X-Payment-Status"] == "settlement_unknown"
    assert _moved(before, _counts(version)) == {"error": 1}
    _assert_unknown_audited(_new_audit(audit), "invalid_exact_evm_transaction_failed",
                            nonce_hex("08"), version)
    assert st.outcome_unknown.kind(_key(nonce_hex("08"))) == st.BEFORE_WORK
    assert v2h.RAN == []


@pytest.mark.parametrize("version", VERSIONS)
def test_retry_on_a_fallback_route_answered_transaction_failed_is_unknown_after_the_work(gw, version):
    """A before-the-work unknown outcome, then the same authorization on a route
    the middleware settles: answered transaction_failed again, it is the
    fallback's unknown outcome, and the entry becomes after_work."""
    client, fac = gw("1,2")
    offer = _offer(client)
    fac.settle = _answer(version, "transaction_failed")
    headers = pay(version, offer, nonce=nonce_hex("09"))
    assert _send(client, BEFORE, headers).status_code == 502
    before = _counts(version)
    r = _send(client, AFTER, headers)
    assert r.status_code == 502, r.text
    assert r.json()["code"] == "PAYMENT_SETTLEMENT_UNKNOWN"
    assert _moved(before, _counts(version)) == {"error": 1}
    assert st.outcome_unknown.kind(_key(nonce_hex("09"))) == st.AFTER_WORK


# --------------------------------------------------------------------------
# Idempotency-Key: an unknown outcome is not a definite failure
# --------------------------------------------------------------------------

@pytest.mark.parametrize("site", sorted(SITES))
@pytest.mark.parametrize("version", VERSIONS)
def test_idempotency_key_stays_taken_after_an_unknown_reason(gw, site, version):
    client, fac = gw("1,2")
    offer = _offer(client)
    route = SITES[site]
    key = {"Idempotency-Key": f"k-660-{site}-{version}"}
    fac.settle = _answer(version, "transaction_failed")
    assert _send(client, route, {**pay(version, offer, nonce=nonce_hex("06")), **key}).status_code == 502
    fac.settle = None
    settles = [c["path"] for c in fac.calls].count("settle")
    r = _send(client, route, {**pay(version, offer, nonce=nonce_hex("07")), **key})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "IDEMPOTENCY_KEY_SETTLEMENT_UNKNOWN"
    assert [c["path"] for c in fac.calls].count("settle") == settles     # not charged again


# --------------------------------------------------------------------------
# Definite refusals: byte for byte as before #660
# --------------------------------------------------------------------------

DEFINITE = {
    "insufficient_funds": "insufficient_funds",
    "nonce_already_used": "invalid_exact_evm_nonce_already_used",
    "invalid_transaction_state": "invalid_transaction_state",
    "invalid_signature": "invalid_signature",
    "free_text": "execution reverted\nwith a second line",
    "unverified_code": "settle_failed",
    "no_reason": "__none__",
}


def _record(client, r, before, audit, version, nonce):
    """Everything a refusal leaves behind: the answer, the metric, whether the
    handler ran, the audit records, and the outcome-unknown registry entry."""
    return {
        "status": r.status_code,
        "body": r.text,
        "headers": sorted([k, v] for k, v in r.headers.items()),
        "metric": _moved(before, _counts(version)),
        "ran": list(v2h.RAN),
        "audit": _new_audit(audit),
        "registry": st.outcome_unknown.kind(_key(nonce)),
    }


def _capture(gw):
    out = {"definite": {}, "refused_after_unknown": {}}
    n = 0x10

    def nonce():
        nonlocal n
        n += 1
        return nonce_hex(f"{n - 1:02x}")

    for version in VERSIONS:
        for site, route in sorted(SITES.items()):
            for name, reason in sorted(DEFINITE.items()):
                for status in ([200] if version == 1 else [200, 400]):
                    client, fac = gw("1,2")
                    offer = _offer(client)
                    fac.settle_status, fac.settle = status, _answer(version, reason)
                    key = {"Idempotency-Key": f"golden-{version}-{site}-{name}-{status}"}
                    first = nonce()
                    before, audit = _counts(version), _audit_lines()
                    r = _send(client, route, {**pay(version, offer, nonce=first), **key})
                    rec = _record(client, r, before, audit, version, first)
                    # The Idempotency-Key afterwards: a new payment under the
                    # same key, which the facilitator now settles.
                    fac.settle_status, fac.settle = 200, None
                    retry = _send(client, route, {**pay(version, offer, nonce=nonce()), **key})
                    rec["idempotency_retry_status"] = retry.status_code
                    out["definite"][f"v{version}/{site}/{name}/{status}"] = rec
            # A definite refusal of an authorization whose earlier outcome is
            # unknown (first a facilitator exception before the work).
            for name in ("nonce_already_used", "invalid_transaction_state"):
                client, fac = gw("1,2")
                offer = _offer(client)
                first = nonce()
                headers = pay(version, offer, nonce=first)
                fac.settle_status, fac.settle = 500, {"oops": True}
                assert _send(client, BEFORE, headers).status_code == 502
                fac.settle_status, fac.settle = 200, _answer(version, DEFINITE[name])
                before, audit = _counts(version), _audit_lines()
                r = _send(client, route, headers)
                out["refused_after_unknown"][f"v{version}/{site}/{name}"] = _record(
                    client, r, before, audit, version, first)
    return out


def test_definite_refusals_match_the_golden(gw):
    """Captured on the code before #660 (app/ of origin/dev), never edited by hand."""
    got = _capture(gw)
    if REGENERATE:
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
    expected = json.loads(GOLDEN.read_text())
    assert sorted(got) == sorted(expected)
    for section in expected:
        assert sorted(got[section]) == sorted(expected[section]), section
        for key in expected[section]:
            assert got[section][key] == expected[section][key], f"{section} {key} differs from the golden"
    # Sanity: the golden really holds what it claims to.
    for rec in expected["definite"].values():
        assert rec["metric"] == {"refused": 1}
        assert rec["registry"] is None                     # no outcome-unknown entry left
        assert rec["idempotency_retry_status"] == 200      # the key was freed
        settled = [e for e in rec["audit"] if e["event_type"] == "payment_settled"]
        assert len(settled) == 1 and settled[0]["data"]["success"] is False
    for rec in expected["refused_after_unknown"].values():
        assert rec["metric"] == {"refused_after_unknown": 1}
        assert rec["status"] == 409 and rec["registry"] is not None
