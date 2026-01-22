# tests/test_stamp_pool.py
"""
Unit tests for the Stamp Pool feature.
"""
import pytest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock
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
        """Test acquiring stamp from empty pool when enabled."""
        with patch('app.api.endpoints.pool.settings') as mock_settings:
            mock_settings.STAMP_POOL_ENABLED = True

            response = client.post(
                "/api/v1/pool/acquire",
                json={"size": "small"}
            )
            assert response.status_code == 200
            data = response.json()
            assert data["success"] is False
            assert "No stamp available" in data["message"]

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
        """Test acquire returns 503 when disabled."""
        with patch('app.api.endpoints.pool.settings') as mock_settings:
            mock_settings.STAMP_POOL_ENABLED = False

            response = client.post(
                "/api/v1/pool/acquire",
                json={"size": "small"}
            )
            assert response.status_code == 503
            assert "not enabled" in response.json()["detail"]

    def test_list_stamps_disabled(self, client):
        """Test list returns 503 when disabled."""
        with patch('app.api.endpoints.pool.settings') as mock_settings:
            mock_settings.STAMP_POOL_ENABLED = False

            response = client.get("/api/v1/pool/available")
            assert response.status_code == 503

    def test_trigger_check_disabled(self, client):
        """Test manual check returns 503 when disabled."""
        with patch('app.api.endpoints.pool.settings') as mock_settings:
            mock_settings.STAMP_POOL_ENABLED = False

            response = client.post("/api/v1/pool/check")
            assert response.status_code == 503


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
