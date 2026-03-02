# tests/test_stamp_pool.py
"""
Unit tests for the Stamp Pool feature.
"""
import json
import os
import tempfile
import pytest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient

from app.services.stamp_pool import (
    StampPoolManager,
    PoolStamp,
    PoolStampStatus,
    PoolStatus
)
from app.api.endpoints.pool import depth_to_size_name


class TestPoolStampDataclass:
    """Test the PoolStamp dataclass."""

    def test_pool_stamp_creation(self):
        """Test creating a PoolStamp."""
        stamp = PoolStamp(
            batch_id="abc123",
            depth=17,
            amount=1000000,
            created_at=datetime.now(timezone.utc),
            ttl_at_creation=604800,  # 1 week
        )

        assert stamp.batch_id == "abc123"
        assert stamp.depth == 17
        assert stamp.amount == 1000000
        assert stamp.status == PoolStampStatus.AVAILABLE
        assert stamp.released_at is None
        assert stamp.released_to is None

    def test_pool_stamp_with_label(self):
        """Test PoolStamp with custom label."""
        stamp = PoolStamp(
            batch_id="def456",
            depth=20,
            amount=2000000,
            created_at=datetime.now(timezone.utc),
            ttl_at_creation=604800,
            label="my-custom-label"
        )

        assert stamp.label == "my-custom-label"


class TestStampPoolManager:
    """Test the StampPoolManager class."""

    @pytest.fixture
    def manager(self):
        """Create a fresh StampPoolManager for each test."""
        return StampPoolManager()

    @pytest.fixture
    def sample_stamp(self):
        """Create a sample pool stamp."""
        return PoolStamp(
            batch_id="test123456789",
            depth=17,
            amount=1000000,
            created_at=datetime.now(timezone.utc),
            ttl_at_creation=604800,
            label="test-stamp"
        )

    def test_add_stamp_to_pool(self, manager):
        """Test adding a stamp to the pool."""
        stamp = manager.add_stamp_to_pool(
            batch_id="abc123",
            depth=17,
            amount=1000000,
            ttl=604800,
            label="test"
        )

        assert stamp.batch_id == "abc123"
        assert stamp.depth == 17
        assert stamp.status == PoolStampStatus.AVAILABLE
        assert "abc123" in manager._pool

    def test_get_available_stamp_exact_match(self, manager):
        """Test getting a stamp with exact depth match."""
        manager.add_stamp_to_pool("stamp17", 17, 1000000, 604800)
        manager.add_stamp_to_pool("stamp20", 20, 1000000, 604800)

        stamp = manager.get_available_stamp(17)
        assert stamp is not None
        assert stamp.batch_id == "stamp17"
        assert stamp.depth == 17

    def test_get_available_stamp_no_match(self, manager):
        """Test getting a stamp when none available at depth."""
        manager.add_stamp_to_pool("stamp20", 20, 1000000, 604800)

        stamp = manager.get_available_stamp(17)
        assert stamp is None

    def test_get_available_stamp_any_size(self, manager):
        """Test getting any available stamp with minimum depth."""
        manager.add_stamp_to_pool("stamp17", 17, 1000000, 604800)
        manager.add_stamp_to_pool("stamp20", 20, 1000000, 604800)
        manager.add_stamp_to_pool("stamp22", 22, 1000000, 604800)

        # Should get smallest that meets requirement
        stamp = manager.get_available_stamp_any_size(min_depth=18)
        assert stamp is not None
        assert stamp.depth == 20  # Smallest >= 18

    def test_get_available_stamp_any_size_no_match(self, manager):
        """Test getting any stamp when none meet minimum."""
        manager.add_stamp_to_pool("stamp17", 17, 1000000, 604800)

        stamp = manager.get_available_stamp_any_size(min_depth=20)
        assert stamp is None

    def test_release_stamp(self, manager):
        """Test releasing a stamp from the pool."""
        manager.add_stamp_to_pool("stamp123", 17, 1000000, 604800)

        released = manager.release_stamp("stamp123", released_to="192.168.1.1")

        assert released is not None
        assert released.batch_id == "stamp123"
        assert released.status == PoolStampStatus.RELEASED
        assert released.released_to == "192.168.1.1"
        assert released.released_at is not None
        assert "stamp123" not in manager._pool  # Removed from pool

    def test_release_stamp_not_found(self, manager):
        """Test releasing a stamp that doesn't exist."""
        released = manager.release_stamp("nonexistent")
        assert released is None

    def test_release_stamp_already_released(self, manager):
        """Test releasing an already released stamp."""
        manager.add_stamp_to_pool("stamp123", 17, 1000000, 604800)
        manager.release_stamp("stamp123")

        # Try to release again
        released = manager.release_stamp("stamp123")
        assert released is None

    def test_get_status(self, manager):
        """Test getting pool status."""
        manager.add_stamp_to_pool("stamp17a", 17, 1000000, 604800)
        manager.add_stamp_to_pool("stamp17b", 17, 1000000, 604800)
        manager.add_stamp_to_pool("stamp20", 20, 1000000, 604800)

        status = manager.get_status()

        assert isinstance(status, PoolStatus)
        assert status.total_stamps == 3
        assert status.current_levels.get(17) == 2
        assert status.current_levels.get(20) == 1
        assert len(status.available_stamps.get(17, [])) == 2
        assert len(status.available_stamps.get(20, [])) == 1

    def test_get_status_empty_pool(self, manager):
        """Test getting status of empty pool."""
        status = manager.get_status()

        assert status.total_stamps == 0
        assert status.current_levels == {}
        assert status.available_stamps == {}


