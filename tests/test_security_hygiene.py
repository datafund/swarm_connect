"""#380: bandwidth credit token rotation and the persisted pool spend ceiling."""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.services.bandwidth_credit import BandwidthCreditManager
from app.services.stamp_pool import StampPoolManager

ADDR = "0x" + "ab" * 20


class TestCreditTokenRotation:
    def test_rotation_revokes_the_old_token(self, tmp_path):
        m = BandwidthCreditManager(state_file=str(tmp_path / "c.json"))
        old = m.issue_token(ADDR)
        assert m.issue_token(ADDR) == old            # still idempotent by default
        new = m.issue_token(ADDR, rotate=True)
        assert new != old
        assert m.resolve_token(old) is None
        assert m.resolve_token(new) == ADDR.lower()

    def test_rotation_survives_a_restart(self, tmp_path):
        path = str(tmp_path / "c.json")
        m = BandwidthCreditManager(state_file=path)
        m.credit(ADDR, 10)
        old = m.issue_token(ADDR)
        new = m.issue_token(ADDR, rotate=True)
        m2 = BandwidthCreditManager(state_file=path)
        m2.load_on_startup()
        assert m2.resolve_token(old) is None and m2.resolve_token(new)

    def test_endpoint_rotates_with_the_current_token(self, tmp_path, monkeypatch):
        from app.main import app
        import app.api.endpoints.chunks as chunks
        m = BandwidthCreditManager(state_file=str(tmp_path / "c.json"))
        monkeypatch.setattr(chunks, "bandwidth_credit_manager", m)
        old = m.issue_token(ADDR)
        client = TestClient(app)
        r = client.post("/api/v1/chunks/token/rotate", headers={"X-Bandwidth-Credit-Token": old})
        assert r.status_code == 200, r.text
        assert r.json()["token"] != old
        r2 = client.post("/api/v1/chunks/token/rotate", headers={"X-Bandwidth-Credit-Token": old})
        assert r2.status_code == 401
        assert r2.json()["detail"]["code"] == "INVALID_CREDIT_TOKEN"


class TestPoolSpendCeilingPersists:
    def test_a_restart_does_not_grant_a_fresh_hour(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "STAMP_POOL_MAX_PURCHASES_PER_HOUR", 3)
        path = str(tmp_path / "pool_state.json")
        a = StampPoolManager(state_file=path)
        a._record_spend(); a._record_spend()
        b = StampPoolManager(state_file=path)
        assert b._spend_budget_remaining() == 1

    def test_spends_older_than_an_hour_are_dropped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "STAMP_POOL_MAX_PURCHASES_PER_HOUR", 3)
        path = tmp_path / "pool_state.json"
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        (tmp_path / "pool_state_spend_times.json").write_text(json.dumps([old, old]))
        assert StampPoolManager(state_file=str(path))._spend_budget_remaining() == 3

    def test_an_unreadable_record_pauses_spending(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "STAMP_POOL_MAX_PURCHASES_PER_HOUR", 3)
        (tmp_path / "pool_state_spend_times.json").write_text("{not json")
        m = StampPoolManager(state_file=str(tmp_path / "pool_state.json"))
        assert m._spend_budget_remaining() == 0

