# tests/test_http_client.py
"""Tests for the shared httpx.AsyncClient lifecycle."""
import pytest
import httpx
from unittest.mock import patch, AsyncMock

from app.services.http_client import init_client, close_client, get_client, _client


class TestClientLifecycle:
    """Tests for init/close/get lifecycle."""

    @pytest.fixture(autouse=True)
    def reset_client(self):
        """Reset the global client state before and after each test."""
        import app.services.http_client as mod
        original = mod._client
        mod._client = None
        yield
        # Restore original to avoid polluting other tests
        mod._client = original

    def test_get_client_before_init_raises(self):
        """get_client() should raise RuntimeError when not initialized."""
        with pytest.raises(RuntimeError, match="HTTP client not initialized"):
            get_client()

    @pytest.mark.asyncio
    async def test_init_creates_client(self):
        """init_client() should create an httpx.AsyncClient."""
        import app.services.http_client as mod
        await init_client()
        try:
            client = get_client()
            assert isinstance(client, httpx.AsyncClient)
        finally:
            await close_client()

    @pytest.mark.asyncio
    async def test_close_sets_client_to_none(self):
        """close_client() should set client to None."""
        import app.services.http_client as mod
        await init_client()
        await close_client()
        assert mod._client is None

    @pytest.mark.asyncio
    async def test_close_without_init_is_safe(self):
        """close_client() should be safe to call when client is None."""
        await close_client()  # Should not raise

    @pytest.mark.asyncio
    async def test_get_client_after_close_raises(self):
        """get_client() should raise after close_client()."""
        await init_client()
        await close_client()
        with pytest.raises(RuntimeError, match="HTTP client not initialized"):
            get_client()

    @pytest.mark.asyncio
    async def test_client_timeout_configuration(self):
        """Client should have correct timeout settings."""
        await init_client()
        try:
            client = get_client()
            assert client.timeout.connect == 5.0
            assert client.timeout.read == 10.0
            assert client.timeout.write == 10.0
        finally:
            await close_client()


class TestGatherFailureBehavior:
    """Tests for asyncio.gather failure scenarios in service functions."""

    @pytest.mark.asyncio
    @patch('app.services.swarm_api.get_local_stamps')
    @patch('app.services.swarm_api.get_all_stamps')
    async def test_gather_global_fails_local_succeeds(self, mock_global, mock_local):
        """When get_all_stamps raises, the error should propagate."""
        from app.services.swarm_api import get_all_stamps_processed

        mock_global.side_effect = httpx.HTTPError("Bee node unreachable")
        mock_local.return_value = [{"batchID": "abc", "usable": True}]

        with pytest.raises(httpx.HTTPError):
            await get_all_stamps_processed()

    @pytest.mark.asyncio
    @patch('app.services.swarm_api.get_local_stamps')
    @patch('app.services.swarm_api.get_all_stamps')
    async def test_gather_local_fails_global_succeeds(self, mock_global, mock_local):
        """When get_local_stamps raises, the error should propagate."""
        from app.services.swarm_api import get_all_stamps_processed

        mock_global.return_value = [{"batchID": "abc", "depth": 17, "bucketDepth": 16, "batchTTL": 86400, "amount": "1000"}]
        mock_local.side_effect = httpx.HTTPError("Stamps endpoint down")

        with pytest.raises(httpx.HTTPError):
            await get_all_stamps_processed()

    @pytest.mark.asyncio
    @patch('app.services.swarm_api.get_local_stamps')
    @patch('app.services.swarm_api.get_all_stamps')
    async def test_gather_both_succeed(self, mock_global, mock_local):
        """Normal case: both calls succeed and data is merged."""
        from app.services.swarm_api import get_all_stamps_processed

        mock_global.return_value = [{"batchID": "abc123", "depth": 17, "bucketDepth": 16, "batchTTL": 86400, "amount": "1000"}]
        mock_local.return_value = [{"batchID": "abc123", "usable": True, "utilization": 1}]

        result = await get_all_stamps_processed()
        assert len(result) == 1
        assert result[0]["batchID"] == "abc123"
        assert result[0]["usable"] is True
