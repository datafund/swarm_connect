# tests/test_stamp_propagation.py
"""Tests for stamp propagation timing signals."""
import datetime
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from app.main import app
from app.services import stamp_tracker
from app.services.swarm_api import calculate_propagation_signals

client = TestClient(app)

VALID_STAMP_ID = "a" * 64
VALID_STAMP_ID_B = "b" * 64


class TestStampTracker:
    """Unit tests for the in-memory stamp purchase tracker."""

    def setup_method(self):
        stamp_tracker.clear_tracker()

    def test_record_and_retrieve(self):
        """Record a purchase and retrieve its timestamp."""
        stamp_tracker.record_purchase("batch123")
        result = stamp_tracker.get_purchase_time("batch123")
        assert result is not None
        assert isinstance(result, datetime.datetime)
        assert result.tzinfo == datetime.timezone.utc

    def test_unknown_stamp_returns_none(self):
        """Unknown stamp returns None."""
        result = stamp_tracker.get_purchase_time("nonexistent")
        assert result is None

    def test_clear_tracker(self):
        """clear_tracker removes all entries."""
        stamp_tracker.record_purchase("batch1")
        stamp_tracker.record_purchase("batch2")
        stamp_tracker.clear_tracker()
        assert stamp_tracker.get_purchase_time("batch1") is None
        assert stamp_tracker.get_purchase_time("batch2") is None

    def test_auto_cleanup_old_entries(self):
        """Entries older than 10 minutes are pruned on next record_purchase."""
        # Insert an old entry directly
        old_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=15)
        stamp_tracker._purchase_times["old_batch"] = old_time

        # Insert a fresh entry — should trigger pruning
        stamp_tracker.record_purchase("new_batch")

        assert stamp_tracker.get_purchase_time("old_batch") is None
        assert stamp_tracker.get_purchase_time("new_batch") is not None

    def test_recent_entries_not_pruned(self):
        """Entries within the 10-minute window survive pruning."""
        stamp_tracker.record_purchase("recent_batch")
        # Trigger pruning by recording another
        stamp_tracker.record_purchase("another_batch")
        assert stamp_tracker.get_purchase_time("recent_batch") is not None


class TestCalculatePropagationSignals:
    """Unit tests for calculate_propagation_signals logic."""

    def setup_method(self):
        stamp_tracker.clear_tracker()

    @patch("app.services.swarm_api.settings")
    def test_tracked_within_window_propagating(self, mock_settings):
        """Tracked stamp within propagation window → 'propagating'."""
        mock_settings.STAMP_PROPAGATION_SECONDS = 120
        stamp_tracker.record_purchase("batch_new")

        result = calculate_propagation_signals("batch_new", usable=False)

        assert result["propagationStatus"] == "propagating"
        assert result["secondsSincePurchase"] is not None
        assert result["secondsSincePurchase"] >= 0
        assert result["estimatedReadyAt"] is not None

    @patch("app.services.swarm_api.settings")
    def test_tracked_past_window_ready(self, mock_settings):
        """Tracked stamp past propagation window → 'ready'."""
        mock_settings.STAMP_PROPAGATION_SECONDS = 120
        # Insert entry from 3 minutes ago
        old_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=180)
        stamp_tracker._purchase_times["batch_old"] = old_time

        result = calculate_propagation_signals("batch_old", usable=False)

        assert result["propagationStatus"] == "ready"
        assert result["secondsSincePurchase"] >= 180
        assert result["estimatedReadyAt"] is not None

    @patch("app.services.swarm_api.settings")
    def test_untracked_usable_true_ready(self, mock_settings):
        """Untracked stamp with usable=True → 'ready' (covers pool stamps)."""
        mock_settings.STAMP_PROPAGATION_SECONDS = 120

        result = calculate_propagation_signals("unknown_batch", usable=True)

        assert result["propagationStatus"] == "ready"
        assert result["secondsSincePurchase"] is None
        assert result["estimatedReadyAt"] is None

    @patch("app.services.swarm_api.settings")
    def test_untracked_usable_false_unknown(self, mock_settings):
        """Untracked stamp with usable=False → 'unknown'."""
        mock_settings.STAMP_PROPAGATION_SECONDS = 120

        result = calculate_propagation_signals("external_batch", usable=False)

        assert result["propagationStatus"] == "unknown"
        assert result["secondsSincePurchase"] is None
        assert result["estimatedReadyAt"] is None

    @patch("app.services.swarm_api.settings")
    def test_untracked_usable_none_unknown(self, mock_settings):
        """Untracked stamp with usable=None → 'unknown'."""
        mock_settings.STAMP_PROPAGATION_SECONDS = 120

        result = calculate_propagation_signals("no_info_batch", usable=None)

        assert result["propagationStatus"] == "unknown"

    @patch("app.services.swarm_api.settings")
    def test_usable_true_always_ready_even_if_tracked(self, mock_settings):
        """usable=True always → 'ready' even if tracker says recently purchased."""
        mock_settings.STAMP_PROPAGATION_SECONDS = 120
        stamp_tracker.record_purchase("batch_just_bought")

        result = calculate_propagation_signals("batch_just_bought", usable=True)

        assert result["propagationStatus"] == "ready"
        # Timing fields still populated for tracked stamps
        assert result["secondsSincePurchase"] is not None
        assert result["estimatedReadyAt"] is not None

    @patch("app.services.swarm_api.settings")
    def test_estimated_ready_at_is_iso_format(self, mock_settings):
        """estimatedReadyAt should be a valid ISO 8601 string."""
        mock_settings.STAMP_PROPAGATION_SECONDS = 120
        stamp_tracker.record_purchase("batch_iso")

        result = calculate_propagation_signals("batch_iso", usable=False)

        # Should parse as ISO 8601
        parsed = datetime.datetime.fromisoformat(result["estimatedReadyAt"])
        assert parsed.tzinfo is not None


