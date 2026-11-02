"""Authorizations whose settlement outcome is unknown survive a restart (#553).

Before, the registry was in memory: after a restart or deploy, the recommended
same-X-PAYMENT retry could again get a 402 with `accepts` (and an x402 client
pays from that), and an after-the-work authorization could run its work again.
A "restart" here is a new registry instance loading the same state file.
"""
import json
import os
import time
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from x402.types import VerifyResponse

import app.x402.settlement as st
from tests.test_x402_integration import create_valid_payment_header
from tests.test_x402_settlement_integrity import (  # noqa: F401  (env is a fixture)
    DELIVERED, _app, _assert_outcome_unknown, _facilitator, _run, env, ok, refused,
)

KEY = ("0x1234567890abcdef1234567890abcdef12345678", "0x" + "00" * 32)


@pytest.fixture
def state(tmp_path, monkeypatch):
    path = str(tmp_path / "x402_settlement_unknown.json")
    monkeypatch.setattr(st, "outcome_unknown", st.OutcomeUnknownRegistry(state_file=path))
    return path


def _restart(monkeypatch, path):
    """A new process: a fresh registry from the file, and an empty replay guard."""
    monkeypatch.setattr(st, "outcome_unknown", st.OutcomeUnknownRegistry(state_file=path))
    st.replay_guard.reset()


def test_p2_unavailable_restart_then_refused_same_authorization_is_not_a_402(env, state, monkeypatch):
    fac = _facilitator(RuntimeError("facilitator down"), refused("nonce_used"))
    header = create_valid_payment_header()
    assert _run(fac, header=header).status_code == 502
    _restart(monkeypatch, state)
    r = _run(fac, header=header)
    _assert_outcome_unknown(r)
    assert DELIVERED == []


def test_p3_after_work_restart_does_not_rerun_the_work(env, state, monkeypatch):
    fac = _facilitator(RuntimeError("facilitator down"), ok())
    header = create_valid_payment_header()
    assert _run(fac, path="/api/v1/data/manifest", header=header).status_code == 502
    _restart(monkeypatch, state)
    _assert_outcome_unknown(_run(fac, path="/api/v1/data/manifest", header=header))
    assert DELIVERED == [1] and fac.settle.await_count == 1


def test_a_settled_retry_is_forgotten_across_a_restart(env, state, monkeypatch):
    fac = _facilitator(RuntimeError("facilitator down"), ok())
    header = create_valid_payment_header()
    assert _run(fac, header=header).status_code == 502
    assert _run(fac, header=header).status_code == 200
    _restart(monkeypatch, state)
    assert not st.outcome_unknown.contains(KEY)


def test_load_prunes_expired_entries_and_keeps_kinds(tmp_path):
    path = str(tmp_path / "s.json")
    a = st.OutcomeUnknownRegistry(state_file=path)
    a.add(("0xa", "0x1"), st.AFTER_WORK)
    a.add(("0xa", "0x2"), st.BEFORE_WORK)
    data = json.load(open(path))
    # Keyed by a hash: the file holds no raw addresses or nonces.
    assert "0xa" not in json.dumps(data) and "0x1" not in json.dumps(data)
    first = next(iter(data["entries"]))
    data["entries"][first]["expires"] = time.time() - 1
    json.dump(data, open(path, "w"))
    b = st.OutcomeUnknownRegistry(state_file=path)
    assert len([k for k in (("0xa", "0x1"), ("0xa", "0x2")) if b.contains(k)]) == 1
    assert b.kind(("0xa", "0x1")) in (None, st.AFTER_WORK)
    assert b.kind(("0xa", "0x2")) in (None, st.BEFORE_WORK)


def test_discard_is_persisted(tmp_path):
    path = str(tmp_path / "s.json")
    a = st.OutcomeUnknownRegistry(state_file=path)
    a.add(("0xa", "0x1"), st.BEFORE_WORK)
    a.discard(("0xa", "0x1"))
    assert not st.OutcomeUnknownRegistry(state_file=path).contains(("0xa", "0x1"))


