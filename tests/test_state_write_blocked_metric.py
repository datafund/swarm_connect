# tests/test_state_write_blocked_metric.py
"""A failed or stalled payment-state write shows on /metrics and is alerted (#585).

While a write of the x402 settlement-unknown registry or of the idempotency
store fails or stalls, paid requests (or keyed paid requests) are refused with
503. gateway_state_file_unreadable (#584) covers only a file that could not be
read at startup, so this refusal showed on no dashboard and no alert.

Also pinned: the scrape-time reader never starts a write, and a recovered disk
clears the flags with no paid traffic and no scrapes, through the background
retry task in app/x402/state_retry.py.
"""
import asyncio
import json
import os
import subprocess
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY, generate_latest

import app.x402.idempotency as idem
import app.x402.settlement as st
from app.core.config import settings
from app.x402 import state_retry
from app.x402.idempotency import IdempotencyStore
from app.x402.settlement import BEFORE_WORK, OutcomeUnknownRegistry

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALERT_RULES = os.path.join(REPO_ROOT, "monitoring", "alerting", "alert-rules.json")
RUNBOOK = os.path.join(REPO_ROOT, "docs", "mainnet-runbook.md")
DASHBOARD = os.path.join(REPO_ROOT, "monitoring", "provisioning", "dashboards", "gateway-overview.json")
RULE = "State file writes blocked"
SERIES = [(f, r) for f in ("settlement_unknown", "idempotency") for r in ("failed", "stalled")]
KEY = ("0x" + "11" * 20, "0x" + "22" * 32)


def _gauge(file: str, reason: str) -> float:
    return REGISTRY.get_sample_value("gateway_state_write_blocked", {"file": file, "reason": reason})


