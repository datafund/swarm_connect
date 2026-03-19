# tests/test_stamp_list_filtering.py
"""Tests for GET /api/v1/stamps/ ownership filtering and accessMode field."""
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _make_stamp(batch_id, local=True, access_mode=None):
    """Helper to create a processed stamp dict."""
    return {
        "batchID": batch_id,
        "amount": "1000000000",
        "blockNumber": 12345,
        "owner": "0x1234567890abcdef",
        "immutableFlag": False,
        "depth": 20,
        "bucketDepth": 16,
        "batchTTL": 86400,
        "utilization": None,
        "utilizationPercent": None,
        "utilizationStatus": None,
        "utilizationWarning": None,
        "usable": True,
        "label": None,
        "secondsSincePurchase": None,
        "estimatedReadyAt": None,
        "propagationStatus": "ready",
        "accessMode": access_mode,
        "expectedExpiration": "2026-12-01-15-30",
        "local": local,
    }


LOCAL_STAMP = _make_stamp("a" * 64, local=True, access_mode=None)
REMOTE_STAMP = _make_stamp("b" * 64, local=False, access_mode=None)
OWNED_STAMP = _make_stamp("c" * 64, local=True, access_mode="owned")
SHARED_STAMP = _make_stamp("d" * 64, local=True, access_mode="shared")
REMOTE_OWNED = _make_stamp("e" * 64, local=False, access_mode="owned")

ALL_STAMPS = [LOCAL_STAMP, REMOTE_STAMP, OWNED_STAMP, SHARED_STAMP, REMOTE_OWNED]


class TestDefaultBehavior:
    """Default (no params) returns only local stamps."""

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_default_returns_local_only(self, mock_get):
        mock_get.return_value = ALL_STAMPS

        response = client.get("/api/v1/stamps/")
        assert response.status_code == 200
        data = response.json()

        batch_ids = {s["batchID"] for s in data["stamps"]}
        # LOCAL_STAMP, OWNED_STAMP, SHARED_STAMP are local=True
        assert "a" * 64 in batch_ids
        assert "c" * 64 in batch_ids
        assert "d" * 64 in batch_ids
        # REMOTE_STAMP and REMOTE_OWNED are local=False
        assert "b" * 64 not in batch_ids
        assert "e" * 64 not in batch_ids
        assert data["total_count"] == 3

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_default_empty_when_no_local(self, mock_get):
        mock_get.return_value = [REMOTE_STAMP]

        response = client.get("/api/v1/stamps/")
        assert response.status_code == 200
        data = response.json()
        assert data["stamps"] == []
        assert data["total_count"] == 0


class TestGlobalView:
    """?global=true returns all stamps (old behavior)."""

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_global_returns_all(self, mock_get):
        mock_get.return_value = ALL_STAMPS

        response = client.get("/api/v1/stamps/?global=true")
        assert response.status_code == 200
        data = response.json()
        assert data["total_count"] == len(ALL_STAMPS)

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_global_false_acts_as_default(self, mock_get):
        mock_get.return_value = ALL_STAMPS

        response = client.get("/api/v1/stamps/?global=false")
        assert response.status_code == 200
        data = response.json()
        # global=false is falsy, falls to default (local only)
        batch_ids = {s["batchID"] for s in data["stamps"]}
        assert "b" * 64 not in batch_ids


class TestWalletFilteringX402Enabled:
    """?wallet=0x... with x402 enabled filters to accessible stamps."""

    WALLET = "0xABCDEF1234567890ABCDEF1234567890ABCDEF12"

    @patch("app.api.endpoints.stamps.settings")
    @patch("app.services.swarm_api.get_all_stamps_processed")
    @patch("app.api.endpoints.stamps.stamp_ownership_manager")
    def test_wallet_shows_owned_shared_untracked(self, mock_ownership, mock_get, mock_settings):
        mock_settings.X402_ENABLED = True
        mock_get.return_value = ALL_STAMPS

        # c*64 is owned by WALLET, e*64 is owned by someone else
        def get_info(batch_id):
            if batch_id == "c" * 64:
                return {"owner": self.WALLET, "mode": "paid"}
            if batch_id == "e" * 64:
                return {"owner": "0xOTHER", "mode": "paid"}
            if batch_id == "d" * 64:
                return {"owner": "shared", "mode": "free"}
            return None
        mock_ownership.get_stamp_info.side_effect = get_info

        response = client.get(f"/api/v1/stamps/?wallet={self.WALLET}")
        assert response.status_code == 200
        data = response.json()
        batch_ids = {s["batchID"] for s in data["stamps"]}

        # LOCAL_STAMP: accessMode=None, local=True → included (untracked local)
        assert "a" * 64 in batch_ids
        # SHARED_STAMP: accessMode="shared" → included
        assert "d" * 64 in batch_ids
        # OWNED_STAMP: owned by this wallet → included
        assert "c" * 64 in batch_ids
        # REMOTE_STAMP: accessMode=None, local=False → excluded
        assert "b" * 64 not in batch_ids
        # REMOTE_OWNED: owned by someone else → excluded
        assert "e" * 64 not in batch_ids

    @patch("app.api.endpoints.stamps.settings")
    @patch("app.services.swarm_api.get_all_stamps_processed")
    @patch("app.api.endpoints.stamps.stamp_ownership_manager")
    def test_wallet_excludes_other_wallets_stamps(self, mock_ownership, mock_get, mock_settings):
        mock_settings.X402_ENABLED = True
        owned_by_other = _make_stamp("f" * 64, local=True, access_mode="owned")
        mock_get.return_value = [owned_by_other]

        mock_ownership.get_stamp_info.return_value = {"owner": "0xOTHER", "mode": "paid"}

        response = client.get(f"/api/v1/stamps/?wallet={self.WALLET}")
        assert response.status_code == 200
        data = response.json()
        assert data["total_count"] == 0


