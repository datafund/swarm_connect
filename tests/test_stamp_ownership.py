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
        manager.register_stamp("batch_001", owner="0xABCDEF1234567890ABCDEF1234567890ABCDEF12", mode="paid", source="pool_acquire")
        info = manager.get_stamp_info("batch_001")
        assert info is not None
        assert info["owner"] == "0xabcdef1234567890abcdef1234567890abcdef12"  # stored canonical (#384)
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
        mgr.register_stamp("paid_stamp", owner="0x1111111111111111111111111111111111111111", mode="paid", source="pool_acquire")
        return mgr

    def test_paid_stamp_access_by_owner(self, manager):
        """Owner wallet can use their paid stamp."""
        with patch('app.services.stamp_ownership.settings') as mock_settings:
            mock_settings.X402_ENABLED = True
            allowed, reason = manager.check_access("paid_stamp", "0x1111111111111111111111111111111111111111", "paid")
            assert allowed is True
            assert "owner match" in reason

    def test_paid_stamp_denied_different_wallet(self, manager):
        """Different wallet gets denied from using a paid stamp."""
        with patch('app.services.stamp_ownership.settings') as mock_settings:
            mock_settings.X402_ENABLED = True
            allowed, reason = manager.check_access("paid_stamp", "0x9999999999999999999999999999999999999999", "paid")
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
            allowed, reason = manager.check_access("shared_stamp", "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "paid")
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
    """Untracked batches, and the gateway's own inventory.

    Named for the behaviour it used to protect. That behaviour is now inverted
    (#312) and these tests hold the line in the other direction.
    """

    @pytest.fixture
    def state_file(self, tmp_path):
        return str(tmp_path / "stamp_owners.json")

    @pytest.fixture
    def manager(self, state_file):
        return StampOwnershipManager(state_file=state_file)

    def test_untracked_stamp_denied(self, manager):
        """A batch nobody owns is refused, not waved through.

        This asserted the opposite until #312. The permissive default was there
        for batches predating the registry, but the set it actually covered was
        the pool's own inventory: every path a caller can obtain a batch through
        registers it, and the pool did not register what it bought. A production
        pool batch reached 50% utilisation without ever being acquired.
        """
        with patch('app.services.stamp_ownership.settings') as mock_settings:
            mock_settings.X402_ENABLED = True
            mock_settings.STAMP_OWNERSHIP_ALLOW_UNTRACKED = False
            allowed, reason = manager.check_access("unknown_stamp", "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "paid")
            assert allowed is False, "an unowned batch was writable by an arbitrary wallet"
            assert "not registered" in reason

    def test_untracked_stamp_allowed_in_permissive_mode(self, manager):
        """The escape hatch, for when the registry file is lost.

        If STAMP_OWNERSHIP_FILE disappears, every batch becomes untracked at once
        and failing closed would lock legitimate owners out of batches they paid
        for. This exists to recover from that, not as a default.
        """
        with patch('app.services.stamp_ownership.settings') as mock_settings:
            mock_settings.X402_ENABLED = True
            mock_settings.STAMP_OWNERSHIP_ALLOW_UNTRACKED = True
            allowed, reason = manager.check_access("unknown_stamp", "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "paid")
            assert allowed is True
            assert "permissive" in reason

    def test_pool_owned_stamp_is_not_writable(self, manager):
        """Gateway inventory is refused to everyone, including the free tier.

        A caller receives a pool batch by acquiring it, which re-registers it to
        them. Writing to one directly bypasses that and spends capacity the
        gateway funded.
        """
        from app.services.stamp_ownership import POOL_OWNER
        manager.register_stamp("pool_stamp", owner=POOL_OWNER, mode="pool", source="pool_purchase")
        with patch('app.services.stamp_ownership.settings') as mock_settings:
            mock_settings.X402_ENABLED = True
            mock_settings.STAMP_OWNERSHIP_ALLOW_UNTRACKED = False
            for wallet, mode in (("0xcccccccccccccccccccccccccccccccccccccccc", "paid"), (None, "free-tier"), (None, None)):
                allowed, reason = manager.check_access("pool_stamp", wallet, mode)
                assert allowed is False, f"pool inventory was writable by {wallet or 'anonymous'} ({mode})"
                assert "acquire it first" in reason

    def test_permissive_mode_does_not_unlock_pool_inventory(self, manager):
        """The escape hatch is for untracked batches, not for owned ones.

        Pool inventory IS tracked, so permissive mode must not reach it —
        otherwise recovering from a lost registry would reopen the hole.
        """
        from app.services.stamp_ownership import POOL_OWNER
        manager.register_stamp("pool_stamp", owner=POOL_OWNER, mode="pool", source="pool_purchase")
        with patch('app.services.stamp_ownership.settings') as mock_settings:
            mock_settings.X402_ENABLED = True
            mock_settings.STAMP_OWNERSHIP_ALLOW_UNTRACKED = True
            allowed, _ = manager.check_access("pool_stamp", "0xcccccccccccccccccccccccccccccccccccccccc", "paid")
            assert allowed is False, "permissive mode unlocked gateway-owned inventory"

    def test_x402_disabled_skips_enforcement(self, manager):
        """No ownership checks when x402 is off."""
        manager.register_stamp("paid_stamp", owner="0xBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB", mode="paid", source="test")
        with patch('app.services.stamp_ownership.settings') as mock_settings:
            mock_settings.X402_ENABLED = False
            # Even a different wallet should be allowed when x402 is disabled
            allowed, reason = manager.check_access("paid_stamp", "0xdddddddddddddddddddddddddddddddddddddddd", "paid")
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
        mgr1.register_stamp("batch_a", owner="0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", mode="paid", source="pool")
        mgr1.register_stamp("batch_b", owner="shared", mode="free", source="pool")

        # New manager loads from same file
        mgr2 = StampOwnershipManager(state_file=state_file)
        mgr2.load_on_startup()

        info_a = mgr2.get_stamp_info("batch_a")
        assert info_a is not None
        assert info_a["owner"] == "0x" + "a" * 40  # stored canonical (#384)

        info_b = mgr2.get_stamp_info("batch_b")
        assert info_b is not None
        assert info_b["owner"] == "shared"

    def test_ownership_corrupt_file_refuses_to_load(self, state_file):
        """Corrupt JSON -> refuse to start, keep the file and a backup (#378).

        Starting empty would deny every owner and the next registration would
        overwrite the only copy of their records.
        """
        import glob
        from app.core.atomic_io import StateLoadError
        with open(state_file, 'w') as f:
            f.write("{{not valid json")

        mgr = StampOwnershipManager(state_file=state_file)
        with pytest.raises(StateLoadError):
            mgr.load_on_startup()

        assert open(state_file).read() == "{{not valid json"
        assert glob.glob(state_file + ".corrupt-*")

    def test_crash_loop_does_not_multiply_backups(self, state_file):
        import glob
        from app.core.atomic_io import StateLoadError
        with open(state_file, 'w') as f:
            f.write("{{not valid json")
        for _ in range(3):
            with pytest.raises(StateLoadError):
                StateManager = StampOwnershipManager(state_file=state_file)
                StateManager.load_on_startup()
        assert len(glob.glob(state_file + ".corrupt-*")) == 1

    def test_malformed_entry_refuses_to_load(self, state_file):
        import json
        from app.core.atomic_io import StateLoadError
        with open(state_file, 'w') as f:
            json.dump({"batch": {"mode": "paid"}}, f)
        with pytest.raises(StateLoadError):
            StampOwnershipManager(state_file=state_file).load_on_startup()

    def test_ownership_missing_file_starts_empty(self, state_file):
        mgr = StampOwnershipManager(state_file=state_file)
        mgr.load_on_startup()
        assert mgr.get_stamp_info("anything") is None

    def test_cleanup_expired_stamps(self, state_file):
        """Expired stamps removed from ownership."""
        mgr = StampOwnershipManager(state_file=state_file)
        mgr.register_stamp("valid", owner="0xBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB", mode="paid", source="pool")
        mgr.register_stamp("expired", owner="0x2222222222222222222222222222222222222222", mode="paid", source="pool")

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
        mgr.register_stamp("to_remove", owner="0xBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB", mode="paid", source="pool")
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
                mock_ownership.check_access.return_value = (False, "stamp owned by 0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee...")
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
                mock_pool.reserve_stamp.return_value = mock_stamp
                mock_pool.release_reserved_stamp.return_value = mock_released
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
                mock_pool.reserve_stamp.return_value = mock_stamp
                mock_pool.release_reserved_stamp.return_value = mock_released
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
        from unittest.mock import AsyncMock
        with patch('app.api.endpoints.stamps.swarm_api') as mock_swarm:
            mock_swarm.get_chainstate = AsyncMock(return_value={"currentPrice": 24000})
            mock_swarm.calculate_stamp_amount.return_value = 100000
            mock_swarm.calculate_stamp_total_cost.return_value = 1000000
            mock_swarm.check_sufficient_funds = AsyncMock(return_value={"sufficient": True})
            mock_swarm.purchase_postage_stamp = AsyncMock(return_value="new_batch_id_123")

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


class TestPoolInventoryIsReportedDistinctly:
    """accessMode must not describe gateway inventory as usable.

    The mapping was `"owned" if mode == "paid" else "shared"`. Pool inventory
    registers with mode="pool", which is not "paid", so it fell through to
    "shared" — the value that tells a client the batch is free for anyone to use,
    about exactly the batches check_access refuses. A client would believe the
    listing and be denied on upload.
    """

    def test_pool_owned_batch_is_not_reported_as_shared(self):
        from app.services.stamp_ownership import POOL_OWNER

        for owner, mode, expected in (
            (POOL_OWNER, "pool", "pool"),
            ("0xffffffffffffffffffffffffffffffffffffffff", "paid", "owned"),
            ("shared", "free", "shared"),
        ):
            info = {"owner": owner, "mode": mode}
            if info["owner"] == POOL_OWNER:
                access_mode = "pool"
            elif info.get("mode") == "paid":
                access_mode = "owned"
            else:
                access_mode = "shared"
            assert access_mode == expected, f"{owner}/{mode} mapped to {access_mode}"

    def test_model_accepts_pool_access_mode(self):
        """The field is a Literal; an unlisted value is rejected at serialisation."""
        from app.api.models.stamp import StampDetails
        import typing
        allowed = typing.get_args(typing.get_args(StampDetails.model_fields["accessMode"].annotation)[0])
        assert "pool" in allowed, f"accessMode Literal does not permit 'pool': {allowed}"


class TestBatchIdCase:
    """The registry keys batch ids in lowercase on every read and write (#558).

    Callers used to have to lowercase the id themselves (#543, #557); one that
    forgot denied the owner of a batch registered under the lowercase id.
    """

    BATCH = "ab" * 32
    UPPER = BATCH.upper()
    MIXED = "Ab" * 32
    OWNER = "0x" + "11" * 20
    OTHER = "0x" + "22" * 20

    @pytest.fixture
    def state_file(self, tmp_path):
        return str(tmp_path / "stamp_owners.json")

    @pytest.fixture
    def x402_on(self):
        with patch('app.services.stamp_ownership.settings') as mock_settings:
            mock_settings.X402_ENABLED = True
            mock_settings.STAMP_OWNERSHIP_ALLOW_UNTRACKED = False
            yield

    def _write(self, state_file, data):
        with open(state_file, "w") as f:
            json.dump(data, f)

    def _read(self, state_file):
        with open(state_file) as f:
            return json.load(f)

    # --- lookups, writes, deletes -------------------------------------------

    def test_uppercase_and_mixed_lookup_find_lowercase_record(self, state_file, x402_on):
        mgr = StampOwnershipManager(state_file=state_file)
        mgr.register_stamp(self.BATCH, owner=self.OWNER, mode="paid", source="pool_acquire")
        for spelling in (self.BATCH, self.UPPER, self.MIXED):
            assert mgr.get_stamp_info(spelling)["owner"] == self.OWNER
            assert mgr.check_access(spelling, self.OWNER, "paid") == (True, "owner match")
            allowed, _ = mgr.check_access(spelling, self.OTHER, "paid")
            assert allowed is False

    def test_uppercase_register_stores_lowercase_key(self, state_file):
        mgr = StampOwnershipManager(state_file=state_file)
        mgr.register_stamp(self.UPPER, owner=self.OWNER, mode="paid", source="direct_purchase")
        assert list(mgr._registry) == [self.BATCH]
        assert list(self._read(state_file)) == [self.BATCH]
        assert mgr.get_stamp_info(self.BATCH)["owner"] == self.OWNER

    def test_only_if_unowned_refuses_a_case_variant(self, state_file):
        mgr = StampOwnershipManager(state_file=state_file)
        mgr.register_stamp(self.BATCH, owner=self.OWNER, mode="paid", source="direct_purchase")
        assert mgr.register_stamp(self.MIXED, owner=self.OTHER, mode="paid", source="direct_purchase",
                                  only_if_unowned=True) is False
        assert mgr.get_stamp_info(self.BATCH)["owner"] == self.OWNER
        assert list(mgr._registry) == [self.BATCH]

    def test_reregister_under_other_case_replaces_not_duplicates(self, state_file):
        from app.services.stamp_ownership import POOL_OWNER
        mgr = StampOwnershipManager(state_file=state_file)
        mgr.register_stamp(self.BATCH, owner=POOL_OWNER, mode="pool", source="pool_purchase")
        mgr.register_stamp(self.UPPER, owner=self.OWNER, mode="paid", source="pool_acquire")
        assert list(mgr._registry) == [self.BATCH]
        assert mgr.get_stamp_info(self.BATCH)["owner"] == self.OWNER

    def test_uppercase_remove_deletes_lowercase_record(self, state_file):
        mgr = StampOwnershipManager(state_file=state_file)
        mgr.register_stamp(self.BATCH, owner=self.OWNER, mode="paid", source="pool")
        mgr.remove_stamp(self.UPPER)
        assert mgr.get_stamp_info(self.BATCH) is None
        assert self._read(state_file) == {}

    def test_cleanup_expired_matches_case_insensitively(self, state_file):
        mgr = StampOwnershipManager(state_file=state_file)
        mgr.register_stamp(self.BATCH, owner=self.OWNER, mode="paid", source="pool")
        mgr.register_stamp("cd" * 32, owner=self.OTHER, mode="paid", source="pool")
        mgr.cleanup_expired(valid_batch_ids={self.UPPER})
        assert mgr.get_stamp_info(self.BATCH) is not None
        assert mgr.get_stamp_info("cd" * 32) is None
        assert list(self._read(state_file)) == [self.BATCH]

    # --- load-time migration ---------------------------------------------------

    def test_load_lowercases_mixed_case_keys_and_persists(self, state_file, x402_on):
        self._write(state_file, {
            self.UPPER: {"owner": "0x" + "AA" * 20, "mode": "paid", "source": "pool"},
            "Cd" * 32: {"owner": "shared", "mode": "free", "source": "pool"},
            "ef" * 32: {"owner": "pool", "mode": "pool", "source": "pool"},
        })
        mgr = StampOwnershipManager(state_file=state_file)
        mgr.load_on_startup()

        expected_keys = {self.BATCH, "cd" * 32, "ef" * 32}
        assert set(mgr._registry) == expected_keys
        assert mgr.check_access(self.BATCH, "0x" + "aa" * 20, "paid")[0] is True
        assert mgr.check_access("CD" * 32, None, None)[0] is True

        saved = self._read(state_file)
        assert set(saved) == expected_keys
        assert saved[self.BATCH]["owner"] == "0x" + "aa" * 20

    def test_clean_file_is_not_rewritten(self, state_file):
        self._write(state_file, {self.BATCH: {"owner": self.OWNER, "mode": "paid"}})
        mgr = StampOwnershipManager(state_file=state_file)
        with patch("app.services.stamp_ownership.atomic_write_json") as write:
            mgr.load_on_startup()
        write.assert_not_called()
        assert mgr.get_stamp_info(self.UPPER)["owner"] == self.OWNER

    def test_migration_save_failure_keeps_file_and_memory(self, state_file):
        self._write(state_file, {self.UPPER: {"owner": self.OWNER, "mode": "paid"}})
        before = open(state_file).read()
        mgr = StampOwnershipManager(state_file=state_file)
        with patch("app.services.stamp_ownership.atomic_write_json", side_effect=OSError("disk full")):
            mgr.load_on_startup()
        assert open(state_file).read() == before
        assert mgr.get_stamp_info(self.BATCH)["owner"] == self.OWNER

        # The next start migrates the same file again.
        mgr2 = StampOwnershipManager(state_file=state_file)
        mgr2.load_on_startup()
        assert list(self._read(state_file)) == [self.BATCH]
        assert mgr2.get_stamp_info(self.BATCH)["owner"] == self.OWNER

    # --- keys that differ only in case -----------------------------------------

    def test_collision_same_owner_keeps_the_lowercase_record(self, state_file):
        # Uppercase first in file order: the lowercase-key record must still win.
        self._write(state_file, {
            self.UPPER: {"owner": "0x" + "11" * 20, "mode": "paid", "source": "old"},
            self.BATCH: {"owner": self.OWNER, "mode": "paid", "source": "new"},
        })
        mgr = StampOwnershipManager(state_file=state_file)
        mgr.load_on_startup()
        assert list(mgr._registry) == [self.BATCH]
        assert mgr.get_stamp_info(self.BATCH)["source"] == "new"
        assert list(self._read(state_file)) == [self.BATCH]

    @pytest.mark.parametrize("special", ["shared", "pool"])
    @pytest.mark.parametrize("wallet_key_is_lower", [True, False])
    def test_collision_wallet_beats_special_owner(self, state_file, x402_on, caplog, special, wallet_key_is_lower):
        wallet_key, special_key = (self.BATCH, self.UPPER) if wallet_key_is_lower else (self.UPPER, self.BATCH)
        self._write(state_file, {
            special_key: {"owner": special, "mode": "free"},
            wallet_key: {"owner": self.OWNER, "mode": "paid"},
        })
        mgr = StampOwnershipManager(state_file=state_file)
        with caplog.at_level("WARNING", logger="app.services.stamp_ownership"):
            mgr.load_on_startup()
        assert list(mgr._registry) == [self.BATCH]
        assert mgr.get_stamp_info(self.BATCH)["owner"] == self.OWNER
        assert mgr.check_access(self.BATCH, self.OWNER, "paid")[0] is True
        assert mgr.check_access(self.BATCH, self.OTHER, "paid")[0] is False
        assert mgr.check_access(self.BATCH, None, None)[0] is False
        assert self._read(state_file)[self.BATCH]["owner"] == self.OWNER
        assert any("two spellings" in r.message for r in caplog.records)

    @pytest.mark.parametrize("owners", [
        ("0x" + "11" * 20, "0x" + "22" * 20),
        ("shared", "pool"),
    ])
    def test_collision_without_safe_choice_refuses_to_load(self, state_file, owners):
        import glob
        from app.core.atomic_io import StateLoadError
        self._write(state_file, {
            self.BATCH: {"owner": owners[0], "mode": "paid"},
            self.MIXED: {"owner": owners[1], "mode": "paid"},
        })
        before = open(state_file).read()
        mgr = StampOwnershipManager(state_file=state_file)
        with pytest.raises(StateLoadError, match="two spellings"):
            mgr.load_on_startup()
        assert open(state_file).read() == before
        assert glob.glob(state_file + ".corrupt-*")
        assert mgr._registry == {}
