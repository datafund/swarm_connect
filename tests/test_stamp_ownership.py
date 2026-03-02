# tests/test_stamp_ownership.py
"""
Tests for stamp ownership tracking and enforcement.
"""
import json
import pytest
from unittest.mock import patch, MagicMock, PropertyMock
from fastapi.testclient import TestClient

from app.services.stamp_ownership import StampOwnershipManager


class TestOwnershipRegistration:
    """Test registering stamp ownership."""

    @pytest.fixture
    def state_file(self, tmp_path):
        return str(tmp_path / "stamp_owners.json")

    @pytest.fixture
    def manager(self, state_file):
        return StampOwnershipManager(state_file=state_file)

    def test_register_paid_stamp(self, manager):
        """Wallet address stored as owner for paid stamps."""
        manager.register_stamp("batch_001", owner="0xABCDEF1234567890", mode="paid", source="pool_acquire")
        info = manager.get_stamp_info("batch_001")
        assert info is not None
        assert info["owner"] == "0xABCDEF1234567890"
        assert info["mode"] == "paid"
        assert info["source"] == "pool_acquire"

    def test_register_shared_stamp(self, manager):
        """Owner stored as 'shared' for free tier stamps."""
        manager.register_stamp("batch_002", owner="shared", mode="free", source="pool_acquire")
        info = manager.get_stamp_info("batch_002")
        assert info is not None
        assert info["owner"] == "shared"
        assert info["mode"] == "free"


class TestPaidStampAccess:
    """Test access control for paid (exclusive) stamps."""

    @pytest.fixture
    def state_file(self, tmp_path):
        return str(tmp_path / "stamp_owners.json")

    @pytest.fixture
    def manager(self, state_file):
        mgr = StampOwnershipManager(state_file=state_file)
        mgr.register_stamp("paid_stamp", owner="0xOwnerWallet123", mode="paid", source="pool_acquire")
        return mgr

    def test_paid_stamp_access_by_owner(self, manager):
        """Owner wallet can use their paid stamp."""
        with patch('app.services.stamp_ownership.settings') as mock_settings:
            mock_settings.X402_ENABLED = True
            allowed, reason = manager.check_access("paid_stamp", "0xOwnerWallet123", "paid")
            assert allowed is True
            assert "owner match" in reason

    def test_paid_stamp_denied_different_wallet(self, manager):
        """Different wallet gets denied from using a paid stamp."""
        with patch('app.services.stamp_ownership.settings') as mock_settings:
            mock_settings.X402_ENABLED = True
            allowed, reason = manager.check_access("paid_stamp", "0xOtherWallet999", "paid")
            assert allowed is False
            assert "not accessible" in reason

    def test_paid_stamp_denied_free_tier_user(self, manager):
        """Free tier user can't use a paid stamp."""
        with patch('app.services.stamp_ownership.settings') as mock_settings:
            mock_settings.X402_ENABLED = True
            allowed, reason = manager.check_access("paid_stamp", None, "free-tier")
            assert allowed is False

    def test_paid_stamp_denied_no_x402(self, manager):
        """Unauthenticated user can't use a paid stamp."""
        with patch('app.services.stamp_ownership.settings') as mock_settings:
            mock_settings.X402_ENABLED = True
            allowed, reason = manager.check_access("paid_stamp", None, None)
            assert allowed is False


class TestSharedStampAccess:
    """Test access control for shared (free tier) stamps."""

    @pytest.fixture
    def state_file(self, tmp_path):
        return str(tmp_path / "stamp_owners.json")

    @pytest.fixture
    def manager(self, state_file):
        mgr = StampOwnershipManager(state_file=state_file)
        mgr.register_stamp("shared_stamp", owner="shared", mode="free", source="pool_acquire")
        return mgr

    def test_shared_stamp_access_by_anyone(self, manager):
        """Any wallet can use a shared stamp."""
        with patch('app.services.stamp_ownership.settings') as mock_settings:
            mock_settings.X402_ENABLED = True
            allowed, reason = manager.check_access("shared_stamp", "0xAnyWallet", "paid")
            assert allowed is True
            assert "shared" in reason

    def test_shared_stamp_access_by_free_tier(self, manager):
        """Free tier user can use a shared stamp."""
        with patch('app.services.stamp_ownership.settings') as mock_settings:
            mock_settings.X402_ENABLED = True
            allowed, reason = manager.check_access("shared_stamp", None, "free-tier")
            assert allowed is True

    def test_shared_stamp_access_no_x402(self, manager):
        """Unauthenticated user can use a shared stamp."""
        with patch('app.services.stamp_ownership.settings') as mock_settings:
            mock_settings.X402_ENABLED = True
            allowed, reason = manager.check_access("shared_stamp", None, None)
            assert allowed is True


