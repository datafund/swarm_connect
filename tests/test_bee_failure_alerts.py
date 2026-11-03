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
    "Public health probe missing",
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
    assert _threshold(rule) == ("gt", 0.2)
    assert rule["for"] == "5m"
    # One 502 at low traffic must not page.
    assert "and on (environment, host)" in expr
    assert ">= 20" in expr


HEALTH_HANDLERS = ("/", "/health", "/metrics")


def test_5xx_rule_leaves_health_traffic_out_of_the_share_and_the_guard():
    """Docker's healthcheck alone sends about 20 requests to / per 10 minutes.

    Counted, it met the 20-request guard by itself (75% of 10-minute windows on
    production in the week to 2026-11-03), and, never answered 5xx, it diluted
    the share. Every http_requests_total selector in the rule, the numerator,
    the denominator and the guard, must leave the health handlers out.
    """
    expr = _expr(_rules()["Gateway 5xx responses"])
    selectors = re.findall(r"http_requests_total(\{[^}]*\})?", expr)
    assert len(selectors) == 3, expr
    for selector in selectors:
        m = re.search(r'handler!~"([^"]+)"', selector)
        assert m, f"health traffic not excluded in {selector!r}"
        excluded = re.compile(m.group(1))
        for handler in HEALTH_HANDLERS:
            assert excluded.fullmatch(handler), handler
        # Anchored like PromQL: real handlers are not excluded by accident.
        for handler in ("/api/v1/stamps/", "/api/v1/data/{reference}", "/api/v1/wallet", "/docs"):
            assert not excluded.fullmatch(handler), handler
    assert 'status=~"5.."' in selectors[0]


def test_probe_rules_select_only_this_probe():
    """The stack holds Synthetic Monitoring probe_* series for other sites."""
    rules = _rules()
    for title in ("Public health check failing", "Public health check slow",
                  "Bee node not healthy (public /health)", "Public health probe missing"):
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


def test_probe_missing_rule_makes_a_lost_probe_visible():
    """The probe rules are silent on no data, so losing the probe needs its own rule.

    Each deploy recreates Alloy from the branch it deployed, so while the probe
    is only on dev, a main deploy removes it. This fires per environment when the
    gateway reports metrics and the probe does not.
    """
    rule = _rules()["Public health probe missing"]
    expr = _expr(rule)
    # Gateways only: not bee_up, and not the probe scrape (whose up is dropped).
    assert 'up{job=~"prometheus.scrape.(main|development)"}' in expr
    assert "unless on (environment, host)" in expr
    assert "last_over_time(probe_http_status_code{" + PROBE_JOB + "}[15m])" in expr
    assert _threshold(rule) == ("gt", 0)
    assert rule["for"] == "30m"
    assert rule["labels"]["severity"] == "warning"
    assert rule["noDataState"] == "OK"


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


# Bee's own answers, by endpoint, as get_node_status_summary() reads them. The
# /health body below is then built by the real summary and the real handler, so
# a change to either changes what these tests check.
BEE_HEALTHY = {
    "topology": {"networkAvailability": "Available", "population": 14000, "depth": 10,
                 "reachability": "Public", "connected": 140},
    "status": {"beeMode": "light", "connectedPeers": 140, "isWarmingUp": False,
               "neighborhoodSize": 141, "storageRadius": 0, "committedDepth": 0,
               "reserveSize": 0, "reserveSizeWithinRadius": 0, "pullsyncRate": 0,
               "batchCommitment": 7007502336, "lastSyncedBlock": 49087290},
    "health": {"status": "ok", "version": "2.8.1-x", "apiVersion": "8.1.0"},
    "addresses": {"overlay": "ab" * 32},
    "chainstate": {"chainTip": 49087297},
}


def _with(endpoint, **changes):
    answers = {k: dict(v) for k, v in BEE_HEALTHY.items()}
    answers[endpoint].update(changes)
    return answers


def _health_body(bee_answers, monkeypatch):
    """GET /health with Bee answering `bee_answers` (endpoint -> JSON, or None = failed)."""
    from app.main import app
    from app.services import swarm_api

    async def fake_fetch(client, endpoint):
        answer = bee_answers.get(endpoint)
        return dict(answer) if answer else {}

    monkeypatch.setattr(swarm_api, "_fetch_bee_json", fake_fetch)
    # The shared client exists only inside the app lifespan; fake_fetch ignores it.
    monkeypatch.setattr(swarm_api, "get_client", lambda: object())
    monkeypatch.setitem(swarm_api._node_status_cache, "data", None)
    monkeypatch.setitem(swarm_api._node_status_cache, "ts", 0.0)
    monkeypatch.setattr("app.main.settings.X402_ENABLED", False)
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    return response.text


def test_probe_regex_matches_a_healthy_node(monkeypatch):
    body = _health_body(BEE_HEALTHY, monkeypatch)
    assert json.loads(body)["bee_node"]["healthy"] is True
    assert _probe_regex().search(body)


def test_bee_node_has_no_nested_value_before_healthy(monkeypatch):
    """The regex reads up to `healthy` without crossing a brace.

    A nested object or list added to bee_node ahead of `healthy` would make the
    regex miss a healthy node and fire a false critical alert. That must fail
    here instead: move the new field after `healthy`, or change the regex.
    """
    node = json.loads(_health_body(BEE_HEALTHY, monkeypatch))["bee_node"]
    keys = list(node)
    assert "healthy" in keys
    for key in keys[:keys.index("healthy")]:
        assert not isinstance(node[key], (dict, list)), (
            f"bee_node.{key} is nested and comes before 'healthy'; the probe regex "
            "in monitoring/alloy/config.alloy would no longer match")


@pytest.mark.parametrize("answers", [
    _with("topology", networkAvailability="Unavailable"),
    _with("status", isWarmingUp=True),
    # The gateway cannot query the node at all: the 2026-11-03 case.
    {"topology": None, "status": None, "health": None, "addresses": None, "chainstate": None},
], ids=["network-unavailable", "warming-up", "bee-unreachable"])
def test_probe_regex_rejects_an_unhealthy_node(answers, monkeypatch):
    body = _health_body(answers, monkeypatch)
    assert json.loads(body)["bee_node"]["healthy"] is False
    assert not _probe_regex().search(body)


def test_probe_regex_rejects_a_body_without_bee_node(monkeypatch):
    from app.services import swarm_api

    async def broken(use_cache=True):
        raise RuntimeError("summary failed")

    monkeypatch.setattr(swarm_api, "get_node_status_summary", broken)
    body = _health_body(BEE_HEALTHY, monkeypatch)
    assert '"bee_node"' not in body
    assert not _probe_regex().search(body)


def test_probe_regex_is_not_fooled_by_a_healthy_key_elsewhere():
    body = json.dumps({"status": "ok", "bee_node": {"mode": "light", "healthy": False, "warnings": []},
                       "other": {"healthy": True}}, separators=(",", ":"))
    assert not _probe_regex().search(body)
