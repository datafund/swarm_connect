# tests/test_dashboard_layout.py
"""
No two panels on the gateway dashboard overlap.

Panels 73 and 80 overlapped for months: 73 covered rows 64-71 and 80 started
at row 68. Grafana resolves an overlap by pushing panels around at load time,
so the dashboard rendered, just not in the layout the JSON describes, and a
hand edit that moved one panel could silently reshuffle others.
"""
import json
import os
from itertools import combinations

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(REPO_ROOT, "monitoring/provisioning/dashboards/gateway-overview.json")
GRID_COLUMNS = 24


def _panels():
    with open(DASHBOARD) as f:
        dashboard = json.load(f)["dashboard"]

    def walk(panels):
        for p in panels:
            yield p
            # A collapsed row keeps its panels nested; they are laid out below it.
            yield from walk(p.get("panels", []))
    return list(walk(dashboard["panels"]))


def _overlap(a, b):
    ga, gb = a["gridPos"], b["gridPos"]
    return (ga["x"] < gb["x"] + gb["w"] and gb["x"] < ga["x"] + ga["w"]
            and ga["y"] < gb["y"] + gb["h"] and gb["y"] < ga["y"] + ga["h"])


def test_no_two_panels_overlap():
    overlaps = [(a["id"], b["id"]) for a, b in combinations(_panels(), 2) if _overlap(a, b)]
    assert not overlaps, f"overlapping panels (id pairs): {overlaps}"


def test_every_panel_fits_the_grid():
    outside = [p["id"] for p in _panels()
               if p["gridPos"]["x"] < 0 or p["gridPos"]["x"] + p["gridPos"]["w"] > GRID_COLUMNS]
    assert not outside, f"panels wider than the {GRID_COLUMNS}-column grid: {outside}"


def test_panel_ids_are_unique():
    ids = [p["id"] for p in _panels()]
    assert len(ids) == len(set(ids)), "duplicate panel ids"