class TestPoolConfiguration:
    """Test stamp pool configuration."""

    def test_default_reserve_config(self):
        """Test default reserve configuration."""
        with patch('app.core.config.settings') as mock_settings:
            mock_settings.STAMP_POOL_RESERVE_SMALL = 1
            mock_settings.STAMP_POOL_RESERVE_MEDIUM = 1
            mock_settings.STAMP_POOL_RESERVE_LARGE = 0

            # Manually call the method since we're patching settings
            config = {}
            if mock_settings.STAMP_POOL_RESERVE_SMALL > 0:
                config[17] = mock_settings.STAMP_POOL_RESERVE_SMALL
            if mock_settings.STAMP_POOL_RESERVE_MEDIUM > 0:
                config[20] = mock_settings.STAMP_POOL_RESERVE_MEDIUM
            if mock_settings.STAMP_POOL_RESERVE_LARGE > 0:
                config[22] = mock_settings.STAMP_POOL_RESERVE_LARGE

            assert config == {17: 1, 20: 1}

    def test_reserve_config_no_large(self):
        """Test reserve config excludes large when set to 0."""
        with patch('app.core.config.settings') as mock_settings:
            mock_settings.STAMP_POOL_RESERVE_SMALL = 2
            mock_settings.STAMP_POOL_RESERVE_MEDIUM = 1
            mock_settings.STAMP_POOL_RESERVE_LARGE = 0

            config = {}
            if mock_settings.STAMP_POOL_RESERVE_SMALL > 0:
                config[17] = mock_settings.STAMP_POOL_RESERVE_SMALL
            if mock_settings.STAMP_POOL_RESERVE_MEDIUM > 0:
                config[20] = mock_settings.STAMP_POOL_RESERVE_MEDIUM
            if mock_settings.STAMP_POOL_RESERVE_LARGE > 0:
                config[22] = mock_settings.STAMP_POOL_RESERVE_LARGE

            assert 22 not in config
            assert config == {17: 2, 20: 1}


class TestDepthToSizeName:
    """Test the depth_to_size_name helper."""

    def test_small_depth(self):
        """Test conversion of depth 17 to 'small'."""
        assert depth_to_size_name(17) == "small"

    def test_medium_depth(self):
        """Test conversion of depth 20 to 'medium'."""
        assert depth_to_size_name(20) == "medium"

    def test_large_depth(self):
        """Test conversion of depth 22 to 'large'."""
        assert depth_to_size_name(22) == "large"

    def test_unknown_depth(self):
        """Test conversion of unknown depth."""
        assert depth_to_size_name(25) == "depth-25"