class TestBackwardCompatibility:
    """Test backward compatibility for untracked stamps."""

    @pytest.fixture
    def state_file(self, tmp_path):
        return str(tmp_path / "stamp_owners.json")

    @pytest.fixture
    def manager(self, state_file):
        return StampOwnershipManager(state_file=state_file)

    def test_untracked_stamp_allowed(self, manager):
        """Stamp not in registry is allowed (backward compat for pre-existing stamps)."""
        with patch('app.services.stamp_ownership.settings') as mock_settings:
            mock_settings.X402_ENABLED = True
            allowed, reason = manager.check_access("unknown_stamp", "0xAnyWallet", "paid")
            assert allowed is True
            assert "backward compatibility" in reason

    def test_x402_disabled_skips_enforcement(self, manager):
        """No ownership checks when x402 is off."""
        manager.register_stamp("paid_stamp", owner="0xOwner", mode="paid", source="test")
        with patch('app.services.stamp_ownership.settings') as mock_settings:
            mock_settings.X402_ENABLED = False
            # Even a different wallet should be allowed when x402 is disabled
            allowed, reason = manager.check_access("paid_stamp", "0xDifferent", "paid")
            assert allowed is True
            assert "x402 disabled" in reason


class TestOwnershipPersistence:
    """Test ownership state persistence."""

    @pytest.fixture
    def state_file(self, tmp_path):
        return str(tmp_path / "stamp_owners.json")

    def test_ownership_save_load(self, state_file):
        """Persist and reload ownership data."""
        mgr1 = StampOwnershipManager(state_file=state_file)
        mgr1.register_stamp("batch_a", owner="0xOwnerA", mode="paid", source="pool")
        mgr1.register_stamp("batch_b", owner="shared", mode="free", source="pool")

        # New manager loads from same file
        mgr2 = StampOwnershipManager(state_file=state_file)
        mgr2.load_on_startup()

        info_a = mgr2.get_stamp_info("batch_a")
        assert info_a is not None
        assert info_a["owner"] == "0xOwnerA"

        info_b = mgr2.get_stamp_info("batch_b")
        assert info_b is not None
        assert info_b["owner"] == "shared"

    def test_ownership_corrupt_file_recovery(self, state_file):
        """Corrupt JSON -> start fresh."""
        with open(state_file, 'w') as f:
            f.write("{{not valid json")

        mgr = StampOwnershipManager(state_file=state_file)
        mgr.load_on_startup()

        # Should be empty after corrupt file
        assert mgr.get_stamp_info("anything") is None

    def test_cleanup_expired_stamps(self, state_file):
        """Expired stamps removed from ownership."""
        mgr = StampOwnershipManager(state_file=state_file)
        mgr.register_stamp("valid", owner="0xOwner", mode="paid", source="pool")
        mgr.register_stamp("expired", owner="0xOwner2", mode="paid", source="pool")

        # Cleanup: only "valid" is still on the Bee node
        mgr.cleanup_expired(valid_batch_ids={"valid"})

        assert mgr.get_stamp_info("valid") is not None
        assert mgr.get_stamp_info("expired") is None

        # Verify state file was updated
        with open(state_file, 'r') as f:
            saved = json.load(f)
        assert "valid" in saved
        assert "expired" not in saved

    def test_remove_stamp(self, state_file):
        """Remove a single stamp from registry."""
        mgr = StampOwnershipManager(state_file=state_file)
        mgr.register_stamp("to_remove", owner="0xOwner", mode="paid", source="pool")
        assert mgr.get_stamp_info("to_remove") is not None

        mgr.remove_stamp("to_remove")
        assert mgr.get_stamp_info("to_remove") is None