class TestExclusiveFiltering:
    """?wallet=...&exclusive=true returns only stamps paid for by that wallet."""

    WALLET = "0xABCDEF1234567890ABCDEF1234567890ABCDEF12"

    @patch("app.api.endpoints.stamps.settings")
    @patch("app.services.swarm_api.get_all_stamps_processed")
    @patch("app.api.endpoints.stamps.stamp_ownership_manager")
    def test_exclusive_returns_only_owned(self, mock_ownership, mock_get, mock_settings):
        mock_settings.X402_ENABLED = True
        mock_get.return_value = ALL_STAMPS

        def get_info(batch_id):
            if batch_id == "c" * 64:
                return {"owner": self.WALLET, "mode": "paid"}
            if batch_id == "d" * 64:
                return {"owner": "shared", "mode": "free"}
            return None
        mock_ownership.get_stamp_info.side_effect = get_info

        response = client.get(f"/api/v1/stamps/?wallet={self.WALLET}&exclusive=true")
        assert response.status_code == 200
        data = response.json()
        batch_ids = {s["batchID"] for s in data["stamps"]}

        # Only the stamp owned by this wallet
        assert "c" * 64 in batch_ids
        # Shared stamp excluded
        assert "d" * 64 not in batch_ids
        # Untracked local stamp excluded
        assert "a" * 64 not in batch_ids
        assert data["total_count"] == 1

    @patch("app.api.endpoints.stamps.settings")
    @patch("app.services.swarm_api.get_all_stamps_processed")
    @patch("app.api.endpoints.stamps.stamp_ownership_manager")
    def test_exclusive_empty_when_no_owned(self, mock_ownership, mock_get, mock_settings):
        mock_settings.X402_ENABLED = True
        mock_get.return_value = [LOCAL_STAMP, SHARED_STAMP]
        mock_ownership.get_stamp_info.return_value = None

        response = client.get(f"/api/v1/stamps/?wallet={self.WALLET}&exclusive=true")
        assert response.status_code == 200
        data = response.json()
        assert data["total_count"] == 0

    @patch("app.api.endpoints.stamps.settings")
    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_exclusive_without_wallet_falls_to_default(self, mock_get, mock_settings):
        """exclusive=true without wallet has no effect — falls to local-only default."""
        mock_settings.X402_ENABLED = True
        mock_get.return_value = ALL_STAMPS

        response = client.get("/api/v1/stamps/?exclusive=true")
        assert response.status_code == 200
        data = response.json()
        # No wallet provided, so falls to default local-only
        batch_ids = {s["batchID"] for s in data["stamps"]}
        assert "b" * 64 not in batch_ids  # remote excluded
        assert "a" * 64 in batch_ids  # local included


class TestWalletFilteringX402Disabled:
    """?wallet param is ignored when x402 is disabled — falls back to local-only."""

    @patch("app.api.endpoints.stamps.settings")
    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_wallet_ignored_when_x402_disabled(self, mock_get, mock_settings):
        mock_settings.X402_ENABLED = False
        mock_get.return_value = ALL_STAMPS

        response = client.get("/api/v1/stamps/?wallet=0xABC")
        assert response.status_code == 200
        data = response.json()
        # Falls back to default local-only filtering
        batch_ids = {s["batchID"] for s in data["stamps"]}
        assert "b" * 64 not in batch_ids  # remote
        assert "a" * 64 in batch_ids  # local


class TestAccessModeField:
    """accessMode field is correctly populated in responses."""

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_access_mode_values(self, mock_get):
        mock_get.return_value = [LOCAL_STAMP, OWNED_STAMP, SHARED_STAMP]

        response = client.get("/api/v1/stamps/")
        assert response.status_code == 200
        data = response.json()

        modes = {s["batchID"]: s["accessMode"] for s in data["stamps"]}
        assert modes["a" * 64] is None
        assert modes["c" * 64] == "owned"
        assert modes["d" * 64] == "shared"

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_access_mode_in_stamp_detail(self, mock_get):
        """accessMode appears in GET /stamps/{id} response too."""
        mock_get.return_value = [OWNED_STAMP]

        response = client.get(f"/api/v1/stamps/{'c' * 64}")
        assert response.status_code == 200
        data = response.json()
        assert data["accessMode"] == "owned"