class TestPropagationIntegration:
    """Integration tests for propagation fields in API responses."""

    def setup_method(self):
        stamp_tracker.clear_tracker()

    def _make_stamp_data(self, batch_id, usable=True, **overrides):
        """Helper to create complete stamp data dict."""
        data = {
            "batchID": batch_id,
            "amount": "1000000000",
            "blockNumber": 12345,
            "owner": "0xabcdef",
            "immutableFlag": False,
            "depth": 20,
            "bucketDepth": 16,
            "batchTTL": 86400,
            "utilization": 5,
            "utilizationPercent": 31.25,
            "utilizationStatus": "ok",
            "utilizationWarning": None,
            "usable": usable,
            "label": None,
            "secondsSincePurchase": None,
            "estimatedReadyAt": None,
            "propagationStatus": "ready" if usable else "unknown",
            "expectedExpiration": "2026-03-17-12-00",
            "local": True,
        }
        data.update(overrides)
        return data

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_get_stamp_includes_propagation_fields(self, mock_get_stamps):
        """GET /stamps/{id} includes propagation fields."""
        mock_get_stamps.return_value = [
            self._make_stamp_data(VALID_STAMP_ID, propagationStatus="ready")
        ]

        response = client.get(f"/api/v1/stamps/{VALID_STAMP_ID}")

        assert response.status_code == 200
        data = response.json()
        assert "secondsSincePurchase" in data
        assert "estimatedReadyAt" in data
        assert "propagationStatus" in data
        assert data["propagationStatus"] == "ready"

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_list_stamps_includes_propagation_fields(self, mock_get_stamps):
        """GET /stamps/ list includes propagation fields on each stamp."""
        mock_get_stamps.return_value = [
            self._make_stamp_data(VALID_STAMP_ID),
            self._make_stamp_data(VALID_STAMP_ID_B, usable=False, propagationStatus="unknown"),
        ]

        response = client.get("/api/v1/stamps/")

        assert response.status_code == 200
        stamps = response.json()["stamps"]
        assert len(stamps) == 2

        for stamp in stamps:
            assert "propagationStatus" in stamp
            assert "secondsSincePurchase" in stamp
            assert "estimatedReadyAt" in stamp

        assert stamps[0]["propagationStatus"] == "ready"
        assert stamps[1]["propagationStatus"] == "unknown"

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_propagation_fields_null_for_untracked(self, mock_get_stamps):
        """Propagation timing fields are null for untracked stamps."""
        mock_get_stamps.return_value = [
            self._make_stamp_data(VALID_STAMP_ID, propagationStatus="unknown")
        ]

        response = client.get(f"/api/v1/stamps/{VALID_STAMP_ID}")

        assert response.status_code == 200
        data = response.json()
        assert data["secondsSincePurchase"] is None
        assert data["estimatedReadyAt"] is None

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_propagating_stamp_shows_timing(self, mock_get_stamps):
        """A propagating stamp shows timing fields."""
        now = datetime.datetime.now(datetime.timezone.utc)
        estimated = (now + datetime.timedelta(seconds=80)).isoformat()
        mock_get_stamps.return_value = [
            self._make_stamp_data(
                VALID_STAMP_ID,
                usable=False,
                secondsSincePurchase=40,
                estimatedReadyAt=estimated,
                propagationStatus="propagating",
            )
        ]

        response = client.get(f"/api/v1/stamps/{VALID_STAMP_ID}")

        assert response.status_code == 200
        data = response.json()
        assert data["propagationStatus"] == "propagating"
        assert data["secondsSincePurchase"] == 40
        assert data["estimatedReadyAt"] is not None

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_health_check_includes_propagation(self, mock_get_stamps):
        """GET /stamps/{id}/check includes propagation fields in status."""
        mock_get_stamps.return_value = [
            self._make_stamp_data(
                VALID_STAMP_ID,
                usable=False,
                secondsSincePurchase=30,
                estimatedReadyAt="2026-03-16T12:02:00+00:00",
                propagationStatus="propagating",
            )
        ]

        response = client.get(f"/api/v1/stamps/{VALID_STAMP_ID}/check")

        assert response.status_code == 200
        status = response.json()["status"]
        assert status["propagationStatus"] == "propagating"
        assert status["secondsSincePurchase"] == 30
        assert status["estimatedReadyAt"] == "2026-03-16T12:02:00+00:00"

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_health_check_ready_stamp(self, mock_get_stamps):
        """Health check for a ready stamp shows propagationStatus='ready'."""
        mock_get_stamps.return_value = [
            self._make_stamp_data(VALID_STAMP_ID, propagationStatus="ready")
        ]

        response = client.get(f"/api/v1/stamps/{VALID_STAMP_ID}/check")

        assert response.status_code == 200
        status = response.json()["status"]
        assert status["propagationStatus"] == "ready"

    @patch("app.services.swarm_api.check_sufficient_funds")
    @patch("app.services.swarm_api.purchase_postage_stamp")
    def test_purchase_records_in_tracker(self, mock_purchase, mock_funds):
        """POST /stamps/ records the purchase in stamp_tracker."""
        stamp_tracker.clear_tracker()
        mock_purchase.return_value = "new_batch_abc123"
        mock_funds.return_value = {
            "sufficient": True,
            "wallet_balance_bzz": 10.0,
            "required_bzz": 0.1,
            "shortfall_bzz": 0.0,
        }

        response = client.post(
            "/api/v1/stamps/", json={"amount": 8000000000, "depth": 17}
        )

        assert response.status_code == 201
        # Verify tracker recorded the purchase
        purchase_time = stamp_tracker.get_purchase_time("new_batch_abc123")
        assert purchase_time is not None

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_pool_stamps_show_ready(self, mock_get_stamps):
        """Pool stamps (usable=True, not tracked) show propagationStatus='ready'."""
        mock_get_stamps.return_value = [
            self._make_stamp_data(
                VALID_STAMP_ID,
                usable=True,
                propagationStatus="ready",
            )
        ]

        response = client.get(f"/api/v1/stamps/{VALID_STAMP_ID}")

        assert response.status_code == 200
        data = response.json()
        assert data["propagationStatus"] == "ready"
        assert data["secondsSincePurchase"] is None
