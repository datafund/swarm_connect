# tests/test_bee_failure_alerts.py
"""Alerts for a gateway that is up while its Bee calls fail (#586).

On 2026-11-03 staging kept answering /metrics for over 30 minutes while every
Bee call failed, /health took more than 10 s and the proxy answered 502. The
only gateway liveness rule reads `up`, which stayed 1. These tests pin the
rules, the Alloy probe of the public /health, and the probe's body check
against the real /health handler.
"""
import json
import os
import re
import subprocess
import sys
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALERT_RULES = os.path.join(REPO_ROOT, "monitoring", "alerting", "alert-rules.json")
ALLOY = os.path.join(REPO_ROOT, "monitoring", "alloy", "config.alloy")
COMPOSE = os.path.join(REPO_ROOT, "docker-compose.yml")
PROBE_JOB = 'job="swarm_connect/health_probe"'

NEW_RULES = {
    "Bee calls failing",
    "Gateway 5xx responses",
    "Public health check failing",
    "Public health check slow",
    "Bee node not healthy (public /health)",
    "Balance check failing",
}


def _rules():
    with open(ALERT_RULES) as f:
        return {r["title"]: r for r in json.load(f)}


def _expr(rule):
    return rule["data"][0]["model"]["expr"]


def _threshold(rule):
    cond = rule["data"][2]["model"]["conditions"][0]["evaluator"]
    return cond["type"], cond["params"][0]


def _alloy():
    with open(ALLOY) as f:
        return f.read()


# --- rules ----------------------------------------------------------------------

def test_all_new_rules_exist_unpaused_and_silent_on_no_data():
    rules = _rules()
    for title in NEW_RULES:
        rule = rules[title]
        assert rule["isPaused"] is False, title
        # Absence is the No Data watchdog's job; these must not double-report it.
        assert rule["noDataState"] == "OK", title
        assert rule["condition"] == "C", title
        assert rule["labels"]["severity"] == rule["ruleGroup"], title


def test_every_new_rule_groups_by_environment_and_host():
    rules = _rules()
    for title in NEW_RULES:
        assert "by (environment, host" in _expr(rules[title]), title


def test_bee_call_error_rule():
    rule = _rules()["Bee calls failing"]
    assert "increase(gateway_bee_api_errors_total[5m])" in _expr(rule)
    assert _threshold(rule) == ("gt", 20)
    assert rule["for"] == "5m"
    assert rule["labels"]["severity"] == "critical"


def test_5xx_rule_is_a_share_with_a_minimum_traffic_guard():
    rule = _rules()["Gateway 5xx responses"]
    expr = _expr(rule)
    assert 'http_requests_total{status=~"5.."}' in expr
    assert "/ sum by (environment, host) (increase(http_requests_total[10m]))" in expr
    # One 502 at low traffic must not page.
    assert "and on (environment, host)" in expr
    assert ">= 20" in expr
    assert _threshold(rule) == ("gt", 0.2)
    assert rule["for"] == "5m"


def test_probe_rules_select_only_this_probe():
    """The stack holds Synthetic Monitoring probe_* series for other sites."""
    rules = _rules()
    for title in ("Public health check failing", "Public health check slow",
                  "Bee node not healthy (public /health)"):
        expr = _expr(rules[title])
        assert "probe_" in expr, title
        for selector in re.findall(r"probe_\w+(\{[^}]*\})?", expr):
            assert PROBE_JOB in selector, f"{title}: unfiltered probe series in {expr}"


def test_probe_failing_rule_needs_five_minutes_without_a_200():
    rule = _rules()["Public health check failing"]
    expr = _expr(rule)
    assert "probe_http_status_code" in expr and "== bool 200" in expr and "[5m:1m]" in expr
    assert _threshold(rule) == ("gt", 0)
    assert rule["labels"]["severity"] == "critical"


def test_probe_slow_rule_threshold():
    rule = _rules()["Public health check slow"]
    assert "avg_over_time(probe_duration_seconds" in _expr(rule)
    # Production measured about 3.5 s on 2026-11-03 (uncached /health, #588).
    assert _threshold(rule) == ("gt", 5)
    assert rule["labels"]["severity"] == "warning"


def test_bee_not_healthy_rule_reads_the_body_check():
    rule = _rules()["Bee node not healthy (public /health)"]
    assert "min_over_time(probe_failed_due_to_regex" in _expr(rule)
    assert _threshold(rule) == ("gt", 0)


def test_balance_check_rule_covers_failed_and_stale_reads():
    rule = _rules()["Balance check failing"]
    expr = _expr(rule)
    assert "gateway_balance_check_ok == bool 0" in expr
    assert "gateway_balance_last_success_timestamp_seconds" in expr
    assert "by (environment, host, check)" in expr
    assert rule["for"] == "15m"


