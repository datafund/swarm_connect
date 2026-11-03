# tests/test_state_file_unreadable_metric.py
"""An unreadable state file that fails closed shows on /metrics and is alerted (#468).

Each of these stores keeps the gateway running but refuses what it guards
while its file cannot be read, until an operator restores it. Nothing alerted
on that: the spend refusal counter moves only when someone is refused, and the
pool, settlement and idempotency stores had no metric at all. The gauge reads
each store's own flag at scrape time, so it shows without any traffic.
"""
import json
import os
import subprocess
import sys

import pytest
from prometheus_client import REGISTRY, generate_latest

from app.core.config import settings

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALERT_RULES = os.path.join(REPO_ROOT, "monitoring", "alerting", "alert-rules.json")
FILES = ("pool", "pool_spend_times", "settlement_unknown", "idempotency", "spend_budget", "pool_allowance")
CORRUPT = '{"truncated'


def _gauge(file: str) -> float:
    return REGISTRY.get_sample_value("gateway_state_file_unreadable", {"file": file})


def _corrupt_file(tmp_path, name):
    path = tmp_path / name
    path.write_text(CORRUPT)
    return str(path)


def test_pool_paused_for_an_unreadable_file(tmp_path, monkeypatch):
    from app.services import stamp_pool
    from app.services.stamp_pool import StampPoolManager
    monkeypatch.setattr(settings, "STAMP_POOL_ENABLED", True)
    mgr = StampPoolManager(state_file=str(tmp_path / "pool_state.json"))
    monkeypatch.setattr(stamp_pool, "stamp_pool_manager", mgr)
    assert mgr._save_state() is True
    assert _gauge("pool") == 0

    with open(mgr._get_state_file_path(), "w") as f:
        f.write(CORRUPT)
    assert mgr._save_state() is False          # the save finds the file broken and pauses
    assert _gauge("pool") == 1
    # With the pool feature off nothing is paused, so it reads 0.
    monkeypatch.setattr(settings, "STAMP_POOL_ENABLED", False)
    assert _gauge("pool") == 0


