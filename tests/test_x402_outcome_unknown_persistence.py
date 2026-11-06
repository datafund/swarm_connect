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
from app.x402.models_v1 import VerifyResponse

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


def _wait_until(predicate, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _restart(monkeypatch, path):
    """A new process: a fresh registry from the file, and an empty replay guard."""
    monkeypatch.setattr(st, "outcome_unknown", st.OutcomeUnknownRegistry(state_file=path))
    st.replay_guard.reset()


def test_p2_unavailable_restart_then_refused_same_authorization_is_not_a_402(env, state, monkeypatch):
    fac = _facilitator(RuntimeError("facilitator down"), refused("invalid_exact_evm_nonce_already_used"))
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
    monkeypatch.setattr(st, "WRITE_RETRY_SECONDS", 0)   # retries are rate-limited (review 7)

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
    monkeypatch.setattr(st, "WRITE_RETRY_SECONDS", 0)
    # The retry runs in the background (review 8): wait for it.
    assert _wait_until(lambda: not reg.unavailable)
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
    fac = _facilitator(RuntimeError("down"), refused("invalid_exact_evm_nonce_already_used"))
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


# --- #553 review 5 ---------------------------------------------------------

def test_cancelled_settlement_before_work_is_remembered(env, state, monkeypatch):
    """settle_payment interrupted (CancelledError: a shutdown or disconnect while waiting
    for the facilitator): the outcome is unknown, not "You were not charged"."""
    import asyncio
    fac = _facilitator(asyncio.CancelledError(), refused("invalid_exact_evm_nonce_already_used"), refused("invalid_exact_evm_nonce_already_used"))
    header = create_valid_payment_header()
    first = _run(fac, header=header)
    assert first.status_code != 402 and "You were not charged" not in first.text
    assert "accepts" not in first.text
    if first.status_code == 502:
        body = first.json()
        assert body["detail"]["code"] == "PAYMENT_SETTLEMENT_UNAVAILABLE"
        assert first.headers["X-Payment-Status"] == "settlement_unknown"
    assert st.outcome_unknown.kind(KEY) == st.BEFORE_WORK
    # Kept reserved: the same X-PAYMENT now is the in-use 409, not a 402.
    _assert_outcome_unknown(_run(fac, header=header))
    # After the replay window, refused at settle: 409, not a 402 with accepts.
    st.replay_guard.reset()
    _assert_outcome_unknown(_run(fac, header=header))
    # And after a restart.
    _restart(monkeypatch, state)
    _assert_outcome_unknown(_run(fac, header=header))
    assert DELIVERED == []


def test_cancelled_settlement_after_work_is_remembered(env, state, monkeypatch):
    import asyncio
    fac = _facilitator(asyncio.CancelledError(), ok())
    header = create_valid_payment_header()
    first = _run(fac, path="/api/v1/data/manifest", header=header)
    assert first.status_code != 402 and "accepts" not in first.text and "You were not charged" not in first.text
    assert st.outcome_unknown.kind(KEY) == st.AFTER_WORK
    _restart(monkeypatch, state)
    _assert_outcome_unknown(_run(fac, path="/api/v1/data/manifest", header=header))
    assert DELIVERED == [1] and fac.settle.await_count == 1


def test_refused_retry_on_a_fallback_route_upgrades_to_after_work(env, state):
    """A before-work authorization retried on a route that settles after its work: the
    work ran, so from then on it must not run again."""
    fac = _facilitator(RuntimeError("down"), refused("invalid_exact_evm_nonce_already_used"), refused("invalid_exact_evm_nonce_already_used"))
    header = create_valid_payment_header()
    assert _run(fac, header=header).status_code == 502
    _assert_outcome_unknown(_run(fac, path="/api/v1/data/manifest", header=header))
    assert st.outcome_unknown.kind(KEY) == st.AFTER_WORK
    st.replay_guard.reset()
    _assert_outcome_unknown(_run(fac, path="/api/v1/data/manifest", header=header))
    assert DELIVERED == [1]


def test_health_warning_does_not_expose_paths_or_exception_text(tmp_path, monkeypatch):
    import asyncio
    from unittest.mock import patch
    path = str(tmp_path / "x402_settlement_unknown.json")
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
    warning = next(w for w in response["x402"]["warnings"] if "PAYMENT_STATE_UNAVAILABLE" in w)
    assert str(tmp_path) not in warning and "corrupt" not in warning and "Expecting" not in warning
    assert "unreadable" in warning and "x402_settlement_unknown.json" in warning


def test_refusal_of_a_remembered_authorization_has_its_own_metric(env):
    from app.services.metrics import x402_settlements_total

    def count(result):
        return x402_settlements_total.labels(result=result, version="1")._value.get()

    refused_before, after_before = count("refused"), count("refused_after_unknown")
    fac = _facilitator(RuntimeError("down"), refused("invalid_exact_evm_nonce_already_used"))
    header = create_valid_payment_header()
    assert _run(fac, header=header).status_code == 502
    assert _run(fac, header=header).status_code == 409
    assert count("refused_after_unknown") == after_before + 1
    assert count("refused") == refused_before


# --- #553 review 6 ---------------------------------------------------------

def _broken_writes(monkeypatch):
    def broken(path, data):
        raise OSError("read-only file system")
    monkeypatch.setattr(st, "atomic_write_json", broken)


def test_a_registry_that_cannot_write_its_file_at_startup_fails_closed(env, tmp_path, monkeypatch):
    """Unwritable state file plus a restart: the new instance must not report available."""
    path = str(tmp_path / "s.json")
    st.OutcomeUnknownRegistry(state_file=path).add(KEY, st.BEFORE_WORK)
    _broken_writes(monkeypatch)
    reg = st.OutcomeUnknownRegistry(state_file=path)
    assert reg.unavailable
    monkeypatch.setattr(st, "outcome_unknown", reg)
    fac = _facilitator(refused("invalid_exact_evm_nonce_already_used"))
    r = _run(fac)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "PAYMENT_STATE_UNAVAILABLE"
    assert fac.settle.await_count == 0


def test_restart_with_an_unwritable_file_does_not_lose_a_memory_only_entry(env, tmp_path, monkeypatch):
    """The measured case: entry kept only in memory (write failed), then a restart."""
    path = str(tmp_path / "s.json")
    _broken_writes(monkeypatch)
    first = st.OutcomeUnknownRegistry(state_file=path)
    first.add(KEY, st.BEFORE_WORK)
    monkeypatch.setattr(st, "outcome_unknown", st.OutcomeUnknownRegistry(state_file=path))
    st.replay_guard.reset()
    r = _run(_facilitator(refused("invalid_exact_evm_nonce_already_used")))
    assert r.status_code == 503
    assert "accepts" not in r.text


def test_write_ahead_entry_exists_while_the_facilitator_is_asked(env, state, monkeypatch):
    """SIGKILL during settle: the entry is on disk before the facilitator is called, so
    after a restart the retry is answered 409, not a payable 402."""
    snapshot = {}

    async def settle(**kw):
        snapshot["file"] = open(state).read()     # what a crash right now would leave
        return refused("invalid_exact_evm_nonce_already_used")

    fac = _facilitator()
    fac.settle = AsyncMock(side_effect=settle)
    header = create_valid_payment_header()
    r = _run(fac, header=header)
    assert r.status_code == 402                   # a definite refusal, no earlier unknown
    assert not st.outcome_unknown.contains(KEY)
    with open(state, "w") as f:                   # the crash: the file as it was mid-settle
        f.write(snapshot["file"])
    _restart(monkeypatch, state)
    _assert_outcome_unknown(_run(_facilitator(refused("invalid_exact_evm_nonce_already_used")), header=header))


def test_write_ahead_on_the_fallback_is_after_work(env, state, monkeypatch):
    snapshot = {}

    async def settle(**kw):
        snapshot["file"] = open(state).read()
        return ok()

    fac = _facilitator()
    fac.settle = AsyncMock(side_effect=settle)
    header = create_valid_payment_header()
    assert _run(fac, path="/api/v1/data/manifest", header=header).status_code == 200
    assert not st.outcome_unknown.contains(KEY)
    with open(state, "w") as f:
        f.write(snapshot["file"])
    _restart(monkeypatch, state)
    assert st.outcome_unknown.kind(KEY) == st.AFTER_WORK


def test_a_normal_success_leaves_no_entry(env, state, monkeypatch):
    """#553 review 7: the resolution is written, so a graceful restart right after a
    delivered payment finds no entry (before, it found the write-ahead one)."""
    fac = _facilitator(ok())
    assert _run(fac).status_code == 200
    assert not st.outcome_unknown.contains(KEY)
    _restart(monkeypatch, state)
    assert not st.outcome_unknown.contains(KEY)


def test_a_failed_write_ahead_refuses_before_the_facilitator(env, state, monkeypatch):
    _broken_writes(monkeypatch)
    fac = _facilitator(ok())
    r = _run(fac)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "PAYMENT_STATE_UNAVAILABLE"
    assert fac.settle.await_count == 0 and DELIVERED == []


def test_a_failed_write_ahead_on_the_fallback_withholds_the_result_unpaid(env, state, monkeypatch):
    header = create_valid_payment_header()
    fac = _facilitator(ok(), ok())
    reg = st.outcome_unknown        # the same registry throughout: no fresh instance
    _broken_writes(monkeypatch)
    r = _run(fac, path="/api/v1/data/manifest", header=header)
    assert r.status_code == 503 and r.json()["code"] == "PAYMENT_STATE_UNAVAILABLE"
    assert "ok" not in r.json()
    assert fac.settle.await_count == 0
    # Nothing was charged, so once the disk recovers the same authorization can pay.
    monkeypatch.undo()
    monkeypatch.setattr(st, "outcome_unknown", reg)
    monkeypatch.setattr(st, "WRITE_RETRY_SECONDS", 0)
    assert _wait_until(lambda: not reg.unavailable)   # background retry (review 8)
    assert _run(fac, path="/api/v1/data/manifest", header=header).status_code == 200


def test_inner_cancel_on_the_fallback_is_a_structured_502(env, state):
    """An inner CancelledError (this task not being cancelled) gets the structured 502,
    the unknown outcome is recorded, and nothing says "not charged". On the Python 3.10
    image this ended in a plain-text 500 instead (#617)."""
    import asyncio
    fac = _facilitator(asyncio.CancelledError())
    r = _run(fac, path="/api/v1/data/manifest")
    assert st.outcome_unknown.kind(KEY) == st.AFTER_WORK
    assert "not charged" not in r.text and "accepts" not in r.text and '"ok"' not in r.text
    assert r.status_code == 502
    body = r.json()
    assert body["code"] == "PAYMENT_SETTLEMENT_UNKNOWN" and body["detail"]["code"] == "PAYMENT_SETTLEMENT_UNKNOWN"
    assert body["nonce"] == KEY[1]
    assert r.headers["X-Payment-Status"] == "settlement_unknown"
    assert "ok" not in body


def test_task_is_being_cancelled_tells_this_task_from_an_inner_cancel():
    """The fallback's choice between the structured 502 and re-raising (#617):
    an inner CancelledError leaves this task uncancelled; a cancellation of the
    task itself is seen even while its CancelledError is being handled."""
    import asyncio
    from app.x402.middleware import _task_is_being_cancelled

    async def inner_cancel():
        inner = asyncio.get_running_loop().create_future()
        inner.cancel()
        try:
            await inner
        except asyncio.CancelledError:
            return _task_is_being_cancelled()

    seen = []

    async def own_cancel(started):
        try:
            started.set()
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            seen.append(_task_is_being_cancelled())
            raise

    async def main():
        assert await inner_cancel() is False
        started = asyncio.Event()
        task = asyncio.create_task(own_cancel(started))
        await started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(main())
    assert seen == [True]


def test_outcome_unknown_message_says_whether_the_work_ran(env, state):
    fac = _facilitator(RuntimeError("down"), refused("invalid_exact_evm_nonce_already_used"))
    header = create_valid_payment_header()
    assert _run(fac, header=header).status_code == 502
    after = _run(fac, path="/api/v1/data/manifest", header=header)
    message = after.json()["detail"]["message"]
    assert "did nothing" not in message and "withheld" in message
    st.replay_guard.reset()
    before = _run(_facilitator(refused("insufficient_funds")), header=create_valid_payment_header(payer="0x" + "be" * 20))
    assert before.status_code == 402   # unrelated authorization: ordinary refusal
    fac = _facilitator(RuntimeError("down"), refused("invalid_exact_evm_nonce_already_used"))
    h2 = create_valid_payment_header(payer="0x" + "cf" * 20)
    assert _run(fac, header=h2).status_code == 502
    assert "did nothing" in _run(fac, header=h2).json()["detail"]["message"]


def test_middleware_built_bodies_have_detail_and_top_level_code(env, state):
    fac = _facilitator(RuntimeError("down"))
    r = _run(fac, path="/api/v1/data/manifest")
    assert r.json()["detail"]["code"] == r.json()["code"] == "PAYMENT_SETTLEMENT_UNKNOWN"
    fac = _facilitator(RuntimeError("down"), refused("invalid_exact_evm_nonce_already_used"))
    h = create_valid_payment_header(payer="0x" + "dd" * 20)
    assert _run(fac, header=h).status_code == 502
    r = _run(fac, path="/api/v1/data/manifest", header=h)
    assert r.json()["detail"]["code"] == r.json()["code"] == "PAYMENT_AUTHORIZATION_OUTCOME_UNKNOWN"
    assert r.json()["message"] == r.json()["detail"]["message"]


def test_settlement_failed_form_2_has_detail_too(env, state):
    r = _run(_facilitator(refused("insufficient_funds")), path="/api/v1/data/manifest")
    assert r.status_code == 402
    assert r.json()["code"] == r.json()["detail"]["code"] == "PAYMENT_SETTLEMENT_FAILED"


# --- #553 review 7 ---------------------------------------------------------

def test_a_refusal_then_restart_then_refusal_is_still_402(env, state, monkeypatch):
    header = create_valid_payment_header()
    assert _run(_facilitator(refused("insufficient_funds")), header=header).status_code == 402
    _restart(monkeypatch, state)
    r = _run(_facilitator(refused("insufficient_funds")), header=header)
    assert r.status_code == 402 and r.json()["detail"]["code"] == "PAYMENT_SETTLEMENT_FAILED"


def test_fallback_success_then_restart_does_not_block_the_authorization(env, state, monkeypatch):
    assert _run(_facilitator(ok()), path="/api/v1/data/manifest").status_code == 200
    _restart(monkeypatch, state)
    assert st.outcome_unknown.kind(KEY) is None


def test_a_failed_write_ahead_is_rolled_back_in_the_same_process(env, state, monkeypatch):
    """The 503 says retry with the same X-PAYMENT: once the disk recovers, that retry
    must settle normally, not meet a 409 left behind by the failed write-ahead."""
    monkeypatch.setattr(st, "WRITE_RETRY_SECONDS", 0)
    header = create_valid_payment_header()
    real = st.atomic_write_json
    _broken_writes(monkeypatch)
    assert _run(_facilitator(ok()), header=header).status_code == 503
    assert not st.outcome_unknown.contains(KEY)
    monkeypatch.setattr(st, "atomic_write_json", real)
    assert _wait_until(lambda: not st.outcome_unknown.unavailable)   # background retry
    st.replay_guard.reset()
    assert _run(_facilitator(ok()), header=header).status_code == 200


def test_a_failed_write_ahead_on_the_fallback_is_rolled_back_in_the_same_process(env, state, monkeypatch):
    monkeypatch.setattr(st, "WRITE_RETRY_SECONDS", 0)
    header = create_valid_payment_header()
    real = st.atomic_write_json
    _broken_writes(monkeypatch)
    assert _run(_facilitator(ok()), path="/api/v1/data/manifest", header=header).status_code == 503
    assert not st.outcome_unknown.contains(KEY)
    monkeypatch.setattr(st, "atomic_write_json", real)
    assert _wait_until(lambda: not st.outcome_unknown.unavailable)   # background retry
    assert _run(_facilitator(ok()), path="/api/v1/data/manifest", header=header).status_code == 200


def test_a_parallel_duplicate_during_settlement_is_told_it_is_in_progress(env, state):
    """Not "no answer came back": the first request's settlement is still running."""
    import asyncio
    import httpx2
    from unittest.mock import patch
    from app.x402.audit import read_audit_log

    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_settle(**kw):
            started.set()
            await release.wait()
            return ok()

        fac = _facilitator()
        fac.settle = AsyncMock(side_effect=slow_settle)
        header = create_valid_payment_header()
        with patch("app.x402.dependency._get_facilitator_client", return_value=fac):
            transport = httpx2.ASGITransport(app=_app(fac))
            async with httpx2.AsyncClient(transport=transport, base_url="http://t") as c:
                first = asyncio.ensure_future(c.post("/api/v1/stamps/", headers={"X-PAYMENT": header}))
                await started.wait()
                dup = await c.post("/api/v1/stamps/", headers={"X-PAYMENT": header})
                release.set()
                return await first, dup

    before = len(read_audit_log(max_entries=100000))
    first, dup = asyncio.run(run())
    new_events = read_audit_log(max_entries=100000)[:len(read_audit_log(max_entries=100000)) - before]
    assert first.status_code == 200
    assert dup.status_code == 409
    detail = dup.json()["detail"]
    assert detail["code"] == "PAYMENT_SETTLEMENT_IN_PROGRESS"
    assert "accepts" not in detail and "no answer" not in detail["message"]
    assert dup.headers["X-Payment-Status"] == "settlement_in_progress"
    stages = [e["data"].get("stage") for e in new_events if e["event_type"] == "payment_failed"]
    assert "duplicate_in_progress" in stages and "outcome_unknown_retry" not in stages
    assert not st.outcome_unknown.contains(KEY)


def test_cap_never_evicts_after_work_or_in_flight_entries(tmp_path, monkeypatch):
    """#553 review 8: an after_work entry is never evicted (its work would run again);
    when only after_work and in-flight entries remain, the write-ahead fails closed."""
    reg = st.OutcomeUnknownRegistry(state_file=str(tmp_path / "s.json"))
    monkeypatch.setattr(st, "OUTCOME_UNKNOWN_MAX_ENTRIES", 2)
    reg.add(("0xa", "0x1"), st.AFTER_WORK)
    prior, recorded = reg.begin_settlement(("0xa", "0x2"), st.BEFORE_WORK)   # in flight
    assert recorded
    prior, recorded = reg.begin_settlement(("0xa", "0x3"), st.BEFORE_WORK)
    assert not recorded and not reg.contains(("0xa", "0x3"))
    assert reg.contains(("0xa", "0x1")) and reg.contains(("0xa", "0x2"))
    # A before_work entry that is not in flight is evicted to make room.
    reg.release(("0xa", "0x2"))
    prior, recorded = reg.begin_settlement(("0xa", "0x4"), st.BEFORE_WORK)
    assert recorded and reg.contains(("0xa", "0x4")) and not reg.contains(("0xa", "0x2"))
    assert reg.contains(("0xa", "0x1"))


def test_write_retry_while_unwritable_is_rate_limited(tmp_path, monkeypatch):
    reg = st.OutcomeUnknownRegistry(state_file=str(tmp_path / "s.json"))
    calls = []

    def broken(path, data):
        calls.append(1)
        raise OSError("read-only")

    monkeypatch.setattr(st, "atomic_write_json", broken)
    reg.add(("0xa", "0x1"), st.BEFORE_WORK)
    for _ in range(50):
        assert reg.unavailable
    assert len(calls) == 1      # the add's write; no retry within WRITE_RETRY_SECONDS


def test_registry_writes_run_off_the_event_loop_thread(env, state, monkeypatch):
    import threading
    threads = {}
    real = st.atomic_write_json

    def recording(path, data):
        threads.setdefault("write", set()).add(threading.get_ident())
        real(path, data)

    async def settle(**kw):
        threads["loop"] = threading.get_ident()
        return ok()

    monkeypatch.setattr(st, "atomic_write_json", recording)
    fac = _facilitator()
    fac.settle = AsyncMock(side_effect=settle)
    assert _run(fac).status_code == 200
    assert threads["loop"] not in threads["write"]


# --- #553 review 8 ---------------------------------------------------------

def _fake_request():
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    req = MagicMock()
    req.state = SimpleNamespace(x402_mode="paid", x402_payer=KEY[0], x402_payment=object(),
                                x402_requirements=object(), x402_auth_key=KEY,
                                x402_idempotency_id=None)
    req.headers = {}
    req.client = SimpleNamespace(host="127.0.0.1")
    return req


def _slow(monkeypatch, started, seconds=0.3):
    real = st.atomic_write_json

    def slow(path, data):
        started.set()
        time.sleep(seconds)
        real(path, data)

    monkeypatch.setattr(st, "atomic_write_json", slow)


def test_in_progress_answer_has_no_retry_after_and_says_not_to_resend():
    exc = st.settlement_in_progress_refusal(KEY)
    assert "Retry-After" not in exc.headers
    message = exc.detail["message"]
    assert "Do not resend this X-PAYMENT" in message
    assert "newly signed payment" in message and "Idempotency-Key" in message
    assert "no safe way" in message
    # #553 review 9: true also when the other request was interrupted before settling.
    assert "The first request's answer is the result" not in message
    assert "interrupted" in message and "nonce" in message


def test_cancel_during_the_write_ahead_releases_and_removes_it(env, state, monkeypatch):
    """#553 review 8: a cancellation while the write-ahead is being written left the
    authorization in flight forever and a before_work entry on disk although the
    facilitator was never asked."""
    import asyncio
    import threading
    from unittest.mock import patch
    started = threading.Event()
    fac = _facilitator(ok())
    reg = st.outcome_unknown
    _slow(monkeypatch, started)

    async def go():
        with patch("app.x402.dependency._get_facilitator_client", return_value=fac):
            task = asyncio.ensure_future(st.settle_payment(_fake_request()))
            await asyncio.to_thread(started.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(go())
    assert _wait_until(lambda: not reg.in_flight(KEY) and not reg.contains(KEY))
    assert fac.settle.await_count == 0
    assert _wait_until(lambda: json.load(open(state))["entries"] == {})


def _new_events(before):
    from app.x402.audit import read_audit_log
    events = read_audit_log(max_entries=100000)
    return events[:len(events) - before]


def _audit_len():
    from app.x402.audit import read_audit_log
    return len(read_audit_log(max_entries=100000))


@pytest.mark.parametrize("outcome", ["settled", "refused"])
def test_cancel_during_the_resolution_keeps_the_records(env, state, monkeypatch, outcome):
    """#553 review 8: the audit record, record_settlement and the metric come before the
    resolution write, so a cancellation while it is written loses none of them."""
    import asyncio
    import threading
    from unittest.mock import MagicMock, patch
    from app.services.metrics import x402_settlements_total
    reg = st.outcome_unknown
    started = threading.Event()
    real_remove = reg._remove     # what aresolve runs (generation-checked since review 9)

    def slow_remove(*a, **kw):
        started.set()
        time.sleep(0.3)
        real_remove(*a, **kw)

    monkeypatch.setattr(reg, "_remove", slow_remove)
    fac = _facilitator(ok() if outcome == "settled" else refused("insufficient_funds"))
    record = MagicMock()
    metric_before = x402_settlements_total.labels(result=outcome, version="1")._value.get()
    audit_before = _audit_len()

    async def go():
        with patch("app.x402.dependency._get_facilitator_client", return_value=fac), \
             patch("app.x402.idempotency.record_settlement", record):
            task = asyncio.ensure_future(st.settle_payment(_fake_request()))
            await asyncio.to_thread(started.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(go())
    settled = [e for e in _new_events(audit_before) if e["event_type"] == "payment_settled"]
    assert settled and settled[0]["data"]["success"] is (outcome == "settled")
    assert settled[0]["data"]["nonce"] == KEY[1]
    assert x402_settlements_total.labels(result=outcome, version="1")._value.get() == metric_before + 1
    if outcome == "settled":
        record.assert_called_once()
    # The shielded resolution still finishes.
    assert _wait_until(lambda: not reg.contains(KEY))


def test_loop_side_reads_do_not_wait_for_a_slow_write(tmp_path, monkeypatch):
    import threading
    reg = st.OutcomeUnknownRegistry(state_file=str(tmp_path / "s.json"))
    started = threading.Event()
    _slow(monkeypatch, started, seconds=0.4)
    t = threading.Thread(target=reg.add, args=(("0xa", "0x1"), st.BEFORE_WORK))
    t.start()
    started.wait(2)
    t0 = time.monotonic()
    reg.kind(("0xa", "0x1")); reg.in_flight(("0xa", "0x1")); reg.contains(("0xa", "0x2"))
    reg.release(("0xa", "0x9"))
    assert time.monotonic() - t0 < 0.1
    t.join()


def test_an_older_snapshot_never_overwrites_a_newer_one(tmp_path):
    path = str(tmp_path / "s.json")
    reg = st.OutcomeUnknownRegistry(state_file=path)
    with reg._lock:
        reg._entries["old"] = {"expires": time.time() + 60, "kind": st.BEFORE_WORK}
    older = reg._snapshot()
    with reg._lock:
        reg._entries["new"] = {"expires": time.time() + 60, "kind": st.BEFORE_WORK}
    newer = reg._snapshot()
    assert reg._write(*newer)
    assert reg._write(*older)            # skipped: a newer snapshot is on disk
    assert set(json.load(open(path))["entries"]) == {"old", "new"}


def test_no_registry_write_runs_on_the_event_loop(env, state, monkeypatch):
    """Settle error, fallback error and an interrupted settlement: the write-ahead is the
    record, so none of them writes again, and nothing writes on the loop thread."""
    import asyncio
    import threading
    loop_threads, write_threads = set(), set()
    real = st.atomic_write_json

    def recording(path, data):
        write_threads.add(threading.get_ident())
        real(path, data)

    monkeypatch.setattr(st, "atomic_write_json", recording)

    def failing(exc):
        async def settle(**kw):
            loop_threads.add(threading.get_ident())
            raise exc
        f = _facilitator()
        f.settle = AsyncMock(side_effect=settle)
        return f

    assert _run(failing(RuntimeError("down"))).status_code == 502
    st.outcome_unknown.reset(); st.replay_guard.reset()
    assert _run(failing(RuntimeError("down")), path="/api/v1/data/manifest").status_code == 502
    st.outcome_unknown.reset(); st.replay_guard.reset()
    _run(failing(asyncio.CancelledError()))
    assert loop_threads and not (loop_threads & write_threads)


def test_the_unavailable_retry_writes_in_the_background(tmp_path, monkeypatch):
    import threading
    reg = st.OutcomeUnknownRegistry(state_file=str(tmp_path / "s.json"))
    monkeypatch.setattr(st, "WRITE_RETRY_SECONDS", 0)
    calls = []
    real = st.atomic_write_json

    def broken(path, data):
        raise OSError("read-only")

    monkeypatch.setattr(st, "atomic_write_json", broken)
    reg.add(("0xa", "0x1"), st.BEFORE_WORK)
    assert reg._write_error

    def recording(path, data):
        calls.append(threading.get_ident())
        real(path, data)

    monkeypatch.setattr(st, "atomic_write_json", recording)
    reg.unavailable          # schedules the retry; does not write on this thread
    assert _wait_until(lambda: not reg.unavailable)
    assert calls and threading.get_ident() not in calls


# --- #553 review 9 ---------------------------------------------------------

def test_a_stale_abandon_never_removes_a_newer_write_ahead(tmp_path):
    """Forced interleaving: a cancelled begin's background removal runs after a newer
    begin for the same authorization; it must leave the newer entry alone."""
    reg = st.OutcomeUnknownRegistry(state_file=str(tmp_path / "s.json"))
    old_prior, old_recorded, old_gen = reg.begin_settlement_ex(KEY, st.BEFORE_WORK)
    assert old_recorded
    reg.mark_asked(KEY, old_gen)
    reg.release(KEY)                                     # the cancelled caller's release
    new_prior, new_recorded, new_gen = reg.begin_settlement_ex(KEY, st.BEFORE_WORK)
    assert new_recorded and new_gen != old_gen
    reg.mark_asked(KEY, new_gen)                         # B asks the facilitator (review 10)
    reg.remove_abandoned(KEY, old_prior, old_gen)        # the stale background removal
    assert reg.contains(KEY) and reg.in_flight(KEY)
    reg.release(KEY)                                     # newer settle errors: entry stays
    reg.remove_abandoned(KEY, old_prior, old_gen)
    assert reg.contains(KEY)
    # Its own generation, not in flight, no earlier unknown outcome: removed. (Here
    # new_prior is the stale entry's kind, which is conservatively kept.)
    reg.remove_abandoned(KEY, new_prior, new_gen)
    assert reg.contains(KEY)
    reg.remove_abandoned(KEY, None, new_gen)
    assert not reg.contains(KEY)


def test_a_hung_write_ahead_fails_closed_without_asking_the_facilitator(env, state, monkeypatch):
    import asyncio
    import threading
    from unittest.mock import patch
    from fastapi import HTTPException
    from app.core.config import settings
    monkeypatch.setattr(settings, "X402_SETTLEMENT_STATE_WRITE_TIMEOUT_SECONDS", 0.2)
    started = threading.Event()
    _slow(monkeypatch, started, seconds=1.0)
    fac = _facilitator(ok())
    reg = st.outcome_unknown

    async def go():
        with patch("app.x402.dependency._get_facilitator_client", return_value=fac):
            t0 = time.monotonic()
            with pytest.raises(HTTPException) as exc:
                await st.settle_payment(_fake_request())
            return exc.value, time.monotonic() - t0

    exc, elapsed = asyncio.run(go())
    assert exc.status_code == 503 and exc.detail["code"] == "PAYMENT_STATE_UNAVAILABLE"
    assert elapsed < 0.8 and fac.settle.await_count == 0
    # The write lands later; the entry is then released and removed like a cancelled begin.
    assert _wait_until(lambda: not reg.in_flight(KEY) and not reg.contains(KEY), timeout=5)


def test_a_saturated_default_executor_does_not_delay_settlement(env, state):
    import asyncio
    import threading
    from unittest.mock import patch
    fac = _facilitator(ok())
    gate = threading.Event()

    async def go():
        loop = asyncio.get_running_loop()
        blockers = [loop.run_in_executor(None, gate.wait, 10) for _ in range(64)]
        try:
            with patch("app.x402.dependency._get_facilitator_client", return_value=fac):
                t0 = time.monotonic()
                await asyncio.wait_for(st.settle_payment(_fake_request()), 3)
                return time.monotonic() - t0
        finally:
            gate.set()
            await asyncio.gather(*blockers)

    assert asyncio.run(go()) < 2
    assert fac.settle.await_count == 1


def test_a_refusal_at_verify_counts_as_refused_after_unknown(env, state):
    from app.services.metrics import x402_settlements_total
    before = x402_settlements_total.labels(result="refused_after_unknown", version="1")._value.get()
    fac = _facilitator(RuntimeError("down"))
    good = VerifyResponse(isValid=True, payer="0xa1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1")
    fac.verify = AsyncMock(side_effect=[good, VerifyResponse(isValid=False, invalidReason="nonce_used", payer=None)])
    header = create_valid_payment_header()
    assert _run(fac, header=header).status_code == 502
    assert _run(fac, header=header).status_code == 409
    assert x402_settlements_total.labels(result="refused_after_unknown", version="1")._value.get() == before + 1


def test_shutdown_waits_for_registry_io_bounded():
    import threading
    done = threading.Event()

    def slow():
        time.sleep(0.3)
        done.set()

    st._submit_registry_io(slow)
    t0 = time.monotonic()
    st.shutdown_registry_io(timeout=0.05)
    assert time.monotonic() - t0 < 0.25          # bounded
    st._submit_registry_io(slow)                  # a new executor after shutdown
    done.clear()
    st.shutdown_registry_io(timeout=2)
    assert done.is_set()                          # waited for pending work


def test_lifespan_shuts_registry_io_down(monkeypatch):
    import inspect
    from app import main
    assert "shutdown_registry_io" in inspect.getsource(main.lifespan)


def test_a_duplicate_during_the_resolution_write_is_in_progress_not_unknown(env, state, monkeypatch):
    """Found by an intermittent failure of the concurrent-reuse test: between the
    facilitator's answer and the resolution write, a duplicate saw the entry with no
    in-flight mark and was told the outcome was unknown. The request stays in flight
    until its resolution is written."""
    import asyncio
    import threading
    import httpx2
    from unittest.mock import patch
    reg = st.outcome_unknown
    started = threading.Event()
    real_remove = reg._remove

    def slow_remove(*a, **kw):
        started.set()
        time.sleep(0.3)
        real_remove(*a, **kw)

    monkeypatch.setattr(reg, "_remove", slow_remove)
    fac = _facilitator(ok())
    header = create_valid_payment_header()

    async def run():
        with patch("app.x402.dependency._get_facilitator_client", return_value=fac):
            transport = httpx2.ASGITransport(app=_app(fac))
            async with httpx2.AsyncClient(transport=transport, base_url="http://t") as c:
                first = asyncio.ensure_future(c.post("/api/v1/stamps/", headers={"X-PAYMENT": header}))
                await asyncio.to_thread(started.wait, 2)
                dup = await c.post("/api/v1/stamps/", headers={"X-PAYMENT": header})
                return await first, dup

    first, dup = asyncio.run(run())
    assert first.status_code == 200
    assert dup.status_code == 409
    assert dup.json()["detail"]["code"] == "PAYMENT_SETTLEMENT_IN_PROGRESS"
    assert _wait_until(lambda: not reg.in_flight(KEY) and not reg.contains(KEY))


def test_state_reads_in_flight_and_kind_atomically(tmp_path):
    """The race behind an intermittent concurrent-reuse failure: a duplicate that read
    'not in flight', then 'remembered', after a write-ahead ran in between, was told
    the outcome was unknown. state() reads both under one lock."""
    reg = st.OutcomeUnknownRegistry(state_file=str(tmp_path / "s.json"))
    assert reg.state(KEY) == (False, None)
    reg.begin_settlement(KEY, st.BEFORE_WORK)
    assert reg.state(KEY) == (True, st.BEFORE_WORK)
    reg.release(KEY)
    assert reg.state(KEY) == (False, st.BEFORE_WORK)


def test_a_duplicate_reserved_out_after_a_write_ahead_ran_is_in_progress(env, state, monkeypatch):
    """Forced interleaving: the duplicate's first registry read sees nothing; the first
    request's write-ahead then runs; the duplicate's guard check fails. It must be told
    'in progress', not 'outcome unknown'."""
    from app.x402.settlement import replay_guard
    reg = st.outcome_unknown
    real_state = reg.state
    calls = []

    def state_then_begin(key):
        result = real_state(key)
        if not calls:
            calls.append(1)
            reg.begin_settlement(KEY, st.BEFORE_WORK)     # the other request, on its thread
            replay_guard.reserve(KEY)                     # it holds the reservation
        return result

    monkeypatch.setattr(reg, "state", state_then_begin)
    r = _run(_facilitator(ok()))
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "PAYMENT_SETTLEMENT_IN_PROGRESS"


# --- #553 review 10 --------------------------------------------------------

def _gated_writes(monkeypatch, gate):
    """Writes block until `gate` is set (a hung disk that later recovers)."""
    import threading
    real = st.atomic_write_json
    started = threading.Event()

    def gated(path, data):
        started.set()
        gate.wait(10)
        real(path, data)

    monkeypatch.setattr(st, "atomic_write_json", gated)
    return started


async def _settle_expecting(status, fac):
    from unittest.mock import patch
    from fastapi import HTTPException
    with patch("app.x402.dependency._get_facilitator_client", return_value=fac):
        with pytest.raises(HTTPException) as exc:
            await st.settle_payment(_fake_request())
    assert exc.value.status_code == status, exc.value.detail
    return exc.value


def test_overlapping_abandoned_write_aheads_do_not_poison_prior(env, state, monkeypatch):
    """The measured sequence: A times out (503), its retry B times out too, the disk
    recovers. Neither was ever sent to the facilitator, so nothing may stay behind,
    and a later definite refusal must be an ordinary 402."""
    import asyncio
    import threading
    from app.core.config import settings
    monkeypatch.setattr(settings, "X402_SETTLEMENT_STATE_WRITE_TIMEOUT_SECONDS", 0.2)
    reg = st.outcome_unknown
    gate = threading.Event()
    _gated_writes(monkeypatch, gate)
    fac = _facilitator(refused("insufficient_funds"))

    async def go():
        await _settle_expecting(503, fac)        # A
        assert not reg.in_flight(KEY)            # released at once: retry is not "in progress"
        await _settle_expecting(503, fac)        # B, the recommended retry
        assert reg.unavailable                   # the hang is visible
    asyncio.run(go())
    assert fac.settle.await_count == 0
    gate.set()                                   # the disk recovers; both writes land
    assert _wait_until(lambda: not reg.contains(KEY) and not reg.in_flight(KEY), timeout=5)
    assert _wait_until(lambda: not reg.unavailable, timeout=5)
    assert _wait_until(lambda: json.load(open(state))["entries"] == {}, timeout=5)
    monkeypatch.undo()
    _restart(monkeypatch, state)                 # reload from disk
    assert not st.outcome_unknown.contains(KEY)
    asyncio.run(_settle_expecting(402, _facilitator(refused("insufficient_funds"))))


def test_an_abandoned_write_ahead_is_not_an_unknown_outcome_before_its_removal(env, state, monkeypatch):
    """Plain cancellation: between the release and the queued removal, the abandoned
    write-ahead must not read as an unknown outcome, and a new attempt that the
    facilitator refuses gets an ordinary 402."""
    import asyncio
    import threading
    from unittest.mock import patch
    reg = st.outcome_unknown
    gate = threading.Event()
    started = _gated_writes(monkeypatch, gate)
    fac = _facilitator(ok())

    async def go():
        with patch("app.x402.dependency._get_facilitator_client", return_value=fac):
            task = asyncio.ensure_future(st.settle_payment(_fake_request()))
            await asyncio.to_thread(started.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        # The window: released, removal not yet run (the write is still hung).
        assert reg.state(KEY) == (False, None)
        assert not reg.contains(KEY)
    asyncio.run(go())
    gate.set()
    asyncio.run(_settle_expecting(402, _facilitator(refused("insufficient_funds"))))
    assert _wait_until(lambda: not reg.contains(KEY), timeout=5)


def test_a_begin_for_a_key_in_flight_is_refused(tmp_path):
    reg = st.OutcomeUnknownRegistry(state_file=str(tmp_path / "s.json"))
    assert reg.begin_settlement(KEY, st.BEFORE_WORK)[1]
    prior, recorded = reg.begin_settlement(KEY, st.BEFORE_WORK)
    assert not recorded and reg.in_flight(KEY)


def test_a_hung_write_shows_in_health(env, state, monkeypatch):
    import asyncio
    import threading
    from app.core.config import settings
    monkeypatch.setattr(settings, "X402_SETTLEMENT_STATE_WRITE_TIMEOUT_SECONDS", 0.2)
    gate = threading.Event()
    _gated_writes(monkeypatch, gate)
    asyncio.run(_settle_expecting(503, _facilitator(ok())))
    assert st.outcome_unknown.public_cause() == "x402_settlement_unknown.json is unresponsive"
    gate.set()
    assert _wait_until(lambda: st.outcome_unknown.public_cause() is None, timeout=5)


def test_a_hung_write_with_a_backlog_fails_closed(env, state, monkeypatch):
    """#553 review 11: refused only when the oldest pending write is older than the
    write timeout (a hung disk), not because writes are queued."""
    import asyncio
    import threading
    from app.core.config import settings
    monkeypatch.setattr(settings, "X402_SETTLEMENT_STATE_WRITE_TIMEOUT_SECONDS", 0.2)
    gate = threading.Event()
    blocker = st._submit_registry_io(gate.wait, 10)
    time.sleep(0.3)                                  # older than the timeout
    fac = _facilitator(ok())
    try:
        t0 = time.monotonic()
        exc = asyncio.run(_settle_expecting(503, fac))
        assert exc.detail["code"] == "PAYMENT_STATE_UNAVAILABLE"
        assert time.monotonic() - t0 < 0.15 and fac.settle.await_count == 0
    finally:
        gate.set()
        blocker.result(5)


def test_a_very_large_backlog_fails_closed(env, state):
    import asyncio
    import threading
    gate = threading.Event()
    blockers = [st._submit_registry_io(gate.wait, 10) for _ in range(st.MAX_PENDING_REGISTRY_IO)]
    fac = _facilitator(ok())
    try:
        asyncio.run(_settle_expecting(503, fac))
        assert fac.settle.await_count == 0
    finally:
        gate.set()
        for b in blockers:
            b.result(5)


def test_a_burst_of_write_aheads_on_a_healthy_disk_is_not_refused(env, state):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch
    reg = st.outcome_unknown

    def request(i):
        req = MagicMock()
        key = ("0x" + "%040x" % (i + 1), "0x" + "%064x" % (i + 1))
        req.state = SimpleNamespace(x402_mode="paid", x402_payer=key[0], x402_payment=object(),
                                    x402_requirements=object(), x402_auth_key=key,
                                    x402_idempotency_id=None)
        req.headers = {}
        req.client = SimpleNamespace(host="127.0.0.1")
        return req

    fac = _facilitator(*([ok()] * 20))

    async def go():
        with patch("app.x402.dependency._get_facilitator_client", return_value=fac):
            return await asyncio.gather(*[st.settle_payment(request(i)) for i in range(20)],
                                        return_exceptions=True)

    results = asyncio.run(go())
    assert [r for r in results if isinstance(r, BaseException)] == []
    assert fac.settle.await_count == 20
    assert not reg.unavailable


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "61"])
def test_the_write_timeout_setting_is_validated(value):
    from pydantic import ValidationError
    from app.core.config import Settings
    with pytest.raises(ValidationError):
        Settings(X402_SETTLEMENT_STATE_WRITE_TIMEOUT_SECONDS=value)


def test_the_write_timeout_setting_accepts_sane_values():
    from app.core.config import Settings
    assert Settings(X402_SETTLEMENT_STATE_WRITE_TIMEOUT_SECONDS="2.5").X402_SETTLEMENT_STATE_WRITE_TIMEOUT_SECONDS == 2.5


def test_a_process_with_a_hung_write_exits_within_the_bound():
    import subprocess
    import sys
    script = (
        "import threading, app.x402.settlement as st\n"
        "st._submit_registry_io(threading.Event().wait)\n"
        "st.shutdown_registry_io(timeout=0.2)\n"
    )
    t0 = time.monotonic()
    proc = subprocess.run([sys.executable, "-c", script], cwd=os.path.dirname(os.path.dirname(__file__)),
                          timeout=20, capture_output=True)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert time.monotonic() - t0 < 10


# --- #553 review 11 --------------------------------------------------------

def test_the_stall_clears_when_the_abandoned_write_had_already_landed(tmp_path, monkeypatch):
    """Measured interleaving: the write-ahead's write completes (and clears the stall)
    before wait_for times out; the timeout then marks the registry stalled. The key had
    a prior, so the abandoned removal does not write. The stall must still clear."""
    import concurrent.futures
    monkeypatch.setattr(st, "WRITE_RETRY_SECONDS", 0)
    reg = st.OutcomeUnknownRegistry(state_file=str(tmp_path / "s.json"))
    reg.add(KEY, st.BEFORE_WORK)                       # an earlier unknown outcome: a prior
    gen = reg.next_generation()
    result = reg.begin_settlement_ex(KEY, st.BEFORE_WORK, gen)    # the write lands
    assert result[1] and result[0] == st.BEFORE_WORK
    reg.mark_stalled("write-ahead did not finish in time")       # the late timeout
    done = concurrent.futures.Future()
    done.set_result(result)
    reg._give_up(KEY, gen, done)
    assert _wait_until(lambda: not reg.unavailable)
    assert reg.contains(KEY)                            # the earlier unknown outcome stays


def test_a_stall_with_no_pending_write_is_probed_in_the_background(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "WRITE_RETRY_SECONDS", 0)
    reg = st.OutcomeUnknownRegistry(state_file=str(tmp_path / "s.json"))
    reg.mark_stalled("test")
    assert reg.unavailable                              # schedules a probe write
    assert _wait_until(lambda: not reg.unavailable)


def test_a_rollback_never_undoes_a_newer_write_ahead(tmp_path, monkeypatch):
    """The write-ahead's write fails after a newer begin for the key took over (only
    possible after an abandon): the rollback must leave the newer entry alone."""
    import threading
    reg = st.OutcomeUnknownRegistry(state_file=str(tmp_path / "s.json"))
    real = st.atomic_write_json
    first_write = threading.Event()
    release = threading.Event()
    calls = []

    def write(path, data):
        calls.append(1)
        if len(calls) == 1:
            first_write.set()
            release.wait(5)
            raise OSError("disk error")
        real(path, data)

    monkeypatch.setattr(st, "atomic_write_json", write)
    old_gen = reg.next_generation()
    t = threading.Thread(target=reg.begin_settlement_ex, args=(KEY, st.BEFORE_WORK, old_gen))
    t.start()
    first_write.wait(2)
    reg.abandon(KEY, old_gen)                          # the caller gave up
    new_gen = reg.next_generation()
    with reg._lock:                                    # a newer write-ahead takes the key (asked, in flight)
        kid = st._key_id(KEY)
        reg._entries[kid] = {"expires": time.time() + 60, "kind": st.AFTER_WORK}
        reg._gens[kid] = new_gen
        reg._unasked.pop(kid, None)
        reg._inflight[kid] = 1
    release.set()
    t.join(5)
    assert reg.kind(KEY) == st.AFTER_WORK               # not rolled back
    assert reg.in_flight(KEY)                           # and its in-flight mark not released


def test_the_fallback_in_progress_answer_says_the_work_ran():
    exc = st.settlement_in_progress_refusal(KEY, work_ran=True)
    assert "not run" not in exc.detail["message"] and "withheld" in exc.detail["message"]


def test_the_outcome_unknown_message_does_not_claim_the_attempt_was_sent():
    exc = st.outcome_unknown_refusal(KEY, where="after_work")
    assert "was sent for settlement" not in exc.detail["message"]