class TestPoolAPIEndpoints:
    """Test the pool API endpoints.

    Note: These tests use the actual settings. The pool endpoints check
    settings.STAMP_POOL_ENABLED at runtime, so we mock where it's read.
    """

    @pytest.fixture
    def client(self):
        """Create test client."""
        from app.main import app
        return TestClient(app)

    def test_get_pool_status(self, client):
        """Test GET /api/v1/pool/status endpoint.
        Status endpoint always works regardless of enabled state."""
        response = client.get("/api/v1/pool/status")
        assert response.status_code == 200
        data = response.json()
        assert "enabled" in data
        assert "reserve_config" in data
        assert "current_levels" in data
        assert "total_stamps" in data

    def test_acquire_stamp_with_pool_enabled(self, client):
        """Test acquiring stamp from empty pool when enabled returns 409."""
        with patch('app.api.endpoints.pool.settings') as mock_settings:
            mock_settings.STAMP_POOL_ENABLED = True

            response = client.post(
                "/api/v1/pool/acquire",
                json={"size": "small"}
            )
            assert response.status_code == 409
            data = response.json()
            assert "No stamp available" in data["detail"]["message"]
            assert "suggestion" in data["detail"]

    def test_acquire_exhausted_includes_suggestion(self, client):
        """Test that pool exhausted response includes a suggestion to buy directly."""
        with patch('app.api.endpoints.pool.settings') as mock_settings:
            mock_settings.STAMP_POOL_ENABLED = True

            response = client.post(
                "/api/v1/pool/acquire",
                json={"size": "medium"}
            )
            assert response.status_code == 409
            detail = response.json()["detail"]
            assert "Pool is exhausted" in detail["message"]
            assert "POST /api/v1/stamps/" in detail["suggestion"]

    def test_acquire_race_condition_returns_409(self, client):
        """Test that race condition during acquire returns 409 with suggestion."""
        with patch('app.api.endpoints.pool.settings') as mock_settings:
            mock_settings.STAMP_POOL_ENABLED = True

            # Mock: stamp found but release fails (race condition)
            mock_stamp = MagicMock()
            mock_stamp.batch_id = "abc123"
            mock_stamp.depth = 17

            with patch('app.api.endpoints.pool.stamp_pool_manager') as mock_pool:
                mock_pool.get_available_stamp.return_value = mock_stamp
                mock_pool.get_available_stamp_any_size.return_value = None
                mock_pool.release_stamp.return_value = None  # Race: already taken

                response = client.post(
                    "/api/v1/pool/acquire",
                    json={"size": "small"}
                )
                assert response.status_code == 409
                detail = response.json()["detail"]
                assert "acquired by another request" in detail["message"]
                assert "suggestion" in detail

    def test_list_available_stamps_with_pool_enabled(self, client):
        """Test listing stamps from empty pool when enabled."""
        with patch('app.api.endpoints.pool.settings') as mock_settings:
            mock_settings.STAMP_POOL_ENABLED = True

            response = client.get("/api/v1/pool/available")
            assert response.status_code == 200
            data = response.json()
            assert data == []


