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



class TestRotationIsDurable:
    def test_a_rotation_that_cannot_be_saved_is_undone(self, tmp_path):
        from app.services import bandwidth_credit
        m = BandwidthCreditManager(state_file=str(tmp_path / "c.json"))
        old = m.issue_token(ADDR)
        with patch.object(bandwidth_credit, "atomic_write_json", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                m.issue_token(ADDR, rotate=True)
        assert m.resolve_token(old) == ADDR.lower()
        assert m.issue_token(ADDR) == old

    def test_the_endpoint_answers_503_and_keeps_the_token(self, tmp_path, monkeypatch):
        from app.main import app
        from app.services import bandwidth_credit
        import app.api.endpoints.chunks as chunks
        m = BandwidthCreditManager(state_file=str(tmp_path / "c.json"))
        monkeypatch.setattr(chunks, "bandwidth_credit_manager", m)
        old = m.issue_token(ADDR)
        with patch.object(bandwidth_credit, "atomic_write_json", side_effect=OSError("disk full")):
            r = TestClient(app).post("/api/v1/chunks/token/rotate", headers={"X-Bandwidth-Credit-Token": old})
        assert r.status_code == 503
        assert r.json()["detail"]["code"] == "TOKEN_ROTATION_FAILED"
        assert m.resolve_token(old) == ADDR.lower()


class TestPaidRotation:
    @pytest.mark.asyncio
    async def test_a_paid_top_up_takes_the_account_back(self, tmp_path, monkeypatch):
        """Mixed-case payer, as a facilitator may report it: same account."""
        from types import SimpleNamespace
        import app.api.endpoints.chunks as chunks
        monkeypatch.setattr(settings, "CHUNK_UPLOAD_ENABLED", True)
        monkeypatch.setattr(settings, "X402_ENABLED", True)
        m = BandwidthCreditManager(state_file=str(tmp_path / "c.json"))
        monkeypatch.setattr(chunks, "bandwidth_credit_manager", m)
        stolen = m.issue_token(ADDR)
        attackers = m.issue_token(ADDR, rotate=True)          # the thief rotates first
        req = SimpleNamespace(state=SimpleNamespace(x402_mode="paid", x402_payer=ADDR.upper().replace("0X", "0x")))
        mb = str(settings.BANDWIDTH_CREDIT_MIN_TOPUP_MB)
        resp = await chunks.top_up_credit(req, mb=mb, rotate_token=True)
        assert resp.token not in (stolen, attackers)
        assert m.resolve_token(attackers) is None
        assert m.resolve_token(resp.token) == ADDR.lower()
        plain = await chunks.top_up_credit(req, mb=mb, rotate_token=False)
        assert plain.token == resp.token                      # a plain top-up returns the current token