class TestParameterPrecedence:
    """When both global and wallet are provided, global wins."""

    @patch("app.api.endpoints.stamps.settings")
    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_global_true_overrides_wallet(self, mock_get, mock_settings):
        mock_settings.X402_ENABLED = True
        mock_get.return_value = ALL_STAMPS

        response = client.get("/api/v1/stamps/?global=true&wallet=0xABC")
        assert response.status_code == 200
        data = response.json()
        # global=true wins — all stamps returned
        assert data["total_count"] == len(ALL_STAMPS)


class TestIsOwnedByEdgeCases:
    """Edge cases for the _is_owned_by helper."""

    @patch("app.api.endpoints.stamps.settings")
    @patch("app.services.swarm_api.get_all_stamps_processed")
    @patch("app.api.endpoints.stamps.stamp_ownership_manager")
    def test_free_mode_stamp_not_treated_as_owned(self, mock_ownership, mock_get, mock_settings):
        """A stamp with mode=free should not match _is_owned_by even if owner field matches."""
        mock_settings.X402_ENABLED = True
        # Stamp has accessMode="shared" (from the processed data),
        # but let's test a stamp with accessMode="owned" that actually has mode=free in registry
        stamp = _make_stamp("f" * 64, local=True, access_mode="owned")
        mock_get.return_value = [stamp]

        # Registry says mode=free (not paid), so _is_owned_by should return False
        mock_ownership.get_stamp_info.return_value = {"owner": "0xWALLET", "mode": "free"}

        response = client.get("/api/v1/stamps/?wallet=0xWALLET")
        data = response.json()
        batch_ids = {s["batchID"] for s in data["stamps"]}
        # accessMode="owned" but registry mode=free → _is_owned_by returns False
        # accessMode is not "shared" and not None → excluded
        assert "f" * 64 not in batch_ids


class TestAccessModeServiceLayer:
    """Test accessMode population at the service layer."""

    @pytest.mark.asyncio
    @patch("app.services.swarm_api.stamp_ownership_manager")
    @patch("app.services.swarm_api.get_local_stamps")
    @patch("app.services.swarm_api.get_all_stamps")
    async def test_access_mode_paid_becomes_owned(self, mock_global, mock_local, mock_ownership):
        mock_global.return_value = [{
            "batchID": "a" * 64, "amount": "1000", "depth": 20,
            "bucketDepth": 16, "batchTTL": 86400,
        }]
        mock_local.return_value = []
        mock_ownership.get_stamp_info.return_value = {"mode": "paid", "owner": "0xABC"}

        from app.services.swarm_api import get_all_stamps_processed
        result = await get_all_stamps_processed()
        assert result[0]["accessMode"] == "owned"

    @pytest.mark.asyncio
    @patch("app.services.swarm_api.stamp_ownership_manager")
    @patch("app.services.swarm_api.get_local_stamps")
    @patch("app.services.swarm_api.get_all_stamps")
    async def test_access_mode_free_becomes_shared(self, mock_global, mock_local, mock_ownership):
        mock_global.return_value = [{
            "batchID": "b" * 64, "amount": "1000", "depth": 20,
            "bucketDepth": 16, "batchTTL": 86400,
        }]
        mock_local.return_value = []
        mock_ownership.get_stamp_info.return_value = {"mode": "free", "owner": "shared"}

        from app.services.swarm_api import get_all_stamps_processed
        result = await get_all_stamps_processed()
        assert result[0]["accessMode"] == "shared"

    @pytest.mark.asyncio
    @patch("app.services.swarm_api.stamp_ownership_manager")
    @patch("app.services.swarm_api.get_local_stamps")
    @patch("app.services.swarm_api.get_all_stamps")
    async def test_access_mode_untracked_is_none(self, mock_global, mock_local, mock_ownership):
        mock_global.return_value = [{
            "batchID": "c" * 64, "amount": "1000", "depth": 20,
            "bucketDepth": 16, "batchTTL": 86400,
        }]
        mock_local.return_value = []
        mock_ownership.get_stamp_info.return_value = None

        from app.services.swarm_api import get_all_stamps_processed
        result = await get_all_stamps_processed()
        assert result[0]["accessMode"] is None


class TestTotalCountAccuracy:
    """total_count reflects the filtered result, not all stamps."""

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_total_count_matches_filtered(self, mock_get):
        mock_get.return_value = ALL_STAMPS

        response = client.get("/api/v1/stamps/")
        data = response.json()
        assert data["total_count"] == len(data["stamps"])

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_total_count_global(self, mock_get):
        mock_get.return_value = ALL_STAMPS

        response = client.get("/api/v1/stamps/?global=true")
        data = response.json()
        assert data["total_count"] == len(ALL_STAMPS)