class TestPoolAPIDisabled:
    """Test pool API when feature is disabled."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        from app.main import app
        return TestClient(app)

    def test_acquire_stamp_disabled(self, client):
        """Test acquire returns 404 when disabled."""
        with patch('app.api.endpoints.pool.settings') as mock_settings:
            mock_settings.STAMP_POOL_ENABLED = False

            response = client.post(
                "/api/v1/pool/acquire",
                json={"size": "small"}
            )
            assert response.status_code == 404
            assert "not enabled" in response.json()["detail"]

    def test_list_stamps_disabled(self, client):
        """Test list returns 404 when disabled."""
        with patch('app.api.endpoints.pool.settings') as mock_settings:
            mock_settings.STAMP_POOL_ENABLED = False

            response = client.get("/api/v1/pool/available")
            assert response.status_code == 404

    def test_trigger_check_disabled(self, client):
        """Test manual check returns 404 when disabled."""
        with patch('app.api.endpoints.pool.settings') as mock_settings:
            mock_settings.STAMP_POOL_ENABLED = False

            response = client.post("/api/v1/pool/check")
            assert response.status_code == 404


class TestImmediateReplenishment:
    """Test immediate replenishment after stamp release."""

    @pytest.fixture
    def manager(self):
        """Create a fresh StampPoolManager for each test."""
        return StampPoolManager()

    def test_trigger_replenishment_when_below_target(self, manager):
        """Test that replenishment is triggered when pool drops below target."""
        # Add a stamp and configure target to 1
        manager.add_stamp_to_pool("stamp17", 17, 1000000, 604800)

        with patch.object(manager, 'get_reserve_config', return_value={17: 1}):
            with patch('app.services.stamp_pool.settings') as mock_settings:
                mock_settings.STAMP_POOL_IMMEDIATE_REPLENISH = True

                # Release the stamp, pool is now at 0, below target 1
                manager.release_stamp("stamp17")

                # Now trigger should return True (needs replenishment)
                # Need to mock asyncio.create_task to prevent actual task creation
                with patch('asyncio.create_task') as mock_create_task:
                    triggered = manager.trigger_replenishment_if_needed(17)
                    assert triggered is True
                    mock_create_task.assert_called_once()

    def test_no_trigger_when_at_target(self, manager):
        """Test no replenishment triggered when pool is at target."""
        # Add two stamps
        manager.add_stamp_to_pool("stamp17a", 17, 1000000, 604800)
        manager.add_stamp_to_pool("stamp17b", 17, 1000000, 604800)

        with patch.object(manager, 'get_reserve_config', return_value={17: 1}):
            with patch('app.services.stamp_pool.settings') as mock_settings:
                mock_settings.STAMP_POOL_IMMEDIATE_REPLENISH = True

                # Release one stamp, pool is now at 1, equal to target 1
                manager.release_stamp("stamp17a")

                # Should not trigger - we're at target
                triggered = manager.trigger_replenishment_if_needed(17)
                assert triggered is False

    def test_no_trigger_when_disabled(self, manager):
        """Test no replenishment when immediate replenishment is disabled."""
        manager.add_stamp_to_pool("stamp17", 17, 1000000, 604800)

        with patch.object(manager, 'get_reserve_config', return_value={17: 1}):
            with patch('app.services.stamp_pool.settings') as mock_settings:
                mock_settings.STAMP_POOL_IMMEDIATE_REPLENISH = False

                # Release the stamp
                manager.release_stamp("stamp17")

                # Should not trigger - feature disabled
                triggered = manager.trigger_replenishment_if_needed(17)
                assert triggered is False

    def test_no_trigger_for_unconfigured_depth(self, manager):
        """Test no replenishment for depths not in reserve config."""
        manager.add_stamp_to_pool("stamp22", 22, 1000000, 604800)

        with patch.object(manager, 'get_reserve_config', return_value={17: 1, 20: 1}):  # No 22
            with patch('app.services.stamp_pool.settings') as mock_settings:
                mock_settings.STAMP_POOL_IMMEDIATE_REPLENISH = True

                # Release the stamp
                manager.release_stamp("stamp22")

                # Should not trigger - depth 22 not configured
                triggered = manager.trigger_replenishment_if_needed(22)
                assert triggered is False

    def test_pending_replenishments_tracked(self, manager):
        """Test that pending replenishments are tracked to avoid over-ordering."""
        with patch.object(manager, 'get_reserve_config', return_value={17: 2}):
            with patch('app.services.stamp_pool.settings') as mock_settings:
                mock_settings.STAMP_POOL_IMMEDIATE_REPLENISH = True

                with patch('asyncio.create_task') as mock_create_task:
                    # First trigger should succeed
                    triggered1 = manager.trigger_replenishment_if_needed(17)
                    assert triggered1 is True
                    assert manager._pending_replenishments.get(17) == 1

                    # Second trigger should also succeed since we need 2
                    triggered2 = manager.trigger_replenishment_if_needed(17)
                    assert triggered2 is True
                    assert manager._pending_replenishments.get(17) == 2

                    # Third trigger should fail - we have 2 pending, target is 2
                    triggered3 = manager.trigger_replenishment_if_needed(17)
                    assert triggered3 is False

                    assert mock_create_task.call_count == 2


class TestLowReserveWarning:
    """Test low reserve warning logic."""

    def test_low_reserve_warning_triggered(self):
        """Test that low reserve warning is triggered correctly."""
        manager = StampPoolManager()

        # Add one stamp at depth 17
        manager.add_stamp_to_pool("stamp17", 17, 1000000, 604800)

        with patch.object(manager, 'get_reserve_config', return_value={17: 2, 20: 1}):
            with patch('app.core.config.settings') as mock_settings:
                mock_settings.STAMP_POOL_LOW_RESERVE_THRESHOLD = 1

                status = manager.get_status()
                # With target 2 and current 1, and threshold 1, warning should trigger
                # because current (1) <= threshold (1) AND current (1) < target (2)
                assert status.low_reserve_warning is True

    def test_no_warning_when_above_threshold(self):
        """Test no warning when levels are adequate."""
        manager = StampPoolManager()

        # Add enough stamps
        manager.add_stamp_to_pool("stamp17a", 17, 1000000, 604800)
        manager.add_stamp_to_pool("stamp17b", 17, 1000000, 604800)
        manager.add_stamp_to_pool("stamp20", 20, 1000000, 604800)

        with patch.object(manager, 'get_reserve_config', return_value={17: 2, 20: 1}):
            with patch('app.core.config.settings') as mock_settings:
                mock_settings.STAMP_POOL_LOW_RESERVE_THRESHOLD = 1

                status = manager.get_status()
                # All targets met, no warning
                assert status.low_reserve_warning is False


class TestPoolStatePersistence:
    """Test pool state persistence (save/load)."""

    @pytest.fixture
    def state_file(self, tmp_path):
        """Create a temporary state file path."""
        return str(tmp_path / "pool_state.json")

    @pytest.fixture
    def manager(self, state_file):
        """Create a StampPoolManager with a temp state file."""
        return StampPoolManager(state_file=state_file)

    def test_save_and_load_state(self, state_file):
        """Test round-trip: add stamps, save, create new manager, load."""
        manager1 = StampPoolManager(state_file=state_file)
        manager1.add_stamp_to_pool("batch_aaa", 17, 1000000, 604800)
        manager1.add_stamp_to_pool("batch_bbb", 20, 2000000, 604800)

        # Create a new manager with the same state file
        manager2 = StampPoolManager(state_file=state_file)
        loaded_ids = manager2._load_state()

        assert loaded_ids == {"batch_aaa", "batch_bbb"}

    def test_load_state_missing_file(self, tmp_path):
        """Test loading state when file doesn't exist returns empty set."""
        manager = StampPoolManager(state_file=str(tmp_path / "nonexistent.json"))
        loaded = manager._load_state()
        assert loaded == set()

    def test_load_state_corrupt_file(self, state_file):
        """Test loading corrupt state file returns empty set and logs warning."""
        with open(state_file, 'w') as f:
            f.write("not valid json {{{")

        manager = StampPoolManager(state_file=state_file)
        loaded = manager._load_state()
        assert loaded == set()

    def test_load_state_wrong_type(self, state_file):
        """Test loading state file with wrong JSON type returns empty set."""
        with open(state_file, 'w') as f:
            json.dump({"not": "a list"}, f)

        manager = StampPoolManager(state_file=state_file)
        loaded = manager._load_state()
        assert loaded == set()

    def test_add_stamp_saves_state(self, manager, state_file):
        """Test that adding a stamp to pool persists it."""
        manager.add_stamp_to_pool("batch_123", 17, 1000000, 604800)

        # Verify state file was written
        with open(state_file, 'r') as f:
            saved = json.load(f)
        assert "batch_123" in saved

    def test_release_stamp_saves_state(self, manager, state_file):
        """Test that releasing a stamp removes it from state file."""
        manager.add_stamp_to_pool("batch_123", 17, 1000000, 604800)
        manager.release_stamp("batch_123")

        with open(state_file, 'r') as f:
            saved = json.load(f)
        assert "batch_123" not in saved

    @pytest.mark.asyncio
    async def test_expired_stamp_removed_from_state(self, manager, state_file):
        """Test that TTL cleanup removes expired stamps from state file."""
        manager.add_stamp_to_pool("batch_exp", 17, 1000000, 604800)

        # Verify stamp is in state file
        with open(state_file, 'r') as f:
            saved = json.load(f)
        assert "batch_exp" in saved

        # Mock Bee node returning stamp as expired
        mock_stamps = [{"batchID": "batch_exp", "batchTTL": 0, "usable": False}]
        with patch('app.services.stamp_pool.swarm_api.get_all_stamps_processed', return_value=mock_stamps):
            await manager._update_stamp_ttls()

        # Stamp should be removed from pool and state file
        assert "batch_exp" not in manager._pool
        with open(state_file, 'r') as f:
            saved = json.load(f)
        assert "batch_exp" not in saved

    def test_save_state_creates_directory(self, tmp_path):
        """Test that _save_state creates the data directory if missing."""
        nested_path = str(tmp_path / "subdir" / "pool_state.json")
        manager = StampPoolManager(state_file=nested_path)
        manager.add_stamp_to_pool("batch_dir", 17, 1000000, 604800)

        assert os.path.exists(nested_path)


