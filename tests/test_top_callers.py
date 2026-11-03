# tests/test_top_callers.py
"""
The heaviest callers, identified safely (#588).

The repository and the metric labels are public: no raw address, and no IPv6
/64, may appear in a metric or a log line. Callers appear only as keyed hash
tags in the log, and as ranks in the metrics.
"""
import logging
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.services import top_callers
from app.services.top_callers import TopCallerTracker, caller_tag, route_group

IPV4 = "203.0.113.77"
IPV6_A = "2001:db8:1234:5678::1"
IPV6_B = "2001:db8:1234:5678:ffff::2"   # same /64 as IPV6_A
IPV6_NET = "2001:db8:1234:5678::/64"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def tracker(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(settings, "TOP_CALLERS_WINDOW_SECONDS", 60)
    monkeypatch.setattr(settings, "TOP_CALLERS_N", 3)
    monkeypatch.setattr(settings, "TOP_CALLERS_LOG_MIN_REQUESTS", 10)
    monkeypatch.setattr(TopCallerTracker, "_now", staticmethod(clock))
    t = TopCallerTracker()
    t.clock = clock
    return t


def test_heaviest_caller_ranks_first(tracker):
    for _ in range(50):
        tracker.observe("198.51.100.1", 429, "/")
    for _ in range(20):
        tracker.observe("198.51.100.2", 200, "/health")
    tracker.observe("198.51.100.3", 200, "/")
    assert tracker.ranked_count(1) == 0  # the window is not complete yet
    tracker.clock.t += 60
    assert [tracker.ranked_count(r) for r in (1, 2, 3, 4)] == [50, 20, 1, 0]
    assert tracker.last_distinct() == 3
    assert tracker.last_ranked()[0][0] == tracker.tag("198.51.100.1")


def test_tags_are_keyed_hashes_not_addresses(tracker):
    tag = tracker.tag(IPV4)
    assert len(tag) == 12 and IPV4 not in tag
    # A fresh process has a fresh random key: tags do not link across restarts.
    assert TopCallerTracker().tag(IPV4) != tag


def test_configured_key_makes_tags_stable_and_computable(monkeypatch):
    monkeypatch.setattr(settings, "TOP_CALLERS_HASH_KEY", "operator-secret")
    a, b = TopCallerTracker(), TopCallerTracker()
    assert a.tag(IPV4) == b.tag(IPV4) == caller_tag(IPV4)
    assert caller_tag(IPV4, "other-secret") != a.tag(IPV4)


def test_log_names_tags_never_addresses(tracker, caplog):
    for _ in range(30):
        tracker.observe(IPV4, 429, "/")
    for _ in range(12):
        tracker.observe(IPV6_NET, 200, "/api/v1/data/" + "ab" * 32)
    with caplog.at_level(logging.WARNING, logger="app.services.top_callers"):
        tracker.clock.t += 60
        tracker.ranked_count(1)
    text = caplog.text
    assert "Top callers in the last 60 s (2 callers)" in text
    assert f"caller={tracker.tag(IPV4)} requests=30 statuses=429:30 top_route=/" in text
    assert "top_route=/api/v1/data" in text
    assert "ab" * 32 not in text
    for raw in (IPV4, "2001:db8", IPV6_NET):
        assert raw not in text


def test_no_log_below_the_threshold(tracker, caplog):
    for _ in range(9):
        tracker.observe(IPV4, 200, "/")
    with caplog.at_level(logging.WARNING, logger="app.services.top_callers"):
        tracker.clock.t += 60
        tracker.ranked_count(1)
    assert "Top callers" not in caplog.text


def test_tracked_callers_are_bounded(tracker, monkeypatch):
    from app.services.metrics import top_callers_untracked_total
    monkeypatch.setattr(settings, "TOP_CALLERS_MAX_TRACKED", 10)
    before = top_callers_untracked_total._value.get()
    for i in range(25):
        tracker.observe(f"198.51.100.{i}", 200, "/")
    tracker.observe("198.51.100.0", 200, "/")  # already tracked: still counted
    assert len(tracker._callers) == 10
    assert top_callers_untracked_total._value.get() == before + 15
    tracker.clock.t += 60
    assert tracker.ranked_count(1) == 2


def test_an_idle_window_reports_nothing(tracker):
    tracker.observe(IPV4, 200, "/")
    tracker.clock.t += 60 * 5
    assert tracker.ranked_count(1) == 0
    assert tracker.last_distinct() == 0


def test_route_group_is_bounded_and_safe():
    assert route_group("/") == "/"
    assert route_group("/health") == "/health"
    assert route_group("/api/v1/data/" + "ab" * 32) == "/api/v1/data"
    assert route_group("/api/v1/stamps/x/extend") == "/api/v1/stamps"
    assert route_group("/evil\nline") == "/other"
    assert route_group("/" + "a" * 40) == "/other"


# --------------------------------------------------------------------------
# Through the app
# --------------------------------------------------------------------------

def _get(client, path, ip):
    return client.get(path, headers={"X-Forwarded-For": ip})


def test_app_counts_root_and_health_and_groups_ipv6_by_64(monkeypatch):
    from app.main import app
    clock = Clock()
    monkeypatch.setattr(settings, "TOP_CALLERS_WINDOW_SECONDS", 60)
    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)
    monkeypatch.setattr(TopCallerTracker, "_now", staticmethod(clock))
    tracker = TopCallerTracker()
    monkeypatch.setattr(top_callers, "top_caller_tracker", tracker)
    import app.middleware.top_callers as mw
    monkeypatch.setattr(mw, "top_caller_tracker", tracker)

    client = TestClient(app)
    with patch("app.services.health_snapshot.get_snapshot", return_value=None):
        for _ in range(4):
            _get(client, "/", IPV6_A)
            _get(client, "/health", IPV6_B)
        _get(client, "/health", IPV4)
        client.get("/metrics")  # not a caller being served
    clock.t += 60
    assert tracker.ranked_count(1) == 8   # one /64
    assert tracker.ranked_count(2) == 1
    assert tracker.last_distinct() == 2


def test_metrics_carry_ranks_only(monkeypatch):
    from app.main import app
    clock = Clock()
    monkeypatch.setattr(TopCallerTracker, "_now", staticmethod(clock))
    tracker = TopCallerTracker()
    monkeypatch.setattr(top_callers, "top_caller_tracker", tracker)
    for _ in range(7):
        tracker.observe(IPV4, 200, "/")
    tracker.observe(IPV6_NET, 200, "/")
    clock.t += settings.TOP_CALLERS_WINDOW_SECONDS
    text = TestClient(app).get("/metrics").text
    lines = [l for l in text.splitlines() if l.startswith("gateway_top_caller")]
    assert 'gateway_top_caller_requests{rank="1"} 7.0' in lines
    assert 'gateway_top_caller_requests{rank="2"} 1.0' in lines
    assert len([l for l in lines if l.startswith("gateway_top_caller_requests{")]) == settings.TOP_CALLERS_N
    for raw in (IPV4, "2001:db8", tracker.tag(IPV4)):
        assert raw not in text