@pytest.mark.asyncio
async def test_pool_gauge_clears_when_a_full_sync_lifts_the_pause(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock, patch
    from app.services import stamp_pool
    from app.services.stamp_pool import StampPoolManager
    monkeypatch.setattr(settings, "STAMP_POOL_ENABLED", True)
    mgr = StampPoolManager(state_file=_corrupt_file(tmp_path, "pool_state.json"))
    monkeypatch.setattr(stamp_pool, "stamp_pool_manager", mgr)
    with patch("app.services.stamp_pool.swarm_api.get_all_stamps_processed", new=AsyncMock(return_value=[])):
        await mgr.sync_from_bee_node()
        assert _gauge("pool") == 1
        with open(mgr._get_state_file_path(), "w") as f:
            json.dump([], f)                       # the operator restores the file
        await mgr.sync_from_bee_node()
    assert _gauge("pool") == 0


def test_pool_spend_times_unreadable_for_an_hour(tmp_path, monkeypatch):
    """An unreadable hourly spend record pauses pool purchases for an hour; the
    gauge shows it for that hour and then clears by itself, as the pause does."""
    from datetime import datetime, timedelta, timezone
    from app.services import stamp_pool
    from app.services.stamp_pool import StampPoolManager
    monkeypatch.setattr(settings, "STAMP_POOL_ENABLED", True)
    (tmp_path / "pool_state_spend_times.json").write_text(CORRUPT)
    mgr = StampPoolManager(state_file=str(tmp_path / "pool_state.json"))
    monkeypatch.setattr(stamp_pool, "stamp_pool_manager", mgr)
    assert mgr._spend_budget_remaining() == 0          # purchases are paused
    assert _gauge("pool_spend_times") == 1
    assert _gauge("pool") == 0                         # the pool state itself is fine

    later = datetime.now(timezone.utc) + timedelta(hours=1, seconds=1)

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return later

    monkeypatch.setattr(stamp_pool, "datetime", Later)
    assert mgr._spend_budget_remaining() > 0           # the pause has lifted
    assert _gauge("pool_spend_times") == 0

    monkeypatch.setattr(stamp_pool, "stamp_pool_manager",
                        StampPoolManager(state_file=str(tmp_path / "other" / "pool_state.json")))
    assert _gauge("pool_spend_times") == 0


def test_settlement_unknown_registry_unreadable(tmp_path, monkeypatch):
    from app.x402 import settlement
    from app.x402.settlement import OutcomeUnknownRegistry
    reg = OutcomeUnknownRegistry(state_file=_corrupt_file(tmp_path, "x402_settlement_unknown.json"))
    assert reg.unavailable                          # it fails closed
    monkeypatch.setattr(settlement, "outcome_unknown", reg)
    assert _gauge("settlement_unknown") == 1
    monkeypatch.setattr(settlement, "outcome_unknown",
                        OutcomeUnknownRegistry(state_file=str(tmp_path / "fresh.json")))
    assert _gauge("settlement_unknown") == 0


def test_idempotency_store_unreadable(tmp_path, monkeypatch):
    from app.x402 import idempotency
    from app.x402.idempotency import IdempotencyStore
    store = IdempotencyStore(state_file=_corrupt_file(tmp_path, "x402_idempotency.json"))
    assert store.unavailable
    monkeypatch.setattr(idempotency, "idempotency_store", store)
    assert _gauge("idempotency") == 1
    monkeypatch.setattr(idempotency, "idempotency_store",
                        IdempotencyStore(state_file=str(tmp_path / "fresh.json")))
    assert _gauge("idempotency") == 0


@pytest.mark.parametrize("module_name, cls_name, file", [
    ("spend_budget", "SpendBudgetTracker", "spend_budget"),
    ("pool_allowance", "PoolAllowanceTracker", "pool_allowance"),
])
def test_daily_store_unreadable_until_the_day_rolls(tmp_path, monkeypatch, module_name, cls_name, file):
    import importlib
    module = importlib.import_module(f"app.services.{module_name}")
    tracker = getattr(module, cls_name)(state_file=_corrupt_file(tmp_path, f"{file}.json"))
    assert tracker._unreadable
    singleton = "spend_budget_tracker" if module_name == "spend_budget" else "pool_allowance_tracker"
    monkeypatch.setattr(module, singleton, tracker)
    assert _gauge(file) == 1
    # The store starts afresh on the next UTC day, so the gauge must not keep
    # alerting past midnight just because no request has rolled the day yet.
    monkeypatch.setattr(module, "_today", lambda: "2999-01-01")
    assert _gauge(file) == 0


def test_a_failing_flag_read_does_not_break_the_scrape(monkeypatch):
    from app.services import spend_budget

    class Broken:
        def state_unreadable(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(spend_budget, "spend_budget_tracker", Broken())
    text = generate_latest(REGISTRY).decode()
    assert 'gateway_state_file_unreadable{file="spend_budget"} 0.0' in text.splitlines()


def test_every_series_exists_in_a_fresh_process():
    code = ("import app.services.metrics\n"
            "from prometheus_client import REGISTRY, generate_latest\n"
            "print(generate_latest(REGISTRY).decode())\n")
    # Inherits the suite's environment, so the stores read the test state dir.
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    for file in FILES:
        line = f'gateway_state_file_unreadable{{file="{file}"}} 0.0'
        assert line in lines, f"missing at startup: {line}"


def test_no_ownership_series():
    """An unreadable ownership registry stops startup ("Gateway Down"); a series
    here would always read 0."""
    assert _gauge("ownership") is None


def test_alert_rule_fires_while_any_file_is_unreadable():
    rules = {r["title"]: r for r in json.load(open(ALERT_RULES))}
    rule = rules["State file unreadable"]
    query = next(q for q in rule["data"] if q["refId"] == "A")["model"]["expr"]
    assert "gateway_state_file_unreadable" in query and "file" in query
    threshold = next(q for q in rule["data"] if q["refId"] == "C")["model"]["conditions"][0]["evaluator"]
    assert threshold == {"params": [0], "type": "gt"}
    assert rule["labels"]["severity"] == "critical"
    description = rule["annotations"]["description"]
    assert "RESTART" in description and "GATEWAY_DAILY_BZZ_CEILING=-1" in description
    for file in FILES:
        assert file in description
