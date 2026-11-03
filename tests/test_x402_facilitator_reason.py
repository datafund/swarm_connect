"""A facilitator's free-text error never reaches the caller (#576).

Measured on staging: a refused settlement answered 402 PAYMENT_SETTLEMENT_FAILED
with the facilitator's raw reason in the message, a multi-line RPC error naming
the RPC URL and quoting the RPC request body. The caller now sees a reason only
if it is a short machine code; the full text stays in the log and the audit
record. Driven through the real dependency, settle_payment() and the middleware
fallback, with a mocked facilitator.
"""
import logging
from unittest.mock import AsyncMock

import pytest
from x402.types import VerifyResponse

from app.x402.audit import read_audit_log
from app.x402.settlement import FACILITATOR_REASON_HIDDEN, public_facilitator_reason
from tests.test_x402_integration import create_valid_payment_header
from tests.test_x402_settlement_integrity import (  # noqa: F401  (env is a fixture)
    DELIVERED, _facilitator, _run, env, refused)

# The shape of the reason measured on staging (0.506.ef5e2c1, 2026-11-03).
RAW = ('Missing or invalid parameters.\nDouble check you have provided the correct parameters.\n\n'
       'URL: https://sepolia.base.org\n'
       'Request body: {"method":"eth_sendRawTransaction","params":["0x02f8b1"]}')
CODE = "invalid_exact_evm_nonce_already_used"
PAYER = "0xa1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1"


def _assert_hidden(r):
    text = r.text
    assert "sepolia.base.org" not in text and "https://" not in text
    assert "Request body" not in text and "eth_sendRawTransaction" not in text
    assert "Missing or invalid parameters" not in text
    # No newline, raw or JSON-escaped, anywhere in the body.
    assert "\n" not in text and "\\n" not in text
    assert FACILITATOR_REASON_HIDDEN in text


def _logged_in_full(caplog):
    return any(RAW in rec.getMessage() for rec in caplog.records)


def _audited_in_full():
    return any(isinstance(v, str) and RAW in v
               for e in read_audit_log(max_entries=1000) for v in e.get("data", {}).values())


def _verify_rejects(reason):
    return VerifyResponse(isValid=False, invalidReason=reason, payer=None)


# --- settle before the work (settle_payment) --------------------------------

def test_settle_refusal_hides_free_text_and_logs_it(env, caplog):
    caplog.set_level(logging.WARNING)
    r = _run(_facilitator(refused(RAW)))
    assert r.status_code == 402
    detail = r.json()["detail"]
    assert detail["code"] == "PAYMENT_SETTLEMENT_FAILED"
    assert detail["reason"] == FACILITATOR_REASON_HIDDEN
    assert detail["message"] == f"The payment could not be settled ({FACILITATOR_REASON_HIDDEN}). Nothing was delivered."
    assert detail["error"] == detail["message"]
    assert detail["accepts"]                           # shape unchanged: still a payable 402
    _assert_hidden(r)
    assert _logged_in_full(caplog)
    assert _audited_in_full()
    assert DELIVERED == []


def test_settle_refusal_shows_a_machine_code(env):
    r = _run(_facilitator(refused(CODE)))
    assert r.status_code == 402
    detail = r.json()["detail"]
    assert detail["reason"] == CODE
    assert detail["message"] == f"The payment could not be settled ({CODE}). Nothing was delivered."


# --- settle after the work (middleware fallback) ----------------------------

def test_fallback_refusal_hides_free_text_and_logs_it(env, caplog):
    caplog.set_level(logging.WARNING)
    r = _run(_facilitator(refused(RAW)), path="/api/v1/data/manifest")
    assert r.status_code == 402
    body = r.json()
    assert body["code"] == body["detail"]["code"] == "PAYMENT_SETTLEMENT_FAILED"
    assert body["message"] == f"The payment could not be settled ({FACILITATOR_REASON_HIDDEN})."
    assert set(body) == {"detail", "code", "message", "x402_status"}   # shape unchanged
    _assert_hidden(r)
    assert _logged_in_full(caplog)
    assert _audited_in_full()


def test_fallback_refusal_shows_a_machine_code(env):
    r = _run(_facilitator(refused(CODE)), path="/api/v1/data/manifest")
    assert r.status_code == 402
    assert r.json()["message"] == f"The payment could not be settled ({CODE})."


# --- verify -----------------------------------------------------------------

def test_verify_rejection_hides_free_text_and_logs_it(env, caplog):
    caplog.set_level(logging.WARNING)
    fac = _facilitator()
    fac.verify = AsyncMock(return_value=_verify_rejects(RAW))
    r = _run(fac)
    assert r.status_code == 402
    detail = r.json()["detail"]
    assert detail["error"] == f"Payment verification failed: {FACILITATOR_REASON_HIDDEN}"
    assert detail["accepts"]
    _assert_hidden(r)
    assert _logged_in_full(caplog)


def test_verify_rejection_shows_a_machine_code(env):
    fac = _facilitator()
    fac.verify = AsyncMock(return_value=_verify_rejects(CODE))
    r = _run(fac)
    assert r.status_code == 402
    assert r.json()["detail"]["error"] == f"Payment verification failed: {CODE}"


# --- 409 after an unknown outcome: `reason` carries the refusal -------------

def test_outcome_unknown_settle_refusal_hides_free_text(env):
    fac = _facilitator(RuntimeError("facilitator down"), refused(RAW))
    header = create_valid_payment_header()
    assert _run(fac, header=header).status_code == 502
    r = _run(fac, header=header)
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == FACILITATOR_REASON_HIDDEN
    _assert_hidden(r)
    assert _audited_in_full()


def test_outcome_unknown_verify_refusal_hides_free_text(env):
    fac = _facilitator(RuntimeError("facilitator down"))
    good = VerifyResponse(isValid=True, payer=PAYER)
    fac.verify = AsyncMock(side_effect=[good, _verify_rejects(RAW)])
    header = create_valid_payment_header()
    assert _run(fac, header=header).status_code == 502
    r = _run(fac, header=header)
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == FACILITATOR_REASON_HIDDEN
    _assert_hidden(r)
    assert _audited_in_full()


def test_outcome_unknown_refusal_shows_a_machine_code(env):
    fac = _facilitator(RuntimeError("facilitator down"), refused(CODE))
    header = create_valid_payment_header()
    assert _run(fac, header=header).status_code == 502
    assert _run(fac, header=header).json()["detail"]["reason"] == CODE


# --- the rule itself ---------------------------------------------------------

@pytest.mark.parametrize("reason", [CODE, "insufficient_funds", "unknown", "x" * 64, "nonce_used"])
def test_machine_codes_are_shown(reason):
    assert public_facilitator_reason(reason) == reason


@pytest.mark.parametrize("reason", [
    RAW, None, "", "x" * 65, "insufficient_funds\n", "Insufficient_funds", "invalid nonce",
    "rpc https://sepolia.base.org", "code:1", "invalid-nonce", 42])
def test_anything_else_is_replaced(reason):
    assert public_facilitator_reason(reason) == FACILITATOR_REASON_HIDDEN