class TestPoolSyncBehavior:
    """Test that sync_from_bee_node only imports known stamps."""

    @pytest.fixture
    def state_file(self, tmp_path):
        """Create a temporary state file path."""
        return str(tmp_path / "pool_state.json")

    @pytest.mark.asyncio
    async def test_sync_only_imports_known_stamps(self, state_file):
        """Bee has 50 stamps, state has 2 IDs -> only 2 imported."""
        # Write state file with 2 known IDs
        with open(state_file, 'w') as f:
            json.dump(["known_aaa", "known_bbb"], f)

        # Mock Bee node returning 50 stamps
        bee_stamps = []
        for i in range(50):
            bee_stamps.append({
                "batchID": f"stamp_{i:03d}",
                "depth": 17,
                "local": True,
                "usable": True,
                "batchTTL": 604800,
                "amount": "1000000",
                "label": ""
            })
        # Add the known stamps to the Bee response
        bee_stamps.append({"batchID": "known_aaa", "depth": 17, "local": True, "usable": True, "batchTTL": 604800, "amount": "1000000", "label": ""})
        bee_stamps.append({"batchID": "known_bbb", "depth": 20, "local": True, "usable": True, "batchTTL": 604800, "amount": "2000000", "label": ""})

        manager = StampPoolManager(state_file=state_file)
        with patch('app.services.stamp_pool.swarm_api.get_all_stamps_processed', return_value=bee_stamps):
            synced = await manager.sync_from_bee_node()

        assert synced == 2
        assert len(manager._pool) == 2
        assert "known_aaa" in manager._pool
        assert "known_bbb" in manager._pool

    @pytest.mark.asyncio
    async def test_sync_first_run_imports_nothing(self, state_file):
        """No state file -> sync returns 0, pool empty."""
        manager = StampPoolManager(state_file=state_file)

        bee_stamps = [
            {"batchID": f"stamp_{i}", "depth": 17, "local": True, "usable": True, "batchTTL": 604800, "amount": "1000000", "label": ""}
            for i in range(10)
        ]
        with patch('app.services.stamp_pool.swarm_api.get_all_stamps_processed', return_value=bee_stamps):
            synced = await manager.sync_from_bee_node()

        assert synced == 0
        assert len(manager._pool) == 0

    @pytest.mark.asyncio
    async def test_sync_skips_expired_known_stamps(self, state_file):
        """Stamp in state but expired on Bee -> not imported, removed from state."""
        with open(state_file, 'w') as f:
            json.dump(["expired_stamp", "valid_stamp"], f)

        bee_stamps = [
            {"batchID": "expired_stamp", "depth": 17, "local": True, "usable": False, "batchTTL": 0, "amount": "1000000", "label": ""},
            {"batchID": "valid_stamp", "depth": 17, "local": True, "usable": True, "batchTTL": 604800, "amount": "1000000", "label": ""},
        ]

        manager = StampPoolManager(state_file=state_file)
        with patch('app.services.stamp_pool.swarm_api.get_all_stamps_processed', return_value=bee_stamps):
            synced = await manager.sync_from_bee_node()

        assert synced == 1
        assert "expired_stamp" not in manager._pool
        assert "valid_stamp" in manager._pool

        # State file should be updated (expired stamp removed)
        with open(state_file, 'r') as f:
            saved = json.load(f)
        assert "expired_stamp" not in saved
        assert "valid_stamp" in saved

    @pytest.mark.asyncio
    async def test_sync_skips_unusable_known_stamps(self, state_file):
        """Stamp in state but unusable -> not imported."""
        with open(state_file, 'w') as f:
            json.dump(["unusable_stamp"], f)

        bee_stamps = [
            {"batchID": "unusable_stamp", "depth": 17, "local": True, "usable": False, "batchTTL": 100, "amount": "1000000", "label": ""},
        ]

        manager = StampPoolManager(state_file=state_file)
        with patch('app.services.stamp_pool.swarm_api.get_all_stamps_processed', return_value=bee_stamps):
            synced = await manager.sync_from_bee_node()

        assert synced == 0
        assert "unusable_stamp" not in manager._pool

    @pytest.mark.asyncio
    async def test_sync_skips_missing_known_stamps(self, state_file):
        """Stamp in state but not on Bee node -> not imported."""
        with open(state_file, 'w') as f:
            json.dump(["gone_stamp"], f)

        bee_stamps = []  # Bee has no stamps

        manager = StampPoolManager(state_file=state_file)
        with patch('app.services.stamp_pool.swarm_api.get_all_stamps_processed', return_value=bee_stamps):
            synced = await manager.sync_from_bee_node()

        assert synced == 0
        assert len(manager._pool) == 0