class TestOwnershipIntegration:
    """Integration tests for upload enforcement and pool/purchase registration."""

    @pytest.fixture
    def client(self):
        from app.main import app
        return TestClient(app)

    def test_upload_with_untracked_stamp_succeeds(self, client):
        """Upload with stamp not in registry succeeds (backward compat)."""
        stamp_id = "a" * 64
        with patch('app.api.endpoints.data.settings') as mock_settings:
            mock_settings.X402_ENABLED = True
            mock_settings.MAX_UPLOAD_SIZE_MB = 10
            with patch('app.api.endpoints.data.stamp_ownership_manager') as mock_ownership:
                mock_ownership.check_access.return_value = (True, "backward compat")
                with patch('app.api.endpoints.data.upload_data_to_swarm', return_value="ref123"):
                    response = client.post(
                        f"/api/v1/data/?stamp_id={stamp_id}",
                        files={"file": ("test.json", b'{"data": "test"}', "application/json")}
                    )
                    assert response.status_code == 200

    def test_upload_with_own_paid_stamp_succeeds(self, client):
        """Owner can upload with their paid stamp."""
        stamp_id = "b" * 64
        with patch('app.api.endpoints.data.settings') as mock_settings:
            mock_settings.X402_ENABLED = True
            mock_settings.MAX_UPLOAD_SIZE_MB = 10
            with patch('app.api.endpoints.data.stamp_ownership_manager') as mock_ownership:
                mock_ownership.check_access.return_value = (True, "owner match")
                with patch('app.api.endpoints.data.upload_data_to_swarm', return_value="ref123"):
                    response = client.post(
                        f"/api/v1/data/?stamp_id={stamp_id}",
                        files={"file": ("test.json", b'{"data": "test"}', "application/json")}
                    )
                    assert response.status_code == 200

    def test_upload_with_others_paid_stamp_denied(self, client):
        """Upload with someone else's paid stamp is denied."""
        stamp_id = "c" * 64
        with patch('app.api.endpoints.data.settings') as mock_settings:
            mock_settings.X402_ENABLED = True
            mock_settings.MAX_UPLOAD_SIZE_MB = 10
            with patch('app.api.endpoints.data.stamp_ownership_manager') as mock_ownership:
                mock_ownership.check_access.return_value = (False, "stamp owned by 0xOther...")
                response = client.post(
                    f"/api/v1/data/?stamp_id={stamp_id}",
                    files={"file": ("test.json", b'{"data": "test"}', "application/json")}
                )
                assert response.status_code == 403
                detail = response.json()["detail"]
                assert detail["code"] == "STAMP_OWNERSHIP_DENIED"

    def test_upload_with_shared_stamp_succeeds(self, client):
        """Anyone can upload with a shared stamp."""
        stamp_id = "d" * 64
        with patch('app.api.endpoints.data.settings') as mock_settings:
            mock_settings.X402_ENABLED = True
            mock_settings.MAX_UPLOAD_SIZE_MB = 10
            with patch('app.api.endpoints.data.stamp_ownership_manager') as mock_ownership:
                mock_ownership.check_access.return_value = (True, "shared stamp")
                with patch('app.api.endpoints.data.upload_data_to_swarm', return_value="ref123"):
                    response = client.post(
                        f"/api/v1/data/?stamp_id={stamp_id}",
                        files={"file": ("test.json", b'{"data": "test"}', "application/json")}
                    )
                    assert response.status_code == 200

    def test_upload_x402_disabled_no_enforcement(self, client):
        """No ownership checks when x402 is off."""
        stamp_id = "e" * 64
        with patch('app.api.endpoints.data.settings') as mock_settings:
            mock_settings.X402_ENABLED = False
            mock_settings.MAX_UPLOAD_SIZE_MB = 10
            with patch('app.api.endpoints.data.upload_data_to_swarm', return_value="ref123"):
                response = client.post(
                    f"/api/v1/data/?stamp_id={stamp_id}",
                    files={"file": ("test.json", b'{"data": "test"}', "application/json")}
                )
                assert response.status_code == 200

    def test_pool_acquire_paid_registers_exclusive(self, client):
        """Acquire via x402 -> stamp exclusive to payer."""
        with patch('app.api.endpoints.pool.settings') as mock_settings:
            mock_settings.STAMP_POOL_ENABLED = True

            mock_stamp = MagicMock()
            mock_stamp.batch_id = "poolstamp_001"
            mock_stamp.depth = 17

            mock_released = MagicMock()
            mock_released.batch_id = "poolstamp_001"
            mock_released.depth = 17

            with patch('app.api.endpoints.pool.stamp_pool_manager') as mock_pool:
                mock_pool.get_available_stamp.return_value = mock_stamp
                mock_pool.release_stamp.return_value = mock_released
                mock_pool.trigger_replenishment_if_needed.return_value = False

                with patch('app.api.endpoints.pool.stamp_ownership_manager') as mock_ownership:
                    # Simulate x402 paid request
                    response = client.post(
                        "/api/v1/pool/acquire",
                        json={"size": "small"},
                        headers={"X-Payment-Mode": "paid"}
                    )
                    assert response.status_code == 200

                    # Verify register_stamp was called
                    mock_ownership.register_stamp.assert_called_once()
                    call_kwargs = mock_ownership.register_stamp.call_args
                    assert call_kwargs[1]["batch_id"] == "poolstamp_001" or call_kwargs[0][0] == "poolstamp_001"

    def test_pool_acquire_free_registers_shared(self, client):
        """Acquire on free tier -> stamp is shared."""
        with patch('app.api.endpoints.pool.settings') as mock_settings:
            mock_settings.STAMP_POOL_ENABLED = True

            mock_stamp = MagicMock()
            mock_stamp.batch_id = "poolstamp_002"
            mock_stamp.depth = 17

            mock_released = MagicMock()
            mock_released.batch_id = "poolstamp_002"
            mock_released.depth = 17

            with patch('app.api.endpoints.pool.stamp_pool_manager') as mock_pool:
                mock_pool.get_available_stamp.return_value = mock_stamp
                mock_pool.release_stamp.return_value = mock_released
                mock_pool.trigger_replenishment_if_needed.return_value = False

                with patch('app.api.endpoints.pool.stamp_ownership_manager') as mock_ownership:
                    response = client.post(
                        "/api/v1/pool/acquire",
                        json={"size": "small"}
                    )
                    assert response.status_code == 200

                    # Should register as shared (no x402_payer on request)
                    mock_ownership.register_stamp.assert_called_once()
                    call_args = mock_ownership.register_stamp.call_args
                    # Check it's registered as shared
                    if call_args[1]:
                        assert call_args[1].get("owner") == "shared" or call_args[1].get("mode") == "free"
                    else:
                        # Positional args: batch_id, owner, mode, source
                        assert call_args[0][1] == "shared"

    def test_direct_purchase_registers_stamp(self, client):
        """Direct purchase via stamps endpoint registers ownership."""
        with patch('app.api.endpoints.stamps.swarm_api') as mock_swarm:
            mock_swarm.get_chainstate.return_value = {"currentPrice": 24000}
            mock_swarm.calculate_stamp_amount.return_value = 100000
            mock_swarm.calculate_stamp_total_cost.return_value = 1000000
            mock_swarm.check_sufficient_funds.return_value = {"sufficient": True}
            mock_swarm.purchase_postage_stamp.return_value = "new_batch_id_123"

            with patch('app.api.endpoints.stamps.stamp_ownership_manager') as mock_ownership:
                response = client.post(
                    "/api/v1/stamps/",
                    json={"duration_hours": 24, "depth": 17}
                )
                assert response.status_code == 201

                # Should register as shared (no x402 context)
                mock_ownership.register_stamp.assert_called_once()
                call_args = mock_ownership.register_stamp.call_args
                if call_args[1]:
                    assert call_args[1].get("batch_id") == "new_batch_id_123"
                    assert call_args[1].get("source") == "direct_purchase"
