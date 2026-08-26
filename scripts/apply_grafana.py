#!/usr/bin/env python3
"""Apply the dashboard and alert rules in monitoring/ to a Grafana instance.

Until now nothing did this. The JSON under monitoring/ described what should be
in Grafana; getting it there was a curl in a README, run by hand. So the two
drifted silently, and a merged change to either file was indistinguishable from
one that had been applied.

Needs a service-account token for the Grafana INSTANCE (glsa_ prefix). The
glc_ token on the gateway hosts is a Cloud Access Policy token scoped to
Prometheus remote-write and returns 401 here — the two are easy to confuse
because both are called a Grafana Cloud token.

    GRAFANA_URL=https://<stack>.grafana.net \\
    GRAFANA_SA_TOKEN=glsa_... \\
    python3 scripts/apply_grafana.py [--dry-run] [--only dashboard|alerts]

Idempotent: alert rules are matched by title and updated in place, so running
it twice does not create duplicates.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = ROOT / "monitoring/provisioning/dashboards/gateway-overview.json"
ALERT_RULES = ROOT / "monitoring/alerting/alert-rules.json"

# Alert rules live in a folder, and the exported rules in the repo carry
# folderUID: null because UIDs are per-instance and committing them would be
# wrong in any other Grafana. The folder is looked up by title instead.
ALERT_FOLDER_TITLE = "Provenance Gateway Alerts"

# A Grafana Cloud stack sleeps when idle, and a sleeping one answers 404 or 503
# with a body that reads exactly like a wrong hostname. Waking it is just
# hitting it and waiting.
WAKE_ATTEMPTS = 10
WAKE_DELAY_SECONDS = 15


class GrafanaError(RuntimeError):
    pass


def request(method, url, token, body=None, expect=(200, 201, 202)):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    # Deliberately no X-Disable-Provenance. The rules already in this instance
    # were created through the API and so carry provenance "api"; a write that
    # disagrees is refused with 409 alerting.provenanceMismatch.
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = resp.read().decode()
            if resp.status not in expect:
                raise GrafanaError(f"{method} {url} -> {resp.status}: {payload[:300]}")
            return json.loads(payload) if payload.strip() else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:300]
        raise GrafanaError(f"{method} {url} -> {exc.code}: {detail}") from None


def wake(base, token):
    """Poll until the stack answers, so a sleeping one is not read as a bad URL."""
    for attempt in range(1, WAKE_ATTEMPTS + 1):
        try:
            health = request("GET", f"{base}/api/health", token)
            print(f"  instance awake (Grafana {health.get('version', '?')})")
            return
        except GrafanaError as exc:
            if attempt == WAKE_ATTEMPTS:
                raise GrafanaError(
                    f"instance did not wake after {WAKE_ATTEMPTS} attempts: {exc}"
                ) from None
            print(f"  waking… attempt {attempt}/{WAKE_ATTEMPTS}")
            time.sleep(WAKE_DELAY_SECONDS)


def apply_dashboard(base, token, dry_run):
    payload = json.loads(DASHBOARD.read_text())
    title = payload["dashboard"]["title"]
    panels = len(payload["dashboard"]["panels"])
    print(f"dashboard: {title} ({panels} panels)")
    if dry_run:
        print("  dry run — not sent")
        return
    result = request("POST", f"{base}/api/dashboards/db", token, payload)
    print(f"  applied: {result.get('status', 'ok')} version {result.get('version')}")


def find_folder_uid(base, token):
    folders = request("GET", f"{base}/api/folders?limit=1000", token)
    for folder in folders:
        if folder.get("title") == ALERT_FOLDER_TITLE:
            return folder["uid"]
    raise GrafanaError(
        f"alert folder {ALERT_FOLDER_TITLE!r} not found. Create it in Grafana "
        "first — this script does not create folders, because guessing where "
        "alerts should live is how they end up somewhere nobody is watching."
    )


def apply_alerts(base, token, dry_run):
    desired = json.loads(ALERT_RULES.read_text())
    print(f"alert rules: {len(desired)} in repo")

    if dry_run:
        for rule in desired:
            state = "paused" if rule.get("isPaused") else "active"
            print(f"  {rule['title']} ({rule['ruleGroup']}, {state}) — dry run")
        return

    folder_uid = find_folder_uid(base, token)
    existing = request("GET", f"{base}/api/v1/provisioning/alert-rules", token)
    by_title = {r["title"]: r for r in existing}

    for rule in desired:
        rule = dict(rule)
        rule["folderUID"] = folder_uid
        rule.setdefault("orgID", 1)
        current = by_title.get(rule["title"])
        if current:
            # Keep the instance's own UID. UIDs are per-instance, which is why
            # they are not committed.
            rule["uid"] = current["uid"]
            request(
                "PUT",
                f"{base}/api/v1/provisioning/alert-rules/{current['uid']}",
                token,
                rule,
            )
            print(f"  updated {rule['title']}")
        else:
            request("POST", f"{base}/api/v1/provisioning/alert-rules", token, rule)
            state = "paused" if rule.get("isPaused") else "active"
            print(f"  created {rule['title']} ({state})")

    stale = sorted(set(by_title) - {r["title"] for r in desired})
    if stale:
        # Not deleted. A rule in Grafana but not in the repo may be someone's
        # deliberate hand-made addition, and silently removing it during an
        # unrelated deploy would be worse than reporting it.
        print(f"  note: {len(stale)} rule(s) in Grafana but not in this repo: {stale}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="parse and report, send nothing")
    parser.add_argument("--only", choices=("dashboard", "alerts"),
                        help="apply just one of the two")
    args = parser.parse_args()

    base = os.environ.get("GRAFANA_URL", "").rstrip("/")
    token = os.environ.get("GRAFANA_SA_TOKEN", "")

    if not args.dry_run:
        if not base or not token:
            sys.exit("GRAFANA_URL and GRAFANA_SA_TOKEN are required (or use --dry-run)")
        if not token.startswith("glsa_"):
            sys.exit(
                "GRAFANA_SA_TOKEN does not look like a service-account token "
                "(expected a glsa_ prefix). A glc_ token is a Cloud Access "
                "Policy token for metrics remote-write and returns 401 here."
            )
        wake(base, token)

    if args.only != "alerts":
        apply_dashboard(base, token, args.dry_run)
    if args.only != "dashboard":
        apply_alerts(base, token, args.dry_run)


if __name__ == "__main__":
    main()
