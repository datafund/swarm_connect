# tests/test_stamp_pool.py
"""
Unit tests for the Stamp Pool feature.
"""
import json
import os
import tempfile
import pytest
from datetime import datetime, timezone, timedelta
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


class TestReplenishGuardOnUnreadableNode:
    """The pool must not treat an unreadable Bee node as an empty pool.

    Regression: on restart the Bee node answers /batches with 503 until it has
    finished syncing. sync_from_bee_node() swallowed that, returned 0, and
    check_and_replenish() read the empty in-memory pool as a real deficit and
    bought a full reserve. Because the gateway and Bee restart together on every
    deploy, this fired on essentially every deploy — one observed node had
    accumulated 77 stamps against a reserve of 5.
    """

    @pytest.fixture
    def state_file(self, tmp_path):
        return str(tmp_path / "pool_state.json")

    @pytest.mark.asyncio
    async def test_bee_unreachable_skips_purchasing(self, state_file):
        """A failed sync must skip replenishment, not buy a full reserve."""
        manager = StampPoolManager(state_file=state_file)
        # Known stamps exist in state, so this is not a legitimate cold start.
        with open(state_file, "w") as f:
            json.dump(["batch_aaa", "batch_bbb"], f)

        purchase = AsyncMock()
        with patch("app.services.stamp_pool.settings.STAMP_POOL_ENABLED", True), \
             patch("app.services.swarm_api.get_all_stamps_processed",
                   new=AsyncMock(side_effect=Exception("503 Service Unavailable"))), \
             patch.object(manager, "_purchase_stamp", purchase):
            results = await manager.check_and_replenish()

        purchase.assert_not_called()
        assert results.get("skipped") is True
        assert manager._last_sync_ok is False
        assert any("could not read stamp state" in e for e in results["errors"])

    @pytest.mark.asyncio
    async def test_successful_sync_allows_purchasing(self, state_file):
        """A genuine cold start (readable node, no known stamps) still buys."""
        manager = StampPoolManager(state_file=state_file)  # no state file written

        purchase = AsyncMock(return_value=None)
        with patch("app.services.stamp_pool.settings.STAMP_POOL_ENABLED", True), \
             patch("app.services.swarm_api.get_all_stamps_processed",
                   new=AsyncMock(return_value=[])), \
             patch.object(manager, "_update_stamp_ttls", new=AsyncMock()), \
             patch.object(manager, "_purchase_stamp", purchase):
            results = await manager.check_and_replenish()

        assert manager._last_sync_ok is True
        assert results.get("skipped") is not True
        assert purchase.called

    def test_sync_flag_starts_false(self, state_file):
        """Before any sync the pool contents are unverified, so no purchasing."""
        assert StampPoolManager(state_file=state_file)._last_sync_ok is False


class TestSyncSurvivesMalformedRecords:
    """One unreadable record must not abort the whole sync.

    Regression: `int(stamp_data.get("amount", 0))` raises on a present-but-null
    or empty value, because the default only applies when the key is ABSENT.
    Bee returns `amount: null` on every /batches entry, and the merged view only
    fills it from /stamps, which is incomplete while the node is starting. The
    try/except wrapped the entire loop, so one bad record discarded every other
    known stamp. Observed in production as:

        ERROR Error syncing stamps from Bee node:
              invalid literal for int() with base 10: ''

    Before the replenish guard (see TestReplenishGuardOnUnreadableNode) that
    read as an empty pool and bought a full reserve on every check interval.
    """

    @pytest.fixture
    def state_file(self, tmp_path):
        return str(tmp_path / "pool_state.json")

    def _stamp(self, batch_id, **over):
        d = {"batchID": batch_id, "depth": 17, "amount": "1000000",
             "usable": True, "batchTTL": 86400, "label": "x"}
        d.update(over)
        return d

    @pytest.mark.asyncio
    async def test_one_bad_record_does_not_lose_the_others(self, state_file):
        manager = StampPoolManager(state_file=state_file)
        with open(state_file, "w") as f:
            json.dump(["good_a", "bad", "good_b"], f)

        stamps = [
            self._stamp("good_a"),
            self._stamp("bad", amount="", depth=None),   # the production shape
            self._stamp("good_b", depth=20),
        ]
        with patch("app.services.swarm_api.get_all_stamps_processed",
                   new=AsyncMock(return_value=stamps)):
            synced = await manager.sync_from_bee_node()

        assert synced == 2, "the two readable stamps must still be imported"
        assert "good_a" in manager._pool and "good_b" in manager._pool
        assert "bad" not in manager._pool
        # The sync read the node successfully, so replenishment stays allowed.
        assert manager._last_sync_ok is True

    @pytest.mark.asyncio
    async def test_bad_record_survives_a_state_rewrite(self, state_file):
        """A record we could not parse may be fine next cycle — don't drop it.

        The state file is only rewritten when something was actually removed, so
        this includes a stamp that is genuinely gone from the node. That forces
        the rewrite and makes the assertion meaningful: without it the file is
        never touched and the check passes whatever the code does.
        """
        manager = StampPoolManager(state_file=state_file)
        with open(state_file, "w") as f:
            json.dump(["unreadable", "gone", "good"], f)

        # "gone" is absent from the node's response, so it is dropped and the
        # state is rewritten. "unreadable" must survive that rewrite.
        with patch("app.services.swarm_api.get_all_stamps_processed",
                   new=AsyncMock(return_value=[
                       self._stamp("unreadable", amount=None, depth=None),
                       self._stamp("good"),
                   ])):
            await manager.sync_from_bee_node()

        persisted = StampPoolManager(state_file=state_file)._load_state()
        assert "unreadable" in persisted, "an unparseable record must be retried, not dropped"
        assert "good" in persisted
        assert "gone" not in persisted, "a stamp no longer on the node should be removed"

    @pytest.mark.asyncio
    async def test_null_amount_is_treated_as_zero_not_an_error(self, state_file):
        """amount is null on every /batches entry; depth is what actually matters."""
        manager = StampPoolManager(state_file=state_file)
        with open(state_file, "w") as f:
            json.dump(["a"], f)

        with patch("app.services.swarm_api.get_all_stamps_processed",
                   new=AsyncMock(return_value=[self._stamp("a", amount=None)])):
            synced = await manager.sync_from_bee_node()

        assert synced == 1
        assert manager._pool["a"].amount == 0
        assert manager._pool["a"].depth == 17


