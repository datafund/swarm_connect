# tests/test_log_delivery_alerts.py
"""Alerts for gateway logs that never reach Loki (#672).

In #661 the Alloy token lacked the logs:write scope. Loki refused every push
with 401, Alloy dropped each batch after its retries, and nothing alerted,
because the metrics pipeline kept working. Two rules close that gap: one on
Alloy's own drop counter (names the cause), one on Loki itself (catches Alloy
not running or not finding the containers).
"""
import json
import os
import re
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALERT_RULES = os.path.join(REPO_ROOT, "monitoring", "alerting", "alert-rules.json")
ALLOY = os.path.join(REPO_ROOT, "monitoring", "alloy", "config.alloy")
DROPPED = "Gateway logs dropped"
MISSING = "Gateway logs missing in Loki"


def _rules():
    with open(ALERT_RULES) as f:
        return {r["title"]: r for r in json.load(f)}


def _query(rule, ref):
    return next(q for q in rule["data"] if q["refId"] == ref)


def _alloy():
    with open(ALLOY) as f:
        return f.read()


def _alloy_block(header):
    """The body of one Alloy component, from its header to the matching brace."""
    text = _alloy()
    start = text.index(header)
    depth = 0
    for i in range(text.index("{", start), len(text)):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        if depth == 0:
            return text[start:i + 1]
    raise AssertionError(f"unterminated block {header}")


def test_both_rules_are_warnings_with_the_usual_three_nodes():
    rules = _rules()
    for title in (DROPPED, MISSING):
        rule = rules[title]
        assert [q["refId"] for q in rule["data"]] == ["A", "B", "C"], title
        assert rule["condition"] == "C", title
        assert rule["labels"] == {"severity": "warning"}, title
        assert rule["ruleGroup"] == "warning", title
        # Both express absence or loss as a series, so no data means all is well.
        assert rule["noDataState"] == "OK", title
        assert rule["execErrState"] == "Error", title
        assert rule["isPaused"] is False, title
        b = _query(rule, "B")["model"]
        assert b["type"] == "reduce" and b["reducer"] == "last" and b["expression"] == "A"
        c = _query(rule, "C")["model"]
        assert c["type"] == "threshold" and c["expression"] == "B"
        assert c["conditions"][0]["evaluator"] == {"params": [0], "type": "gt"}


def test_dropped_rule_reads_alloys_drop_counter():
    rule = _rules()[DROPPED]
    a = _query(rule, "A")
    assert a["datasourceUid"] == "grafanacloud-prom"
    expr = a["model"]["expr"]
    assert 'increase(loki_write_dropped_entries_total{job="swarm_connect/alloy"}[15m])' in expr
    # One Alloy serves both environments, so per host; reason names the cause.
    assert "sum by (host, reason)" in expr
    assert a["relativeTimeRange"]["from"] >= 900
    assert rule["for"] == "5m"


def test_missing_rule_reads_loki_for_each_environment():
    rule = _rules()[MISSING]
    a = _query(rule, "A")
    assert a["datasourceUid"] == "grafanacloud-logs"
    assert a["model"]["queryType"] == "instant"
    expr = a["model"]["expr"]
    for env in ("main", "development"):
        assert f'absent_over_time({{job="swarm_connect/gateway", environment="{env}"}}[15m])' in expr
    assert " or " in expr
    assert rule["for"] == "15m"


def test_descriptions_tell_the_operator_what_to_do():
    rules = _rules()
    dropped = rules[DROPPED]["annotations"]
    assert "{{ $labels.reason }}" in dropped["summary"]
    for needed in ("final error sending batch", "logs:write", "invalid scope requested",
                   "GRAFANA_CLOUD_LOKI_USERNAME", "#661", "#672", "mainnet-runbook"):
        assert needed in dropped["description"], needed
    missing = rules[MISSING]["annotations"]
    assert "{{ $labels.environment }}" in missing["summary"]
    for needed in ("Gateway Down", DROPPED, "docker logs", "#672", "mainnet-runbook"):
        assert needed in missing["description"], needed


def test_alloy_exports_only_the_two_delivery_counters():
    assert 'prometheus.exporter.self "alloy"' in _alloy()
    scrape = _alloy_block('prometheus.scrape "alloy_self"')
    assert "prometheus.exporter.self.alloy.targets" in scrape
    assert "prometheus.relabel.alloy_self.receiver" in scrape
    relabel = _alloy_block('prometheus.relabel "alloy_self"')
    m = re.search(r'action\s*=\s*"keep"\s*source_labels\s*=\s*\["__name__"\]\s*regex\s*=\s*"([^"]+)"', relabel)
    assert m, "the alloy_self relabel must start with a keep on __name__"
    keep = re.compile(m.group(1))
    assert keep.fullmatch("loki_write_dropped_entries_total")
    assert keep.fullmatch("loki_write_sent_entries_total")
    # `up` must never pass: "Gateway Down" and "No Data" read up.
    for name in ("up", "loki_write_dropped_bytes_total", "alloy_build_info"):
        assert not keep.fullmatch(name), name
    assert "prometheus.remote_write.grafana_cloud.receiver" in relabel


def test_alloy_self_series_carry_the_rule_labels():
    relabel = _alloy_block('prometheus.relabel "alloy_self"')
    # The job the rule selects on.
    assert re.search(r'target_label\s*=\s*"job"\s*replacement\s*=\s*"swarm_connect/alloy"', relabel)
    # loki.write's own host label (the Loki hostname) is moved out of the way
    # before host is set to this machine, as on every other series.
    move = relabel.index('target_label  = "loki_host"')
    own = relabel.index('replacement  = sys.env("HOST_LABEL")')
    assert move < own


def test_runbook_readme_and_claude_md_name_both_alerts():
    for doc in ("docs/mainnet-runbook.md", "monitoring/alerting/README.md", "CLAUDE.md"):
        with open(os.path.join(REPO_ROOT, doc)) as f:
            text = f.read()
        for title in (DROPPED, MISSING):
            assert title in text, (doc, title)


def test_apply_grafana_dry_run_lists_both_rules():
    result = subprocess.run(
        [sys.executable, "scripts/apply_grafana.py", "--dry-run", "--only", "alerts"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
        env={k: v for k, v in os.environ.items() if not k.startswith("GRAFANA_")},
    )
    assert result.returncode == 0, result.stderr
    for title in (DROPPED, MISSING):
        assert f"{title} (warning, active)" in result.stdout
