"""Idempotency store disk I/O off the event loop (stacked on #553).

Every mutating method of the store wrote its state file while holding the
store's lock, on the caller's thread: a hung or slow disk blocked the event
loop for keyed paid requests (mark_settling, mark_settled, clear_settling,
complete, resolve), and begin() then waited on that lock too.
"""
import asyncio
import json
import threading
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

import app.x402.idempotency as idem
import app.x402.settlement as st
from app.x402.idempotency import IdempotencyStore
from tests.test_x402_idempotency import _app, fac, pay  # noqa: F401  (fac is a fixture)


def _wait_until(predicate, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = IdempotencyStore(state_file=str(tmp_path / "idem.json"))
    monkeypatch.setattr(idem, "idempotency_store", s)
    monkeypatch.setattr(idem, "WRITE_RETRY_SECONDS", 0)
    return s


def _gate_writes(monkeypatch):
    gate, started = threading.Event(), threading.Event()
    real = idem.atomic_write_json

    def gated(path, data):
        started.set()
        gate.wait(10)
        real(path, data)

    monkeypatch.setattr(idem, "atomic_write_json", gated)
    return gate, started


def test_a_hung_write_does_not_block_the_event_loop_and_fails_closed(fac, store, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "X402_SETTLEMENT_STATE_WRITE_TIMEOUT_SECONDS", 0.2)
    gate, _ = _gate_writes(monkeypatch)
    app, bought = _app(fac)

    async def run():
        ticks = []

        async def heartbeat():
            for _ in range(10):
                t0 = time.monotonic()
                await asyncio.sleep(0.02)
                ticks.append(time.monotonic() - t0)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            hb = asyncio.ensure_future(heartbeat())
            r = await c.post("/api/v1/stamps/", json={"depth": 17},
                             headers={"X-PAYMENT": pay(), "Idempotency-Key": "k-hung"})
            await hb
            return r, ticks

    r, ticks = asyncio.run(run())
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "PAYMENT_STATE_UNAVAILABLE"
    assert fac.settle.await_count == 0 and not bought
    assert max(ticks) < 0.15                     # the loop kept running
    assert store.unavailable                     # keyed requests now fail closed
    gate.set()
    assert _wait_until(lambda: not store.unavailable)


def test_a_timed_out_settling_record_leaves_no_unknown_outcome(fac, store, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "X402_SETTLEMENT_STATE_WRITE_TIMEOUT_SECONDS", 0.2)
    gate, _ = _gate_writes(monkeypatch)
    app, _ = _app(fac)
    nonce = "0x" + "9a" * 32
    r = TestClient(app).post("/api/v1/stamps/", json={"depth": 17},
                             headers={"X-PAYMENT": pay(nonce=nonce), "Idempotency-Key": "k-t"})
    assert r.status_code == 503
    key = ("0x" + "a1" * 20, nonce)
    assert not st.outcome_unknown.in_flight(key)
    gate.set()
    assert _wait_until(lambda: not st.outcome_unknown.contains(key))


def test_begin_does_not_wait_for_a_write_in_progress(store, monkeypatch):
    gate, started = _gate_writes(monkeypatch)
    state, _, token = store.begin("e1", "h", "a1")
    store.mark_settling("e1", token, "a1", "n1", persist="background")
    assert started.wait(2)
    t0 = time.monotonic()
    assert store.begin("e2", "h", "a2")[0] == "new"
    store.peek("e1")
    assert time.monotonic() - t0 < 0.1
    gate.set()


def test_a_write_error_fails_closed_until_a_write_succeeds(fac, store, monkeypatch):
    real = idem.atomic_write_json

    def broken(path, data):
        raise OSError("read-only file system")

    monkeypatch.setattr(idem, "atomic_write_json", broken)
    app, _ = _app(fac)
    c = TestClient(app)
    r = c.post("/api/v1/stamps/", json={"depth": 17}, headers={"X-PAYMENT": pay(), "Idempotency-Key": "k-e"})
    assert r.status_code == 503 and fac.settle.await_count == 0
    r = c.post("/api/v1/stamps/", json={"depth": 17}, headers={"X-PAYMENT": pay(), "Idempotency-Key": "k-e2"})
    assert r.status_code == 503 and r.json()["detail"]["code"] == "IDEMPOTENCY_UNAVAILABLE"
    monkeypatch.setattr(idem, "atomic_write_json", real)
    assert _wait_until(lambda: not store.unavailable)
    r = c.post("/api/v1/stamps/", json={"depth": 17}, headers={"X-PAYMENT": pay(), "Idempotency-Key": "k-e3"})
    assert r.status_code == 201


def test_no_idempotency_write_runs_on_the_event_loop(fac, store, monkeypatch):
    loop_threads, write_threads = set(), set()
    real = idem.atomic_write_json

    def recording(path, data):
        write_threads.add(threading.get_ident())
        real(path, data)

    monkeypatch.setattr(idem, "atomic_write_json", recording)
    original_settle = fac.settle.side_effect

    async def settle(**kw):
        loop_threads.add(threading.get_ident())
        return original_settle(**kw)

    fac.settle.side_effect = settle
    app, _ = _app(fac)
    c = TestClient(app)
    first = c.post("/api/v1/stamps/", json={"depth": 17}, headers={"X-PAYMENT": pay(), "Idempotency-Key": "k-w"})
    assert first.status_code == 201
    assert _wait_until(lambda: store.peek(idem.entry_id("0x" + "a1" * 20, "POST", "/api/v1/stamps/", "k-w"))
                       and store.peek(idem.entry_id("0x" + "a1" * 20, "POST", "/api/v1/stamps/", "k-w"))["state"] == idem.DONE)
    retry = c.post("/api/v1/stamps/", json={"depth": 17}, headers={"X-PAYMENT": pay(), "Idempotency-Key": "k-w"})
    assert retry.status_code == 201 and retry.json() == first.json()
    assert write_threads and loop_threads and not (loop_threads & write_threads)


def test_an_older_snapshot_never_overwrites_a_newer_one(tmp_path):
    path = str(tmp_path / "idem.json")
    s = IdempotencyStore(state_file=path)
    with s._lock:
        s._entries["old"] = {"state": idem.DONE, "request_hash": "h", "expires": time.time() + 60}
    older = s._snapshot()
    with s._lock:
        s._entries["new"] = {"state": idem.DONE, "request_hash": "h", "expires": time.time() + 60}
    newer = s._snapshot()
    assert s._write(*newer)
    assert s._write(*older)
    assert set(json.load(open(path))["entries"]) == {"old", "new"}


def test_the_stored_state_survives_a_restart(store, tmp_path):
    state, _, token = store.begin("e1", "h", "a1")
    store.mark_settled("e1", token, "a1", "0xtx", persist="background")
    path = tmp_path / "idem.json"
    assert _wait_until(lambda: path.exists() and "e1" in json.load(open(path))["entries"])
    again = IdempotencyStore(state_file=str(tmp_path / "idem.json"))
    assert again.begin("e1", "h", "a2")[0] == idem.SETTLED


def test_health_shows_an_unresponsive_idempotency_store(store, monkeypatch):
    from unittest.mock import patch as p
    from app.main import read_root
    store.mark_stalled("test")
    with p("app.main.settings") as s, \
         p("app.x402.base_balance.check_base_eth_balance", new=AsyncMock(return_value={
             "ok": True, "is_critical": False, "balance_eth": 1.0, "threshold_eth": 0.005,
             "critical_eth": 0.001, "address": "0xT", "warning": None})), \
         p("app.x402.preflight.check_preflight_balances", new=AsyncMock(return_value={
             "can_accept": True, "warnings": [], "errors": [], "balances": {}})), \
         p("app.services.swarm_api.get_node_status_summary",
           new=AsyncMock(return_value={"healthy": True, "network_availability": "Available", "warnings": []})):
        s.X402_ENABLED = True
        s.PROJECT_NAME = "t"
        monkeypatch.setattr(idem, "WRITE_RETRY_SECONDS", 3600)    # keep the stall for the probe
        response = asyncio.run(read_root())
    warning = next(w for w in response["x402"]["warnings"] if "IDEMPOTENCY_UNAVAILABLE" in w)
    assert "unresponsive" in warning and response["status"] == "ok"