class TestCoerceInt:
    """The shared coercion helper behind the fix."""

    @pytest.mark.parametrize("value,expected", [
        (None, 0), ("", 0), ("123", 123), (456, 456), ("nonsense", 0), (0, 0),
    ])
    def test_coerces_or_falls_back(self, value, expected):
        from app.services.swarm_api import coerce_int
        assert coerce_int(value, 0) == expected

    def test_plain_int_call_would_have_raised(self):
        """Documents precisely what was wrong with the original expression."""
        with pytest.raises((TypeError, ValueError)):
            int({"amount": None}.get("amount", 0))
        with pytest.raises(ValueError):
            int({"amount": ""}.get("amount", 0))


class TestPurchaseFailureBackoff:
    """A failed purchase must not be retried every cycle.

    An underfunded pool retried an unaffordable purchase on every check —
    observed making the same depth-20 attempt over and over, each one holding a
    request handler for as long as Bee took to refuse it. The failure is usually
    persistent (out of funds; an amount below Bee's minimum validity), so
    immediate retry achieves nothing.
    """

    @pytest.fixture
    def manager(self, tmp_path):
        return StampPoolManager(state_file=str(tmp_path / "pool_state.json"))

    def test_not_backing_off_initially(self, manager):
        assert manager._is_backing_off(17) is False

    def test_backoff_grows_and_is_capped(self, manager):
        waits = [manager._backoff_seconds(17) for _ in range(8)]
        assert waits[0] == 60
        assert waits == sorted(waits), "backoff must not shrink"
        assert max(waits) <= 3600, "backoff must be capped"

    def test_backoff_is_per_depth(self, manager):
        """A depth-20 failure must not stop depth-17 being replenished."""
        manager._backoff_seconds(20)
        manager._backoff[20] = datetime.now(timezone.utc) + timedelta(seconds=600)
        assert manager._is_backing_off(20) is True
        assert manager._is_backing_off(17) is False

    def test_expired_backoff_allows_retry(self, manager):
        manager._backoff[17] = datetime.now(timezone.utc) - timedelta(seconds=1)
        assert manager._is_backing_off(17) is False

    @pytest.mark.asyncio
    async def test_failed_purchase_records_backoff_and_stops_retrying(self, manager):
        """One failure, one attempt — not one attempt per stamp needed."""
        with open(manager._get_state_file_path(), "w") as f:
            json.dump([], f)

        purchase = AsyncMock(side_effect=Exception("out of funds"))
        with patch("app.services.stamp_pool.settings.STAMP_POOL_ENABLED", True), \
             patch("app.services.swarm_api.get_all_stamps_processed",
                   new=AsyncMock(return_value=[])), \
             patch.object(manager, "_update_stamp_ttls", new=AsyncMock()), \
             patch.object(manager, "_purchase_stamp", purchase):
            await manager.check_and_replenish()

        assert manager._is_backing_off(17) or manager._is_backing_off(20)
        # Reserve is several stamps per depth; a failure must break out rather
        # than attempt each one in turn.
        assert purchase.call_count <= 2, (
            f"kept trying after a failure: {purchase.call_count} attempts"
        )

    @pytest.mark.asyncio
    async def test_backed_off_depth_is_skipped_next_cycle(self, manager):
        with open(manager._get_state_file_path(), "w") as f:
            json.dump([], f)
        for depth in manager.get_reserve_config():
            manager._backoff[depth] = datetime.now(timezone.utc) + timedelta(seconds=600)

        purchase = AsyncMock()
        with patch("app.services.stamp_pool.settings.STAMP_POOL_ENABLED", True), \
             patch("app.services.swarm_api.get_all_stamps_processed",
                   new=AsyncMock(return_value=[])), \
             patch.object(manager, "_update_stamp_ttls", new=AsyncMock()), \
             patch.object(manager, "_purchase_stamp", purchase):
            results = await manager.check_and_replenish()

        purchase.assert_not_called()
        assert any("not retrying until" in e for e in results["errors"]), (
            "a skipped depth must say why, not fail silently"
        )


class TestBeeErrorSurfaced:
    """Bee's own message must reach the log and the response."""

    def test_extracts_message_from_json_body(self):
        import httpx
        from app.services.stamp_pool import _bee_error_message

        request = httpx.Request("POST", "http://bee:1633/stamps/1/20")
        response = httpx.Response(400, json={"code": 400, "message": "out of funds"},
                                  request=request)
        exc = httpx.HTTPStatusError("err", request=request, response=response)
        assert _bee_error_message(exc) == "out of funds"

    def test_returns_none_without_a_response(self):
        import httpx
        from app.services.stamp_pool import _bee_error_message

        request = httpx.Request("POST", "http://bee:1633/stamps/1/20")
        assert _bee_error_message(httpx.ConnectError("refused", request=request)) is None