def test_an_unreadable_file_is_kept_and_paid_requests_fail_closed(env, tmp_path, monkeypatch):
    path = str(tmp_path / "s.json")
    with open(path, "w") as f:
        f.write("{not json")
    reg = st.OutcomeUnknownRegistry(state_file=path)
    assert reg.unavailable
    assert any(n.startswith("s.json.corrupt-") for n in os.listdir(tmp_path))
    reg.add(("0xa", "0x1"), st.BEFORE_WORK)          # never overwrites the file
    assert open(path).read() == "{not json"
    monkeypatch.setattr(st, "outcome_unknown", reg)
    fac = _facilitator(ok())
    r = _run(fac)
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "PAYMENT_STATE_UNAVAILABLE"
    assert "accepts" not in r.json()["detail"]
    assert fac.settle.await_count == 0 and DELIVERED == []


def test_cap_evicts_before_work_entries_first(tmp_path, monkeypatch):
    reg = st.OutcomeUnknownRegistry(state_file=str(tmp_path / "s.json"))
    monkeypatch.setattr(st, "OUTCOME_UNKNOWN_MAX_ENTRIES", 2)
    reg.add(("0xa", "0x1"), st.AFTER_WORK)       # oldest, but the work ran: kept
    reg.add(("0xa", "0x2"), st.BEFORE_WORK)
    reg.add(("0xa", "0x3"), st.BEFORE_WORK)
    assert reg.contains(("0xa", "0x1"))
    assert not reg.contains(("0xa", "0x2"))
    assert reg.contains(("0xa", "0x3"))


def test_unavailable_carries_the_payment_headers(env):
    fac = _facilitator(RuntimeError("facilitator down"))
    r = _run(fac)
    assert r.status_code == 502
    assert r.json()["detail"]["code"] == "PAYMENT_SETTLEMENT_UNAVAILABLE"
    assert r.headers["X-Payment-Mode"] == "failed"
    assert r.headers["X-Payment-Status"] == "settlement_unknown"


def test_a_failed_write_fails_closed_until_a_write_succeeds(env, tmp_path, monkeypatch):
    """#553 review 4: an entry only in memory is lost at restart, so a failed save makes
    the registry unavailable (paid requests 503, unpaid) until a later save succeeds."""
    reg = st.OutcomeUnknownRegistry(state_file=str(tmp_path / "s.json"))
    monkeypatch.setattr(st, "outcome_unknown", reg)

    def broken(path, data):
        raise OSError("read-only file system")

    monkeypatch.setattr(st, "atomic_write_json", broken)
    reg.add(("0xa", "0x1"), st.BEFORE_WORK)
    assert reg.unavailable
    fac = _facilitator(ok())
    r = _run(fac)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "PAYMENT_STATE_UNAVAILABLE"
    assert fac.settle.await_count == 0
    # Free traffic is unaffected.
    free = TestClient(_app(fac)).post("/api/v1/stamps/", headers={"X-Payment-Mode": "free"})
    assert free.status_code != 503
    # The disk recovers: the next check writes the state and lets paid requests through.
    monkeypatch.undo()
    monkeypatch.setattr(st, "outcome_unknown", reg)
    assert not reg.unavailable
    assert st.OutcomeUnknownRegistry(state_file=str(tmp_path / "s.json")).contains(("0xa", "0x1"))
    assert _run(_facilitator(ok())).status_code == 200


def test_unreadable_file_message_does_not_claim_the_gateway_refuses_to_start(tmp_path):
    path = str(tmp_path / "s.json")
    with open(path, "w") as f:
        f.write("[]")
    reg = st.OutcomeUnknownRegistry(state_file=path)
    assert "Refusing to start" not in reg.unavailable
    assert "PAYMENT_STATE_UNAVAILABLE" in reg.unavailable


def _outcome_unknown_records():
    from app.x402.audit import read_audit_log
    return [e for e in read_audit_log()
            if e["event_type"] == "payment_failed" and e["data"].get("stage") == "outcome_unknown_retry"]


