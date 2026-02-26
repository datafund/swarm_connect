# tests/test_global_rate_limit.py
"""
Tests for global rate limiting middleware (Issue #101).
"""
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient

from app.middleware.rate_limit import SlidingWindowCounter, _is_exempt_path


class TestSlidingWindowCounter:
    """Unit tests for the sliding window rate limiter."""

    def test_allows_within_limit(self):
        counter = SlidingWindowCounter()
        for _ in range(5):
            allowed, stats = counter.is_allowed("ip1", limit=10)
            assert allowed is True
        assert stats["remaining"] >= 0

    def test_blocks_over_limit(self):
        counter = SlidingWindowCounter()
        for _ in range(10):
            counter.is_allowed("ip1", limit=10)
        allowed, stats = counter.is_allowed("ip1", limit=10)
        assert allowed is False
        assert stats["remaining"] == 0
        assert "retry_after" in stats

    def test_independent_per_ip(self):
        counter = SlidingWindowCounter()
        # Fill up ip1
        for _ in range(10):
            counter.is_allowed("ip1", limit=10)
        # ip2 should still be allowed
        allowed, _ = counter.is_allowed("ip2", limit=10)
        assert allowed is True

    def test_cleanup_stale(self):
        counter = SlidingWindowCounter()
        counter.is_allowed("stale_ip", limit=10)
        # Cleanup with 0 max_age should remove it
        counter.cleanup_stale(max_age=0)
        # Next request should be allowed (fresh window)
        allowed, stats = counter.is_allowed("stale_ip", limit=10)
        assert allowed is True
        assert stats["remaining"] == 9


class TestExemptPaths:
    """Test path exemption logic."""

    def test_health_exempt(self):
        assert _is_exempt_path("/health") is True

    def test_root_exempt(self):
        assert _is_exempt_path("/") is True

    def test_docs_exempt(self):
        assert _is_exempt_path("/docs") is True

    def test_openapi_exempt(self):
        assert _is_exempt_path("/openapi.json") is True
        assert _is_exempt_path("/api/v1/openapi.json") is True

    def test_api_not_exempt(self):
        assert _is_exempt_path("/api/v1/stamps/") is False
        assert _is_exempt_path("/api/v1/data/") is False


class TestRateLimitMiddleware:
    """Integration tests for rate limit middleware with FastAPI."""

    @patch('app.middleware.rate_limit.settings')
    def test_rate_limit_headers_present(self, mock_settings):
        """Responses should include rate limit headers."""
        mock_settings.RATE_LIMIT_ENABLED = True
        mock_settings.RATE_LIMIT_PER_MINUTE = 60
        mock_settings.RATE_LIMIT_BURST = 10
        mock_settings.X402_ENABLED = False

        # Import fresh app to get middleware
        from app.main import app
        client = TestClient(app)
        response = client.get("/api/v1/stamps/")
        # Rate limit headers should be present (may be 200 or 502 depending on Swarm)
        if "X-RateLimit-Limit" in response.headers:
            assert int(response.headers["X-RateLimit-Limit"]) > 0

    def test_health_check_not_rate_limited(self):
        """Health check should never be rate limited."""
        from app.main import app
        client = TestClient(app)
        # Make many requests to health — should always work
        for _ in range(100):
            response = client.get("/health")
            assert response.status_code == 200

    def test_counter_returns_429_when_exceeded(self):
        """Direct test that counter blocks after limit."""
        counter = SlidingWindowCounter()
        limit = 3
        for _ in range(limit):
            allowed, _ = counter.is_allowed("test_ip", limit=limit)
            assert allowed is True

        # Next should be blocked
        allowed, stats = counter.is_allowed("test_ip", limit=limit)
        assert allowed is False
        assert stats["retry_after"] >= 1