def _wait_until(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class _Disk:
    """Stands in for atomic_write_json in one module: ok, failing, or held."""

    def __init__(self, module, monkeypatch):
        self.real = module.atomic_write_json
        self.failing = False
        self.gate = threading.Event()
        self.gate.set()
        self.calls = 0
        monkeypatch.setattr(module, "atomic_write_json", self.write)

    def write(self, path, data):
        self.calls += 1
        if not self.gate.wait(10):
            raise TimeoutError("test gate never released")
        if self.failing:
            raise OSError(28, "No space left on device")
        self.real(path, data)


@pytest.fixture
def x402_on(monkeypatch):
    monkeypatch.setattr(settings, "X402_ENABLED", True)
    monkeypatch.setattr(settings, "X402_SETTLEMENT_STATE_WRITE_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(idem, "WRITE_RETRY_SECONDS", 0)
    monkeypatch.setattr(st, "WRITE_RETRY_SECONDS", 0)


@pytest.fixture
def idem_store(tmp_path, monkeypatch, x402_on):
    disk = _Disk(idem, monkeypatch)
    store = IdempotencyStore(state_file=str(tmp_path / "x402_idempotency.json"))
    monkeypatch.setattr(idem, "idempotency_store", store)
    yield store, disk
    disk.gate.set()
    assert _wait_until(lambda: idem._idempotency_io.backlog() == 0)


@pytest.fixture
def registry(tmp_path, monkeypatch, x402_on):
    disk = _Disk(st, monkeypatch)
    reg = OutcomeUnknownRegistry(state_file=str(tmp_path / "x402_settlement_unknown.json"))
    monkeypatch.setattr(st, "outcome_unknown", reg)
    yield reg, disk
    disk.gate.set()
    assert _wait_until(lambda: st._registry_io_backlog() == 0)


def _all_zero(file):
    return _gauge(file, "failed") == 0 and _gauge(file, "stalled") == 0


# --- series exist from startup -----------------------------------------------

def test_every_series_exists_in_a_fresh_process():
    code = ("import app.services.metrics\n"
            "from prometheus_client import REGISTRY, generate_latest\n"
            "print(generate_latest(REGISTRY).decode())\n")
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    for file, reason in SERIES:
        line = f'gateway_state_write_blocked{{file="{file}",reason="{reason}"}} 0.0'
        assert line in lines, f"missing at startup: {line}"


# --- failed writes -----------------------------------------------------------

def test_idempotency_failed_write_reads_1_until_a_write_succeeds(idem_store):
    store, disk = idem_store
    assert _all_zero("idempotency")
    disk.failing = True
    assert store._persist() is False
    assert store.unavailable                      # keyed paid requests are refused...
    assert _gauge("idempotency", "failed") == 1   # ...and the gauge says so
    assert _gauge("idempotency", "stalled") == 0
    # Reading `unavailable` started a background retry; let it fail first.
    assert _wait_until(lambda: idem._idempotency_io.backlog() == 0)
    disk.failing = False
    assert store._persist() is True
    assert _all_zero("idempotency")


def test_settlement_failed_write_reads_1_until_a_write_succeeds(registry):
    reg, disk = registry
    assert _all_zero("settlement_unknown")
    disk.failing = True
    prior, recorded, _ = reg.begin_settlement_ex(KEY, BEFORE_WORK)
    assert recorded is False                       # the write-ahead is refused
    assert not reg.contains(KEY)                   # and taken back: nothing is settled on it
    assert reg.unavailable
    assert _gauge("settlement_unknown", "failed") == 1
    assert _wait_until(lambda: st._registry_io_backlog() == 0)
    disk.failing = False
    assert reg._persist() is True
    assert _all_zero("settlement_unknown")


# --- stalled writes ----------------------------------------------------------

def test_idempotency_stall_reads_1_until_the_write_completes(idem_store):
    store, disk = idem_store
    disk.gate.clear()
    assert asyncio.run(store.apersist(0.2)) is False    # the request-path timeout
    assert store.write_stalled()
    assert _gauge("idempotency", "stalled") == 1
    assert _gauge("idempotency", "failed") == 0
    disk.gate.set()
    assert _wait_until(lambda: _gauge("idempotency", "stalled") == 0)
    assert not store.unavailable


def test_settlement_stall_reads_1_until_the_write_completes(registry):
    reg, disk = registry
    disk.gate.clear()
    prior, recorded, gen = asyncio.run(reg.abegin_settlement(KEY, BEFORE_WORK))
    assert recorded is False and reg.write_stalled()
    assert _gauge("settlement_unknown", "stalled") == 1
    disk.gate.set()
    assert _wait_until(lambda: _gauge("settlement_unknown", "stalled") == 0)
    assert not reg.unavailable


def test_a_hung_lane_reads_stalled_before_any_request_is_refused(idem_store, registry):
    """No request has marked a stall yet, but the next one would be refused."""
    store, idisk = idem_store
    reg, sdisk = registry
    idisk.gate.clear()
    sdisk.gate.clear()
    store.schedule_persist()
    st._submit_registry_io(reg._persist)
    time.sleep(0.35)                               # past the 0.2 s write timeout
    assert not store.write_stalled() and not reg.write_stalled()
    assert _gauge("idempotency", "stalled") == 1
    assert _gauge("settlement_unknown", "stalled") == 1
    idisk.gate.set()
    sdisk.gate.set()
    assert _wait_until(lambda: _all_zero("idempotency") and _all_zero("settlement_unknown"))


def test_a_full_registry_backlog_reads_stalled_until_it_drains(registry, monkeypatch):
    """abegin_settlement refuses a request once MAX_PENDING_REGISTRY_IO writes
    are pending, however young they are; the gauge reads the same check."""
    reg, disk = registry
    monkeypatch.setattr(settings, "X402_SETTLEMENT_STATE_WRITE_TIMEOUT_SECONDS", 30)
    monkeypatch.setattr(st, "MAX_PENDING_REGISTRY_IO", 3)
    disk.gate.clear()
    for _ in range(3):
        st._submit_registry_io(reg._persist)
    assert st._registry_io_backlog() == 3
    assert st._registry_io_oldest_age() < 30          # not the age check
    assert not reg.write_stalled()                     # and not the flag
    assert _gauge("settlement_unknown", "stalled") == 1
    # The request path refuses at the same point.
    prior, recorded, _ = asyncio.run(reg.abegin_settlement(KEY, BEFORE_WORK))
    assert recorded is False
    disk.gate.set()
    assert _wait_until(lambda: st._registry_io_backlog() == 0)
    assert _wait_until(lambda: _gauge("settlement_unknown", "stalled") == 0)


# --- the reader has no side effects ------------------------------------------

def test_scraping_never_starts_a_write(idem_store, registry):
    store, idisk = idem_store
    reg, sdisk = registry
    idisk.failing = sdisk.failing = True
    store._persist()
    reg._persist()
    idisk.failing = sdisk.failing = False
    before = (idisk.calls, sdisk.calls)
    for _ in range(5):
        generate_latest(REGISTRY)
    time.sleep(0.2)
    assert (idisk.calls, sdisk.calls) == before
    assert _gauge("idempotency", "failed") == 1
    assert _gauge("settlement_unknown", "failed") == 1


def test_x402_off_reads_0(idem_store, registry, monkeypatch):
    store, idisk = idem_store
    reg, sdisk = registry
    idisk.failing = sdisk.failing = True
    store._persist()
    reg._persist()
    monkeypatch.setattr(settings, "X402_ENABLED", False)
    for file, reason in SERIES:
        assert _gauge(file, reason) == 0


def test_a_failing_flag_read_does_not_break_the_scrape(monkeypatch, x402_on):
    class Broken:
        def write_failed(self):
            raise RuntimeError("boom")

        write_stalled = write_failed

    monkeypatch.setattr(idem, "idempotency_store", Broken())
    text = generate_latest(REGISTRY).decode().splitlines()
    assert 'gateway_state_write_blocked{file="idempotency",reason="failed"} 0.0' in text
    assert 'gateway_state_write_blocked{file="idempotency",reason="stalled"} 0.0' in text


# --- background retry task ----------------------------------------------------

def test_a_recovered_disk_clears_with_no_requests_and_no_scrapes(idem_store, registry):
    store, idisk = idem_store
    reg, sdisk = registry
    idisk.failing = sdisk.failing = True
    store._persist()
    reg._persist()
    assert store.write_failed() and reg.write_failed()
    idisk.failing = sdisk.failing = False

    async def run():
        await state_retry.start_state_write_retry_task(interval=0.05)
        try:
            end = time.monotonic() + 5
            while (store.write_failed() or reg.write_failed()) and time.monotonic() < end:
                await asyncio.sleep(0.05)
        finally:
            await state_retry.stop_state_write_retry_task()

    asyncio.run(run())
    assert not store.write_failed() and not reg.write_failed()
    assert not store.unavailable and not reg.unavailable


def test_the_retry_task_does_nothing_while_healthy(idem_store, registry):
    store, idisk = idem_store
    reg, sdisk = registry
    before = (idisk.calls, sdisk.calls)

    async def run():
        await state_retry.start_state_write_retry_task(interval=0.02)
        await asyncio.sleep(0.2)
        await state_retry.stop_state_write_retry_task()

    asyncio.run(run())
    assert (idisk.calls, sdisk.calls) == before


def test_a_shutdown_while_the_retry_task_runs(idem_store, registry):
    """Stopped while a retry is held at the disk: the task ends at once, the
    reference is dropped, and nothing raises."""
    store, idisk = idem_store
    idisk.failing = True
    store._persist()
    idisk.failing = False
    idisk.gate.clear()

    async def run():
        await state_retry.start_state_write_retry_task(interval=0.02)
        task = state_retry._task
        await asyncio.sleep(0.1)                 # a retry is now held at the disk
        assert not task.done()
        await state_retry.stop_state_write_retry_task()
        return task

    task = asyncio.run(run())
    assert task.done() and task.cancelled()
    assert state_retry._task is None
    idisk.gate.set()
    assert _wait_until(lambda: not store.write_failed())


def test_a_pass_that_raises_does_not_end_the_task(monkeypatch, x402_on):
    def broken():
        raise RuntimeError("boom")

    monkeypatch.setattr(state_retry, "_stores", broken)
    assert state_retry.retry_blocked_writes() == 0      # the pass itself never raises

    async def run():
        await state_retry.start_state_write_retry_task(interval=0.01)
        task = state_retry._task
        await asyncio.sleep(0.1)                          # several failing passes
        assert not task.done()
        await state_retry.stop_state_write_retry_task()   # does not raise
        return task

    task = asyncio.run(run())
    assert task.cancelled() and state_retry._task is None


def test_stop_does_not_raise_when_the_task_failed(monkeypatch):
    """Even a task that ended with an error of its own is stopped quietly, so
    the shutdown steps after it (state-write lanes, HTTP client) still run."""
    async def run():
        async def failed():
            raise RuntimeError("boom")

        task = asyncio.ensure_future(failed())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert task.done()
        monkeypatch.setattr(state_retry, "_task", task)
        await state_retry.stop_state_write_retry_task()

    asyncio.run(run())
    assert state_retry._task is None


def test_the_lifespan_starts_and_stops_the_retry_task(monkeypatch):
    from app.main import app
    from app.x402 import facilitator
    monkeypatch.setattr(settings, "X402_ENABLED", True)
    monkeypatch.setattr(facilitator, "validate_x402_config", lambda: None)
    with TestClient(app):
        task = state_retry._task
        assert task is not None and not task.done()
    assert state_retry._task is None
    assert task.done()


# --- alert, runbook, dashboard -----------------------------------------------

def _rule():
    return {r["title"]: r for r in json.load(open(ALERT_RULES))}[RULE]


def test_alert_rule_fires_while_any_write_is_blocked():
    rule = _rule()
    query = next(q for q in rule["data"] if q["refId"] == "A")["model"]["expr"]
    assert query == "max by (environment, host, file) (gateway_state_write_blocked)"
    threshold = next(q for q in rule["data"] if q["refId"] == "C")["model"]["conditions"][0]["evaluator"]
    assert threshold == {"params": [0], "type": "gt"}
    assert rule["labels"]["severity"] == "critical"
    assert rule["ruleGroup"] == "critical"
    assert rule["for"] == "1m"
    description = rule["annotations"]["description"]
    for text in ("settlement_unknown", "idempotency", "PAYMENT_STATE_UNAVAILABLE",
                 "IDEMPOTENCY_UNAVAILABLE", "restart", "section 5",
                 "stalled flag", "past that timeout", "64 registry writes are pending"):
        assert text in description


def test_runbook_section_5_names_the_alert():
    text = open(RUNBOOK).read()
    section = text.split("## 5.")[1].split("\n## 6.")[0]
    assert RULE in section and "gateway_state_write_blocked" in section


def test_dashboard_shows_the_blocked_writes():
    exprs = [t["expr"] for p in json.load(open(DASHBOARD))["dashboard"]["panels"] for t in p.get("targets", [])]
    assert any("gateway_state_write_blocked" in e for e in exprs)


def test_apply_grafana_dry_run_lists_the_rule():
    result = subprocess.run(
        [sys.executable, os.path.join(REPO_ROOT, "scripts", "apply_grafana.py"), "--dry-run", "--only", "alerts"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert RULE in result.stdout