def test_every_outcome_unknown_409_is_audited_with_the_nonce(env):
    """#553 review 4: the verify-stage and after-work 409s left no record."""
    header = create_valid_payment_header()
    nonce = "0x" + "00" * 32
    # after the work (dependency)
    fac = _facilitator(RuntimeError("down"))
    assert _run(fac, path="/api/v1/data/manifest", header=header).status_code == 502
    assert _run(fac, path="/api/v1/data/manifest", header=header).status_code == 409
    rec = _outcome_unknown_records()[0]
    assert rec["data"]["nonce"] == nonce and "after_work" in rec["data"]["reason"]
    # refused at verify
    st.outcome_unknown.reset(); st.replay_guard.reset()
    fac = _facilitator(RuntimeError("down"))
    good = VerifyResponse(isValid=True, payer="0xa1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1")
    fac.verify = AsyncMock(side_effect=[good, VerifyResponse(isValid=False, invalidReason="nonce_used", payer=None)])
    assert _run(fac, header=header).status_code == 502
    assert _run(fac, header=header).status_code == 409
    rec = _outcome_unknown_records()[0]
    assert rec["data"]["nonce"] == nonce and "verify" in rec["data"]["reason"]
    assert "nonce_used" in rec["data"]["reason"]
    # refused at settle
    st.outcome_unknown.reset(); st.replay_guard.reset()
    fac = _facilitator(RuntimeError("down"), refused("nonce_used"))
    assert _run(fac, header=header).status_code == 502
    assert _run(fac, header=header).status_code == 409
    rec = _outcome_unknown_records()[0]
    assert rec["data"]["nonce"] == nonce and "settle" in rec["data"]["reason"]


def test_health_warns_while_payment_state_is_unavailable(tmp_path, monkeypatch):
    import asyncio
    from unittest.mock import patch
    path = str(tmp_path / "s.json")
    with open(path, "w") as f:
        f.write("{not json")
    monkeypatch.setattr(st, "outcome_unknown", st.OutcomeUnknownRegistry(state_file=path))
    from app.main import read_root
    with patch("app.main.settings") as s, \
         patch("app.x402.base_balance.check_base_eth_balance", new=AsyncMock(return_value={
             "ok": True, "is_critical": False, "balance_eth": 1.0, "threshold_eth": 0.005,
             "critical_eth": 0.001, "address": "0xT", "warning": None})), \
         patch("app.x402.preflight.check_preflight_balances", new=AsyncMock(return_value={
             "can_accept": True, "warnings": [], "errors": [], "balances": {}})), \
         patch("app.services.swarm_api.get_node_status_summary",
               new=AsyncMock(return_value={"healthy": True, "network_availability": "Available", "warnings": []})):
        s.X402_ENABLED = True
        s.PROJECT_NAME = "t"
        response = asyncio.run(read_root())
    assert response["status"] == "degraded"
    assert any("PAYMENT_STATE_UNAVAILABLE" in w for w in response["x402"]["warnings"])


def test_health_warns_while_the_idempotency_store_is_unavailable(monkeypatch):
    import asyncio
    from unittest.mock import patch
    from app.x402.idempotency import idempotency_store
    monkeypatch.setattr(idempotency_store, "unavailable", "JSONDecodeError: x")
    from app.main import read_root
    with patch("app.main.settings") as s, \
         patch("app.x402.base_balance.check_base_eth_balance", new=AsyncMock(return_value={
             "ok": True, "is_critical": False, "balance_eth": 1.0, "threshold_eth": 0.005,
             "critical_eth": 0.001, "address": "0xT", "warning": None})), \
         patch("app.x402.preflight.check_preflight_balances", new=AsyncMock(return_value={
             "can_accept": True, "warnings": [], "errors": [], "balances": {}})), \
         patch("app.services.swarm_api.get_node_status_summary",
               new=AsyncMock(return_value={"healthy": True, "network_availability": "Available", "warnings": []})):
        s.X402_ENABLED = True
        s.PROJECT_NAME = "t"
        response = asyncio.run(read_root())
    assert response["status"] == "ok"    # only keyed paid requests are refused
    assert any("IDEMPOTENCY_UNAVAILABLE" in w for w in response["x402"]["warnings"])
