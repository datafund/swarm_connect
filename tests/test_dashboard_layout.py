# tests/test_dashboard_layout.py
"""
No two panels on the gateway dashboard overlap.

Panels 73 and 80 overlapped for months: 73 covered rows 64-71 and 80 started
at row 68. Grafana resolves an overlap by pushing panels around at load time,
so the dashboard rendered, just not in the layout the JSON describes, and a
hand edit that moved one panel could silently reshuffle others.

A collapsed row keeps its panels nested in its own "panels" list, with the y
values they had when the row was open, while the panels below move up into
the space. So nested panels are not in the top-level coordinate space: they
are checked only against each other, and top-level panels (rows included)
only against each other.
"""
import json
import os
from itertools import combinations

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(REPO_ROOT, "monitoring/provisioning/dashboards/gateway-overview.json")
GRID_COLUMNS = 24


def _top_level_panels():
    with open(DASHBOARD) as f:
        return json.load(f)["dashboard"]["panels"]


def _all_panels(panels):
    for p in panels:
        yield p
        yield from p.get("panels", [])


def _overlap(a, b):
    ga, gb = a["gridPos"], b["gridPos"]
    return (ga["x"] < gb["x"] + gb["w"] and gb["x"] < ga["x"] + ga["w"]
            and ga["y"] < gb["y"] + gb["h"] and gb["y"] < ga["y"] + ga["h"])


def find_overlaps(panels):
    """Overlapping id pairs, each layout space checked on its own: the top
    level, and the nested panels of each collapsed row."""
    spaces = [panels] + [p["panels"] for p in panels if p.get("panels")]
    return [(a["id"], b["id"])
            for space in spaces
            for a, b in combinations(space, 2) if _overlap(a, b)]


def _panel(pid, x, y, w, h, **extra):
    return {"id": pid, "gridPos": {"x": x, "y": y, "w": w, "h": h}, **extra}


def test_no_two_panels_overlap():
    overlaps = find_overlaps(_top_level_panels())
    assert not overlaps, f"overlapping panels (id pairs): {overlaps}"


def test_every_panel_fits_the_grid():
    outside = [p["id"] for p in _all_panels(_top_level_panels())
               if p["gridPos"]["x"] < 0 or p["gridPos"]["x"] + p["gridPos"]["w"] > GRID_COLUMNS]
    assert not outside, f"panels wider than the {GRID_COLUMNS}-column grid: {outside}"


def test_panel_ids_are_unique():
    ids = [p["id"] for p in _all_panels(_top_level_panels())]
    assert len(ids) == len(set(ids)), "duplicate panel ids"


class TestFindOverlaps:
    """The checker itself, on synthetic layouts."""

    def test_a_real_overlap_is_reported(self):
        panels = [_panel(1, 0, 0, 8, 8), _panel(2, 0, 4, 12, 8)]
        assert find_overlaps(panels) == [(1, 2)]

    def test_panels_that_only_touch_do_not_overlap(self):
        panels = [_panel(1, 0, 0, 8, 8), _panel(2, 8, 0, 8, 8), _panel(3, 0, 8, 24, 4)]
        assert find_overlaps(panels) == []

    def test_a_collapsed_rows_panels_are_not_checked_against_the_top_level(self):
        # Row collapsed at y=10: its panels keep y=11 from when it was open,
        # and the panel below the row has moved up to y=11 into the freed space.
        panels = [
            _panel(1, 0, 0, 24, 10),
            _panel(10, 0, 10, 24, 1, type="row", collapsed=True,
                   panels=[_panel(11, 0, 11, 12, 8), _panel(12, 12, 11, 12, 8)]),
            _panel(2, 0, 11, 24, 8),
        ]
        assert find_overlaps(panels) == []

    def test_an_overlap_inside_a_collapsed_row_is_reported(self):
        panels = [
            _panel(10, 0, 0, 24, 1, type="row", collapsed=True,
                   panels=[_panel(11, 0, 1, 12, 8), _panel(12, 6, 3, 12, 8)]),
            _panel(2, 0, 1, 24, 8),
        ]
        assert find_overlaps(panels) == [(11, 12)]
