"""Daily per-origin allowance on pooled batches.

The pool pre-buys postage and pays to keep it alive, and `/pool/acquire` had no
gate at all: 3,866 acquire calls in one day drove 40 replacement purchases on
staging, and an anonymous caller took a production batch.

The consumer that made this hard is a static browser app — no backend, no
identity of its own, borrowing the visitor's wallet. There is no address to
allow-list and no key it could sign with, so the control has to be something a
browser supplies. `Origin` is that, with the important caveat these tests pin:
it is attribution with a budget, not authentication.
"""
import json

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.services.pool_allowance import PoolAllowanceTracker, UNLIMITED

APP = "https://dataprovenance.app"


@pytest.fixture
def tracker(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "POOL_DAILY_ALLOWANCES", f"{APP}=3")
    monkeypatch.setattr(settings, "POOL_DEFAULT_DAILY_ALLOWANCE", 1)
    return PoolAllowanceTracker(state_file=str(tmp_path / "allowance.json"))


class TestAllowanceCounting:
    def test_named_origin_gets_its_configured_budget(self, tracker):
        for i in range(3):
            ok, info = tracker.check(APP)
            assert ok, f"refused at {i} of an allowance of 3"
            tracker.consume(APP)
        ok, info = tracker.check(APP)
        assert not ok
        assert info["used"] == 3 and info["allowance"] == 3

    def test_unlisted_origin_falls_back_to_the_default(self, tracker):
        ok, _ = tracker.check("https://someone-else.example")
        assert ok
        tracker.consume("https://someone-else.example")
        ok, _ = tracker.check("https://someone-else.example")
        assert not ok, "an unlisted origin exceeded the default allowance of 1"

    def test_origins_have_separate_budgets(self, tracker):
        for _ in range(3):
            tracker.consume(APP)
        ok, _ = tracker.check("https://other.example")
        assert ok, "one origin exhausting its budget blocked a different origin"

    def test_no_origin_header_uses_the_default(self, tracker):
        """CLIs, SDKs and server-to-server callers send no Origin."""
        ok, info = tracker.check(None)
        assert ok and info["origin"] == "(none)"
        tracker.consume(None)
        ok, _ = tracker.check(None)
        assert not ok

    def test_port_and_trailing_slash_do_not_defeat_matching(self, tracker):
        """A caller sending https://app:443/ must hit the same budget as https://app."""
        for _ in range(3):
            tracker.consume(f"{APP}:443/")
        ok, _ = tracker.check(APP)
        assert not ok, "the same origin written differently got a second budget"

    def test_unlimited_default_is_the_pre_existing_behaviour(self, tmp_path, monkeypatch):
        """Deploying this must change nothing until allowances are configured."""
        monkeypatch.setattr(settings, "POOL_DAILY_ALLOWANCES", "")
        monkeypatch.setattr(settings, "POOL_DEFAULT_DAILY_ALLOWANCE", UNLIMITED)
        t = PoolAllowanceTracker(state_file=str(tmp_path / "a.json"))
        for _ in range(500):
            ok, _ = t.check(APP)
            assert ok
            t.consume(APP)


class TestPersistence:
    def test_a_restart_does_not_grant_a_fresh_allowance(self, tmp_path, monkeypatch):
        """Otherwise a crash loop hands out a full budget per restart.

        That is the shape of the incident this exists to prevent, so an in-memory
        counter would leave the hole open in the case that matters most.
        """
        monkeypatch.setattr(settings, "POOL_DAILY_ALLOWANCES", f"{APP}=2")
        monkeypatch.setattr(settings, "POOL_DEFAULT_DAILY_ALLOWANCE", 0)
        path = str(tmp_path / "allowance.json")

        first = PoolAllowanceTracker(state_file=path)
        first.consume(APP)
        first.consume(APP)
        assert not first.check(APP)[0]

        second = PoolAllowanceTracker(state_file=path)
        ok, info = second.check(APP)
        assert not ok, f"a restart reset the allowance: {info}"

    def test_state_from_a_previous_day_is_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "POOL_DAILY_ALLOWANCES", f"{APP}=2")
        path = str(tmp_path / "allowance.json")
        with open(path, "w") as f:
            json.dump({"day": "2020-01-01", "used": {APP: 99}}, f)

        t = PoolAllowanceTracker(state_file=path)
        ok, info = t.check(APP)
        assert ok, "yesterday's usage still counted against today"
        assert info["used"] == 0


class TestEndpointBehaviour:
    def test_exhausted_allowance_returns_a_message_a_person_can_read(self, monkeypatch, tmp_path):
        """The caller is a browser app whose user has never heard of a postage batch."""
        monkeypatch.setattr(settings, "STAMP_POOL_ENABLED", True)
        monkeypatch.setattr(settings, "POOL_DAILY_ALLOWANCES", f"{APP}=0")
        monkeypatch.setattr(settings, "POOL_DEFAULT_DAILY_ALLOWANCE", 0)

        from app.services import pool_allowance
        monkeypatch.setattr(pool_allowance, "pool_allowance_tracker",
                            PoolAllowanceTracker(state_file=str(tmp_path / "a.json")))
        import app.api.endpoints.pool as pool_ep
        monkeypatch.setattr(pool_ep, "pool_allowance_tracker",
                            pool_allowance.pool_allowance_tracker)

        resp = TestClient(app).post("/api/v1/pool/acquire", json={"size": "small"},
                                    headers={"Origin": APP})
        assert resp.status_code == 429, resp.text
        d = resp.json()["detail"]
        assert d["code"] == "DAILY_STAMP_ALLOWANCE_EXHAUSTED"
        assert "resets" in d["message"].lower()
        assert "alternative" in d, "no route forward was offered"
        assert d["resets_at"]

    def test_a_refused_acquire_does_not_spend_the_allowance(self, tmp_path, monkeypatch):
        """The pool being empty must not cost the caller a day's budget."""
        monkeypatch.setattr(settings, "STAMP_POOL_ENABLED", True)
        monkeypatch.setattr(settings, "POOL_DAILY_ALLOWANCES", f"{APP}=5")
        tracker = PoolAllowanceTracker(state_file=str(tmp_path / "a.json"))

        from app.services import pool_allowance
        monkeypatch.setattr(pool_allowance, "pool_allowance_tracker", tracker)
        import app.api.endpoints.pool as pool_ep
        monkeypatch.setattr(pool_ep, "pool_allowance_tracker", tracker)
        monkeypatch.setattr(pool_ep.stamp_pool_manager, "get_available_stamp", lambda d: None)
        monkeypatch.setattr(pool_ep.stamp_pool_manager, "get_available_stamp_any_size", lambda d: None)

        resp = TestClient(app).post("/api/v1/pool/acquire", json={"size": "small"},
                                    headers={"Origin": APP})
        assert resp.status_code == 409, resp.text
        _, info = tracker.check(APP)
        assert info["used"] == 0, "an empty pool consumed the caller's allowance"
