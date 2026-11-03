# tests/test_state_save_failure_metric.py
"""Failed saves of the pool state and the ownership registry are counted and alerted (#506).

Both saves log at ERROR and carry on, because they run after payment has
settled and raising there would turn a paid delivery into a 500. The change is
then in memory only and a restart loses it; nothing alerted on that. These tests
drive the real save paths and read the counter the alert is built on.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from prometheus_client import REGISTRY

from app.services.stamp_ownership import POOL_OWNER, StampOwnershipManager, stamp_ownership_manager
from app.services.stamp_pool import PoolStamp, StampPoolManager

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALERT_RULES = os.path.join(REPO_ROOT, "monitoring", "alerting", "alert-rules.json")

A = "a" * 64
B = "b" * 64
OWNER = "0x" + "ab" * 20


def _failures(file: str) -> float:
    return REGISTRY.get_sample_value("gateway_state_save_failures_total", {"file": file}) or 0.0


def _pool(tmp_path, *ids) -> StampPoolManager:
    mgr = StampPoolManager(state_file=str(tmp_path / "pool_state.json"))
    for b in ids:
        mgr._pool[b] = PoolStamp(batch_id=b, depth=17, amount=1,
                                 created_at=datetime.now(timezone.utc), ttl_at_creation=3600)
    return mgr


# --- pool state ---------------------------------------------------------------

def test_failed_pool_save_on_release_is_counted(tmp_path, monkeypatch):
    """The issue's case: the release after settlement cannot write the file."""
    for b in (A, B):
        monkeypatch.setitem(stamp_ownership_manager._registry, b, {"owner": POOL_OWNER, "mode": "pool"})
    mgr = _pool(tmp_path, A, B)
    assert mgr._save_state() is True
    before = _failures("pool")

    with patch("app.services.stamp_pool.atomic_write_json", side_effect=OSError("No space left on device")):
        assert mgr.release_stamp(A) is not None   # the release itself still succeeds

    assert _failures("pool") == before + 1
    # The file is behind the pool, as the alert says.
    assert json.load(open(mgr._get_state_file_path())) == sorted([A, B])


def test_successful_pool_save_is_not_counted(tmp_path):
    mgr = _pool(tmp_path, A)
    before = _failures("pool")
    assert mgr._save_state() is True
    assert _failures("pool") == before


def test_refused_pool_save_for_unreadable_file_is_not_counted_here(tmp_path):
    """A save refused during a #547 pause is the unreadable-file case, not a failed write."""
    mgr = _pool(tmp_path, A)
    with open(mgr._get_state_file_path(), "w") as f:
        f.write('["trunc')
    before = _failures("pool")
    assert mgr._save_state() is False
    assert _failures("pool") == before


# --- ownership registry ---------------------------------------------------------

def test_failed_ownership_write_is_counted(tmp_path):
    mgr = StampOwnershipManager(state_file=str(tmp_path / "stamp_owners.json"))
    before = _failures("ownership")
    with patch("app.services.stamp_ownership.atomic_write_json", side_effect=OSError("Permission denied")):
        assert mgr.register_stamp(A, owner=OWNER, mode="paid", source="pool_acquire") is True
    assert _failures("ownership") == before + 1
    assert mgr.health_warnings()   # the #562 /health warning is unchanged


def test_refused_ownership_save_after_failed_copy_is_counted(tmp_path):
    """A save refused because the pre-migration copy failed (#558) is not on disk either."""
    state_file = str(tmp_path / "stamp_owners.json")
    with open(state_file, "w") as f:
        json.dump({A.upper(): {"owner": OWNER, "mode": "paid"}}, f)
    mgr = StampOwnershipManager(state_file=state_file)
    before = _failures("ownership")
    with patch("app.services.stamp_ownership._keep_pre_migration_copy", side_effect=OSError("read-only")):
        mgr.load_on_startup()
        assert mgr._saves_refused is True
        assert _failures("ownership") == before + 1
        mgr.register_stamp(B, owner=OWNER, mode="paid", source="pool_acquire")
    assert _failures("ownership") == before + 2


def test_successful_ownership_save_is_not_counted(tmp_path):
    mgr = StampOwnershipManager(state_file=str(tmp_path / "stamp_owners.json"))
    before = _failures("ownership")
    mgr.register_stamp(A, owner=OWNER, mode="paid", source="pool_acquire")
    assert _failures("ownership") == before


# --- series exist from startup, and the alert reads them ----------------------------

def test_both_series_exist_at_zero_in_a_fresh_process():
    code = ("import app.services.metrics\n"
            "from prometheus_client import REGISTRY, generate_latest\n"
            "print(generate_latest(REGISTRY).decode())\n")
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    for file in ("pool", "ownership"):
        line = f'gateway_state_save_failures_total{{file="{file}"}} 0.0'
        assert line in lines, f"missing at startup: {line}"


def test_alert_rule_fires_on_any_failed_save():
    rules = {r["title"]: r for r in json.load(open(ALERT_RULES))}
    rule = rules["State file save failures"]
    query = next(q for q in rule["data"] if q["refId"] == "A")["model"]["expr"]
    assert "increase(gateway_state_save_failures_total[15m])" in query
    assert "file" in query   # the alert names the file
    threshold = next(q for q in rule["data"] if q["refId"] == "C")["model"]["conditions"][0]["evaluator"]
    assert threshold == {"params": [0], "type": "gt"}
    assert rule["labels"]["severity"] == "critical"
    assert "Do NOT restart" in rule["annotations"]["description"]
