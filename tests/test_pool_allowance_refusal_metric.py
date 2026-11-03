# tests/test_pool_allowance_refusal_metric.py
"""
Daily-allowance refusals on /pool/acquire are counted (#554).

The dashboard's "Pool Daily Allowance Refusals" panel read
gateway_pool_acquires_total{status="error"}, which is an empty pool or a lost
reservation race (409). A refusal because the allowance was spent incremented
no counter at all, so the panel could not show one. These tests drive the real
endpoint into each refusal and check the dedicated counter.
"""
import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from app.api.models.stamp import SIZE_PRESETS
from app.core.config import settings
from app.main import app
from app.services.pool_allowance import PoolAllowanceTracker

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = "https://dataprovenance.app"
METRIC = "gateway_pool_allowance_refusals_total"


def _refusals(size, scope, response):
    from app.services.metrics import pool_allowance_refusals_total
    return pool_allowance_refusals_total.labels(size=size, scope=scope, response=response)._value.get()


SIZES = tuple(SIZE_PRESETS) + ("other",)
SCOPES = ("origin", "client")
RESPONSES = ("402", "429", "409")


def _all_refusals():
    return {(size, scope, response): _refusals(size, scope, response)
            for size in SIZES for scope in SCOPES for response in RESPONSES}


def _pool_errors():
    from app.services.metrics import pool_acquires_total
    return sum(pool_acquires_total.labels(size=s, status="error")._value.get() for s in SIZE_PRESETS)


def _enable_mainnet_x402(monkeypatch):
    from unittest.mock import AsyncMock
    monkeypatch.setattr(settings, "X402_ENABLED", True)
    monkeypatch.setattr(settings, "X402_NETWORK", "base")
    monkeypatch.setattr(settings, "X402_PAY_TO_ADDRESS", "0xc87688A40CE2ff1765BA54497c7471c892755488")
    from app.x402 import dependency as dep
    monkeypatch.setattr(dep, "_calculate_price_for_request",
                        AsyncMock(return_value={"price_usd": 0.05, "description": "Pooled stamp"}))


@pytest.fixture
def pool(monkeypatch, tmp_path):
    """A stocked pool with a fresh allowance tracker; x402 off unless a test turns it on."""
    monkeypatch.setattr(settings, "STAMP_POOL_ENABLED", True)
    monkeypatch.setattr(settings, "X402_ENABLED", False)
    from app.services import pool_allowance
    tracker = PoolAllowanceTracker(state_file=str(tmp_path / "a.json"))
    monkeypatch.setattr(pool_allowance, "pool_allowance_tracker", tracker)
    import app.api.endpoints.pool as pool_ep
    monkeypatch.setattr(pool_ep, "pool_allowance_tracker", tracker)
    monkeypatch.setattr(pool_ep.stamp_pool_manager, "get_available_stamp",
                        lambda d: type("S", (), {"depth": SIZE_PRESETS["small"]})())
    return pool_ep


def _acquire(headers=None):
    return TestClient(app).post("/api/v1/pool/acquire", json={"size": "small"}, headers=headers or {})


def _only_changed(before, after):
    return {k: after[k] - before[k] for k in after if after[k] != before[k]}


class TestAllowanceRefusalsAreCounted:

    def test_origin_allowance_refusal_answered_429_is_counted(self, pool, monkeypatch):
        monkeypatch.setattr(settings, "POOL_DAILY_ALLOWANCES", f"{APP}=0")
        monkeypatch.setattr(settings, "POOL_DEFAULT_DAILY_ALLOWANCE", 0)
        before, errors_before = _all_refusals(), _pool_errors()

        resp = _acquire({"Origin": APP})

        assert resp.status_code == 429, resp.text
        assert resp.json()["detail"]["code"] == "DAILY_STAMP_ALLOWANCE_EXHAUSTED"
        assert _only_changed(before, _all_refusals()) == {("small", "origin", "429"): 1}
        # An allowance refusal is not a pool error.
        assert _pool_errors() == errors_before

    def test_origin_allowance_refusal_answered_402_is_counted(self, pool, monkeypatch):
        _enable_mainnet_x402(monkeypatch)
        monkeypatch.setattr(settings, "POOL_DAILY_ALLOWANCES", f"{APP}=0")
        monkeypatch.setattr(settings, "POOL_DEFAULT_DAILY_ALLOWANCE", 0)
        before = _all_refusals()

        resp = _acquire({"Origin": APP})

        assert resp.status_code == 402, resp.text
        assert resp.json()["detail"]["code"] == "DAILY_STAMP_ALLOWANCE_EXHAUSTED"
        assert _only_changed(before, _all_refusals()) == {("small", "origin", "402"): 1}

    def test_per_client_refusal_is_counted_with_client_scope(self, pool, monkeypatch):
        monkeypatch.setattr(settings, "POOL_DAILY_ALLOWANCES", "")
        monkeypatch.setattr(settings, "POOL_DEFAULT_DAILY_ALLOWANCE", 10)
        monkeypatch.setattr(settings, "POOL_ALLOWANCE_PER_IP", 0)
        before = _all_refusals()

        resp = _acquire()

        assert resp.status_code == 429, resp.text
        assert resp.json()["detail"]["code"] == "DAILY_STAMP_ALLOWANCE_PER_CLIENT_EXHAUSTED"
        assert _only_changed(before, _all_refusals()) == {("small", "client", "429"): 1}

    def test_an_empty_pool_is_not_an_allowance_refusal(self, pool, monkeypatch):
        monkeypatch.setattr(settings, "POOL_DAILY_ALLOWANCES", f"{APP}=0")
        monkeypatch.setattr(settings, "POOL_DEFAULT_DAILY_ALLOWANCE", 0)
        monkeypatch.setattr(pool.stamp_pool_manager, "get_available_stamp", lambda d: None)
        monkeypatch.setattr(pool.stamp_pool_manager, "get_available_stamp_any_size", lambda d: None)
        before, errors_before = _all_refusals(), _pool_errors()

        resp = _acquire({"Origin": APP})

        assert resp.status_code == 409, resp.text
        assert _all_refusals() == before
        assert _pool_errors() == errors_before + 1