def test_balance_alerts_point_at_the_check():
    rules = _rules()
    for title in ("Critical BZZ Balance", "Low BZZ Balance", "Low xDAI Balance", "Low Base ETH"):
        assert "gateway_balance_check_ok" in rules[title]["annotations"]["description"], title


def test_apply_grafana_dry_run_lists_the_new_rules():
    result = subprocess.run(
        [sys.executable, os.path.join(REPO_ROOT, "scripts", "apply_grafana.py"), "--dry-run", "--only", "alerts"],
        capture_output=True, text=True, timeout=60, cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    for title in NEW_RULES:
        assert title in result.stdout, title


# --- the Alloy probe --------------------------------------------------------------

def test_alloy_probes_both_public_health_urls_by_environment():
    text = _alloy()
    assert 'prometheus.exporter.blackbox "public_health"' in text
    for name, url in (("main", "https://provenance-gateway.datafund.io/health"),
                      ("development", "https://provenance-gateway.dev.datafund.io/health")):
        assert re.search(r'name\s*=\s*"%s"\s*\n\s*address\s*=\s*"%s"' % (name, re.escape(url)), text), name
    assert 'replacement  = "swarm_connect/health_probe"' in text
    assert 'regex         = "integrations/blackbox/(.*)"' in text


def _compose_healthcheck_timeout():
    with open(COMPOSE) as f:
        timeouts = re.findall(r"healthcheck:\n(?:\s+\w+:.*\n)*?\s+timeout:\s*(\d+)s", f.read())
    assert timeouts, "no healthcheck timeout found in docker-compose.yml"
    return min(int(t) for t in timeouts)


def test_probe_timeout_is_shorter_than_the_docker_healthcheck():
    """So every check Docker counts towards 'unhealthy' also fails the probe."""
    m = re.search(r"http_health: \{ prober: http, timeout: (\d+)s", _alloy())
    assert m
    assert int(m.group(1)) < _compose_healthcheck_timeout()


def test_probe_up_series_cannot_count_as_a_gateway():
    """`up` of the probe scrape is not forwarded, and its job is not prometheus.scrape.*"""
    text = _alloy()
    block = text[text.index('prometheus.relabel "public_health"'):]
    block = block[:block.index("\n}\n")]
    keep = re.search(r'regex\s*=\s*"([^"]+)"', block).group(1)
    assert not re.fullmatch(keep, "up")
    assert re.fullmatch(keep, "probe_success")
    assert not re.fullmatch(r"prometheus\.scrape.*", "swarm_connect/health_probe")


def test_probe_targets_name_no_private_addresses():
    text = _alloy()
    # The probe section only: an older comment quotes a sample log line.
    text = text[text.index("Probe each environment's public /health"):text.index("Ship gateway container logs")]
    assert not re.search(r"\b(10|127|192\.168|172\.(1[6-9]|2\d|3[01]))\.\d+\.\d+", text)


# --- the body check against the real handler ---------------------------------------

def _probe_regex():
    m = re.search(r"fail_if_body_not_matches_regexp: \['(.+?)'\]", _alloy())
    assert m, "probe regex not found"
    # Go RE2 and Python agree on this subset (\{, [^{}], \s, character classes).
    return re.compile(m.group(1))


NODE = {"mode": "light", "version": "2.8.1-x", "api_version": "8.1.0", "bee_status": "ok",
        "overlay": "ab" * 32, "connected_peers": 140, "population": 14000, "depth": 10,
        "reachability": "Public", "network_availability": "Available", "warming_up": False,
        "healthy": True, "warnings": []}


def _health_body(node):
    from app.main import app
    with patch("app.services.swarm_api.get_node_status_summary", new_callable=AsyncMock) as summary, \
         patch("app.main.settings.X402_ENABLED", False):
        if isinstance(node, Exception):
            summary.side_effect = node
        else:
            summary.return_value = node
        response = TestClient(app).get("/health")
    assert response.status_code == 200
    return response.text


def test_probe_regex_matches_a_healthy_node():
    assert _probe_regex().search(_health_body(NODE))


@pytest.mark.parametrize("node", [
    dict(NODE, healthy=False, network_availability="Unavailable",
         warnings=["network availability is 'Unavailable'"]),
    dict(NODE, healthy=False, warnings=["could not query the Bee node (topology/status unavailable)"]),
    dict(NODE, healthy=False, warming_up=True, warnings=["node is warming up"]),
])
def test_probe_regex_rejects_an_unhealthy_node(node):
    assert not _probe_regex().search(_health_body(node))


def test_probe_regex_rejects_a_body_without_bee_node():
    body = _health_body(RuntimeError("Bee unreachable"))
    assert '"bee_node"' not in body
    assert not _probe_regex().search(body)


def test_probe_regex_is_not_fooled_by_a_healthy_key_elsewhere():
    body = json.dumps({"status": "ok", "bee_node": dict(NODE, healthy=False),
                       "other": {"healthy": True}}, separators=(",", ":"))
    assert not _probe_regex().search(body)
