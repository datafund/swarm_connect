# tests/test_flood_alerts.py
"""The two warning rules for flood resilience (#588).

"Bee calls refused by the gate": refusals that keep coming, not one burst.
"Health snapshot stale": the /health snapshot is not being refreshed.
Both use the repository's usual three nodes (query, reduce last, threshold),
grouped by environment and host.
"""
import json
import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALERT_RULES = os.path.join(REPO_ROOT, "monitoring", "alerting", "alert-rules.json")

REFUSED = "Bee calls refused by the gate"
STALE = "Health snapshot stale"
EXPECTED = {
    REFUSED: {
        "expr": "sum by (environment, host) (increase(gateway_bee_call_rejections_total[5m]))",
        "threshold": 25, "for": "10m",
    },
    STALE: {
        "expr": "max by (environment, host) (gateway_health_snapshot_age_seconds)",
        "threshold": 120, "for": "5m",
    },
}


def _rules():
    with open(ALERT_RULES) as f:
        return {r["title"]: r for r in json.load(f)}


def _query(rule, ref):
    return next(q for q in rule["data"] if q["refId"] == ref)


@pytest.mark.parametrize("title", [REFUSED, STALE])
def test_rule_shape(title):
    rule = _rules()[title]
    want = EXPECTED[title]
    assert [q["refId"] for q in rule["data"]] == ["A", "B", "C"]
    assert rule["condition"] == "C"
    assert rule["labels"] == {"severity": "warning"}
    assert rule["ruleGroup"] == "warning"
    assert rule["noDataState"] == "OK"
    assert rule["execErrState"] == "Error"
    assert rule["isPaused"] is False
    assert rule["for"] == want["for"]

    a = _query(rule, "A")
    assert a["datasourceUid"] == "grafanacloud-prom"
    assert a["model"]["expr"] == want["expr"]
    assert "by (environment, host)" in a["model"]["expr"]
    b = _query(rule, "B")["model"]
    assert b["type"] == "reduce" and b["reducer"] == "last" and b["expression"] == "A"
    c = _query(rule, "C")["model"]
    assert c["type"] == "threshold" and c["expression"] == "B"
    assert c["conditions"][0]["evaluator"] == {"params": [want["threshold"]], "type": "gt"}


def test_a_single_burst_cannot_fire_the_refusal_alert():
    """The burst stays in the 5-minute window for 5 minutes, less than the hold."""
    rule = _rules()[REFUSED]
    assert "[5m]" in _query(rule, "A")["model"]["expr"]
    assert rule["for"] == "10m"


def test_stale_threshold_is_well_above_a_working_refresh():
    from app.core.config import Settings
    s = Settings(_env_file=None, SWARM_BEE_API_URL="http://localhost:1633")
    docker_healthcheck_interval = 30
    worst_working_age = (s.HEALTH_SNAPSHOT_TTL_SECONDS + docker_healthcheck_interval
                         + s.HEALTH_SNAPSHOT_REFRESH_TIMEOUT_SECONDS)
    assert EXPECTED[STALE]["threshold"] >= 1.5 * worst_working_age


@pytest.mark.parametrize("title, words", [
    (REFUSED, ["request lanes are full", "gateway_bee_calls_in_flight{lane}", "gateway_top_caller_requests",
               "BEE_MAX_CONCURRENT_CALLS", "burst", "mainnet-runbook.md section 5"]),
    (STALE, ["failing or hanging", "Bee", "gateway_health_snapshot_refreshes_total",
             "mainnet-runbook.md section 5"]),
])
def test_descriptions_say_what_to_do(title, words):
    annotations = _rules()[title]["annotations"]
    assert "{{ $labels.environment }}" in annotations["summary"]
    for w in words:
        assert w in annotations["description"], w


def test_metrics_the_rules_read_exist_from_startup():
    code = ("import app.services.metrics\n"
            "from prometheus_client import REGISTRY, generate_latest\n"
            "print(generate_latest(REGISTRY).decode())\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "gateway_bee_call_rejections_total{" in out.stdout
    assert "gateway_health_snapshot_age_seconds " in out.stdout


def test_runbook_and_dry_run_list_both():
    with open(os.path.join(REPO_ROOT, "docs", "mainnet-runbook.md")) as f:
        runbook = f.read()
    out = subprocess.run([sys.executable, "scripts/apply_grafana.py", "--dry-run", "--only", "alerts"],
                         cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    for title in (REFUSED, STALE):
        assert f"| **{title}** (warning) |" in runbook
        assert f"{title} (warning, active)" in out.stdout