def _serve_only_a_larger_batch(pool, monkeypatch, depth):
    """The requested size is out of stock; a batch of `depth` could stand in."""
    monkeypatch.setattr(pool.stamp_pool_manager, "get_available_stamp", lambda d: None)
    monkeypatch.setattr(pool.stamp_pool_manager, "get_available_stamp_any_size",
                        lambda d: type("S", (), {"depth": depth})())


class TestFallbackRefusalsAreCounted:
    """A free acquire whose size is out of stock may be served a larger batch,
    charged to the larger size's allowance. When that allowance is spent the
    answer is 409 REQUESTED_SIZE_UNAVAILABLE, and it is still an allowance
    refusal: counted with response 409 and the larger size."""

    def test_origin_allowance_spent_on_the_larger_size(self, pool, monkeypatch):
        monkeypatch.setattr(settings, "POOL_DAILY_ALLOWANCES", f"{APP}=0")
        monkeypatch.setattr(settings, "POOL_DEFAULT_DAILY_ALLOWANCE", 0)
        _serve_only_a_larger_batch(pool, monkeypatch, SIZE_PRESETS["medium"])
        before, errors_before = _all_refusals(), _pool_errors()

        resp = _acquire({"Origin": APP})

        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["code"] == "REQUESTED_SIZE_UNAVAILABLE"
        assert _only_changed(before, _all_refusals()) == {("medium", "origin", "409"): 1}
        assert _pool_errors() == errors_before

    def test_client_cap_spent_on_the_larger_size(self, pool, monkeypatch):
        monkeypatch.setattr(settings, "POOL_DAILY_ALLOWANCES", "")
        monkeypatch.setattr(settings, "POOL_DEFAULT_DAILY_ALLOWANCE", 10)
        monkeypatch.setattr(settings, "POOL_ALLOWANCE_PER_IP", 0)
        _serve_only_a_larger_batch(pool, monkeypatch, SIZE_PRESETS["large"])
        before = _all_refusals()

        resp = _acquire()

        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["code"] == "REQUESTED_SIZE_UNAVAILABLE"
        assert _only_changed(before, _all_refusals()) == {("large", "client", "409"): 1}

    def test_a_non_preset_depth_is_counted_as_other(self, pool, monkeypatch):
        """The pool can hold a batch of a non-preset depth (adopted from Bee at
        the depth Bee reports). Its size name is depth-N, which must not become
        a label value of its own."""
        monkeypatch.setattr(settings, "POOL_DAILY_ALLOWANCES", f"{APP}=0")
        monkeypatch.setattr(settings, "POOL_DEFAULT_DAILY_ALLOWANCE", 0)
        _serve_only_a_larger_batch(pool, monkeypatch, 18)
        before = _all_refusals()

        resp = _acquire({"Origin": APP})

        assert resp.status_code == 409, resp.text
        assert _only_changed(before, _all_refusals()) == {("other", "origin", "409"): 1}
        text = TestClient(app).get("/metrics").text
        assert not [l for l in text.splitlines() if l.startswith(METRIC + "{") and "depth-" in l]


class TestSeriesAndLabels:

    def test_every_series_exists_at_zero_in_a_fresh_process(self):
        code = (
            "import app.services.metrics\n"
            "from prometheus_client import REGISTRY, generate_latest\n"
            "print(generate_latest(REGISTRY).decode())\n"
        )
        result = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT,
                                capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stderr
        lines = result.stdout.splitlines()
        expected = [
            f'{METRIC}{{response="{r}",scope="{sc}",size="{s}"}} 0.0'
            for s in SIZES for sc in SCOPES for r in RESPONSES
        ]
        assert len(expected) == 24
        missing = expected
        missing = [m for m in missing if m not in lines]
        assert not missing, f"not created at 0 at startup: {missing}"

    def test_labels_carry_no_caller_identity(self, pool, monkeypatch):
        from app.services.metrics import pool_allowance_refusals_total
        assert tuple(pool_allowance_refusals_total._labelnames) == ("size", "scope", "response")

        monkeypatch.setattr(settings, "POOL_DAILY_ALLOWANCES", f"{APP}=0")
        monkeypatch.setattr(settings, "POOL_DEFAULT_DAILY_ALLOWANCE", 0)
        _acquire({"Origin": APP, "X-Forwarded-For": "203.0.113.7"})

        text = TestClient(app).get("/metrics").text
        lines = [l for l in text.splitlines() if l.startswith(METRIC + "{")]
        assert lines, "the counter is not served on /metrics"
        for line in lines:
            assert "dataprovenance" not in line
            assert "203.0.113" not in line
            assert "testclient" not in line
