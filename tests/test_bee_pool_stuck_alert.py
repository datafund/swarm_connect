# tests/test_bee_pool_stuck_alert.py
"""The "Bee HTTP pool slots stuck" warning rule (#587).

Self-recovery replaces the shared HTTP client only when the pool is mostly
stuck and calls fail with PoolTimeout in a row; any response resets that
count. While only part of the pool is stuck the gateway is slower but still
answers, and this alert is what shows that phase.
"""
import json
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALERT_RULES = os.path.join(REPO_ROOT, "monitoring", "alerting", "alert-rules.json")
TITLE = "Bee HTTP pool slots stuck"


def _rule():
    with open(ALERT_RULES) as f:
        rules = {r["title"]: r for r in json.load(f)}
    return rules[TITLE]


def _query(rule, ref):
    return next(q for q in rule["data"] if q["refId"] == ref)


def test_rule_is_a_warning_with_the_usual_three_nodes():
    rule = _rule()
    assert [q["refId"] for q in rule["data"]] == ["A", "B", "C"]
    assert rule["condition"] == "C"
    assert rule["labels"] == {"severity": "warning"}
    assert rule["ruleGroup"] == "warning"
    assert rule["noDataState"] == "OK"
    assert rule["execErrState"] == "Error"
    assert rule["for"] == "10m"
    assert rule["isPaused"] is False


def test_query_is_the_stuck_share_of_the_pool_per_environment_and_host():
    a = _query(_rule(), "A")
    assert a["datasourceUid"] == "grafanacloud-prom"
    expr = a["model"]["expr"]
    assert 'gateway_bee_http_pool_connections{state="stuck"}' in expr
    assert "gateway_bee_http_pool_max_connections" in expr
    assert expr.count("by (environment, host)") == 2
    b = _query(_rule(), "B")["model"]
    assert b["type"] == "reduce" and b["reducer"] == "last" and b["expression"] == "A"


def test_threshold_is_ten_percent():
    c = _query(_rule(), "C")["model"]
    assert c["type"] == "threshold" and c["expression"] == "B"
    assert c["conditions"][0]["evaluator"] == {"params": [0.1], "type": "gte"}


def test_description_tells_the_operator_what_to_do():
    annotations = _rule()["annotations"]
    assert "{{ $labels.environment }}" in annotations["summary"]
    description = annotations["description"]
    for needed in ("restart the gateway container", "BEE_HTTP_POOL_STUCK_FRACTION",
                   "gateway_bee_http_client_replacements_total", "#587", "mainnet-runbook"):
        assert needed in description
    # A restart cuts off everything in flight, not only paid purchases.
    assert "uploads, downloads and paid stamp purchases" in description


def test_runbook_and_claude_md_name_the_alert():
    for doc in ("docs/mainnet-runbook.md", "CLAUDE.md"):
        with open(os.path.join(REPO_ROOT, doc)) as f:
            assert TITLE in f.read(), doc


def test_apply_grafana_dry_run_lists_the_rule():
    result = subprocess.run(
        [sys.executable, "scripts/apply_grafana.py", "--dry-run", "--only", "alerts"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
        env={k: v for k, v in os.environ.items() if not k.startswith("GRAFANA_")},
    )
    assert result.returncode == 0, result.stderr
    assert f"{TITLE} (warning, active)" in result.stdout


def test_max_connections_gauge_exists_at_startup_with_the_setting():
    code = (
        "import app.services.metrics\n"
        "from prometheus_client import REGISTRY, generate_latest\n"
        "print(generate_latest(REGISTRY).decode())\n"
    )
    env = dict(os.environ, BEE_HTTP_MAX_CONNECTIONS="40")
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert "gateway_bee_http_pool_max_connections 40.0" in lines
    assert 'gateway_bee_http_pool_connections{state="stuck"} 0.0' in lines