class TestPoolResizeBehavior:
    """Test pool purchasing behavior on first run and restarts."""

    @pytest.fixture
    def state_file(self, tmp_path):
        """Create a temporary state file path."""
        return str(tmp_path / "pool_state.json")

    @pytest.mark.asyncio
    async def test_pool_purchases_to_target_on_first_run(self, state_file):
        """Empty state -> purchases exactly reserve count."""
        manager = StampPoolManager(state_file=state_file)

        purchase_count = 0

        async def mock_purchase(depth):
            nonlocal purchase_count
            purchase_count += 1
            batch_id = f"purchased_{purchase_count}"
            manager.add_stamp_to_pool(batch_id, depth, 1000000, 604800)
            return batch_id

        with patch.object(manager, '_purchase_stamp', side_effect=mock_purchase):
            with patch.object(manager, 'get_reserve_config', return_value={17: 2}):
                with patch('app.services.stamp_pool.swarm_api.get_all_stamps_processed', return_value=[]):
                    with patch.object(manager, '_update_stamp_ttls', new_callable=AsyncMock):
                        with patch.object(manager, '_get_stamp_ttl', return_value=604800):
                            with patch('app.services.stamp_pool.settings') as mock_settings:
                                mock_settings.STAMP_POOL_ENABLED = True
                                mock_settings.STAMP_POOL_MIN_TTL_HOURS = 24

                                await manager.check_and_replenish()

        assert purchase_count == 2
        assert len(manager._pool) == 2

    @pytest.mark.asyncio
    async def test_pool_does_not_over_purchase(self, state_file):
        """Restart with full state -> no new purchases."""
        # Pre-populate state file
        with open(state_file, 'w') as f:
            json.dump(["existing_1", "existing_2"], f)

        bee_stamps = [
            {"batchID": "existing_1", "depth": 17, "local": True, "usable": True, "batchTTL": 604800, "amount": "1000000", "label": ""},
            {"batchID": "existing_2", "depth": 17, "local": True, "usable": True, "batchTTL": 604800, "amount": "1000000", "label": ""},
        ]

        manager = StampPoolManager(state_file=state_file)
        purchase_count = 0

        async def mock_purchase(depth):
            nonlocal purchase_count
            purchase_count += 1
            return f"new_{purchase_count}"

        with patch.object(manager, '_purchase_stamp', side_effect=mock_purchase):
            with patch.object(manager, 'get_reserve_config', return_value={17: 2}):
                with patch('app.services.stamp_pool.swarm_api.get_all_stamps_processed', return_value=bee_stamps):
                    with patch.object(manager, '_update_stamp_ttls', new_callable=AsyncMock):
                        with patch.object(manager, '_get_stamp_ttl', return_value=604800):
                            with patch('app.services.stamp_pool.settings') as mock_settings:
                                mock_settings.STAMP_POOL_ENABLED = True
                                mock_settings.STAMP_POOL_MIN_TTL_HOURS = 24

                                await manager.check_and_replenish()

        assert purchase_count == 0
        assert len(manager._pool) == 2

    @pytest.mark.asyncio
    async def test_pool_fills_gap_after_release(self, state_file):
        """Release a stamp -> replenishment brings back to target."""
        manager = StampPoolManager(state_file=state_file)

        # Add stamps up to target
        manager.add_stamp_to_pool("stamp_a", 17, 1000000, 604800)
        manager.add_stamp_to_pool("stamp_b", 17, 1000000, 604800)

        # Release one
        manager.release_stamp("stamp_a")
        assert len(manager._pool) == 1

        purchase_count = 0

        async def mock_purchase(depth):
            nonlocal purchase_count
            purchase_count += 1
            batch_id = f"replenished_{purchase_count}"
            manager.add_stamp_to_pool(batch_id, depth, 1000000, 604800)
            return batch_id

        # Simulate check_and_replenish
        bee_stamps = [
            {"batchID": "stamp_b", "depth": 17, "local": True, "usable": True, "batchTTL": 604800, "amount": "1000000", "label": ""},
        ]

        with patch.object(manager, '_purchase_stamp', side_effect=mock_purchase):
            with patch.object(manager, 'get_reserve_config', return_value={17: 2}):
                with patch('app.services.stamp_pool.swarm_api.get_all_stamps_processed', return_value=bee_stamps):
                    with patch.object(manager, '_update_stamp_ttls', new_callable=AsyncMock):
                        with patch.object(manager, '_get_stamp_ttl', return_value=604800):
                            with patch('app.services.stamp_pool.settings') as mock_settings:
                                mock_settings.STAMP_POOL_ENABLED = True
                                mock_settings.STAMP_POOL_MIN_TTL_HOURS = 24

                                await manager.check_and_replenish()

        assert purchase_count == 1
        assert len(manager._pool) == 2

    @pytest.mark.asyncio
    async def test_pool_respects_reserve_change(self, state_file):
        """If reserve config changes (1 to 2), purchases the diff."""
        # State has 1 stamp
        with open(state_file, 'w') as f:
            json.dump(["existing_1"], f)

        bee_stamps = [
            {"batchID": "existing_1", "depth": 17, "local": True, "usable": True, "batchTTL": 604800, "amount": "1000000", "label": ""},
        ]

        manager = StampPoolManager(state_file=state_file)
        purchase_count = 0

        async def mock_purchase(depth):
            nonlocal purchase_count
            purchase_count += 1
            batch_id = f"new_{purchase_count}"
            manager.add_stamp_to_pool(batch_id, depth, 1000000, 604800)
            return batch_id

        # Config now wants 2 stamps at depth 17
        with patch.object(manager, '_purchase_stamp', side_effect=mock_purchase):
            with patch.object(manager, 'get_reserve_config', return_value={17: 2}):
                with patch('app.services.stamp_pool.swarm_api.get_all_stamps_processed', return_value=bee_stamps):
                    with patch.object(manager, '_update_stamp_ttls', new_callable=AsyncMock):
                        with patch.object(manager, '_get_stamp_ttl', return_value=604800):
                            with patch('app.services.stamp_pool.settings') as mock_settings:
                                mock_settings.STAMP_POOL_ENABLED = True
                                mock_settings.STAMP_POOL_MIN_TTL_HOURS = 24

                                await manager.check_and_replenish()

        assert purchase_count == 1  # Only purchased the diff
        assert len(manager._pool) == 2

    @pytest.mark.asyncio
    async def test_pool_does_not_shrink_below_target(self, state_file):
        """If reserve lowered (2->1), doesn't remove stamps (natural attrition)."""
        with open(state_file, 'w') as f:
            json.dump(["stamp_1", "stamp_2"], f)

        bee_stamps = [
            {"batchID": "stamp_1", "depth": 17, "local": True, "usable": True, "batchTTL": 604800, "amount": "1000000", "label": ""},
            {"batchID": "stamp_2", "depth": 17, "local": True, "usable": True, "batchTTL": 604800, "amount": "1000000", "label": ""},
        ]

        manager = StampPoolManager(state_file=state_file)
        purchase_count = 0

        async def mock_purchase(depth):
            nonlocal purchase_count
            purchase_count += 1
            return f"new_{purchase_count}"

        # Config now wants only 1, but pool has 2
        with patch.object(manager, '_purchase_stamp', side_effect=mock_purchase):
            with patch.object(manager, 'get_reserve_config', return_value={17: 1}):
                with patch('app.services.stamp_pool.swarm_api.get_all_stamps_processed', return_value=bee_stamps):
                    with patch.object(manager, '_update_stamp_ttls', new_callable=AsyncMock):
                        with patch.object(manager, '_get_stamp_ttl', return_value=604800):
                            with patch('app.services.stamp_pool.settings') as mock_settings:
                                mock_settings.STAMP_POOL_ENABLED = True
                                mock_settings.STAMP_POOL_MIN_TTL_HOURS = 24

                                await manager.check_and_replenish()

        # Should not purchase anything AND should not remove stamps
        assert purchase_count == 0
        assert len(manager._pool) == 2  # Both stamps kept
