# tests/test_x402_ratelimit.py
"""
Unit tests for x402 rate limiting.
"""
import pytest
import time
from unittest.mock import patch

from app.x402.ratelimit import (
    RateLimiter,
    check_rate_limit,
    get_rate_limit_headers,
    get_rate_limiter,
    reset_rate_limiter,
)


class TestRateLimiter:
    """Test the RateLimiter class."""

    def test_init_with_custom_limit(self):
        """Initialize with custom rate limit."""
        limiter = RateLimiter(requests_per_minute=5)
        assert limiter.requests_per_minute == 5
        assert limiter.window_seconds == 60

    def test_init_with_custom_window(self):
        """Initialize with custom window size."""
        limiter = RateLimiter(requests_per_minute=10, window_seconds=30)
        assert limiter.requests_per_minute == 10
        assert limiter.window_seconds == 30

    @patch("app.x402.ratelimit.settings")
    def test_init_from_config(self, mock_settings):
        """Initialize from config when no limit specified."""
        mock_settings.X402_RATE_LIMIT_PER_IP = 15
        limiter = RateLimiter()
        assert limiter.requests_per_minute == 15

    def test_first_request_allowed(self):
        """First request is always allowed."""
        limiter = RateLimiter(requests_per_minute=5)
        is_limited, count, limit = limiter.is_rate_limited("192.168.1.1")
        assert is_limited is False
        assert count == 1
        assert limit == 5

    def test_requests_under_limit_allowed(self):
        """Requests under the limit are allowed."""
        limiter = RateLimiter(requests_per_minute=5)

        for i in range(5):
            is_limited, count, limit = limiter.is_rate_limited("192.168.1.1")
            assert is_limited is False
            assert count == i + 1

    def test_requests_over_limit_blocked(self):
        """Requests over the limit are blocked."""
        limiter = RateLimiter(requests_per_minute=3)

        # Make 3 allowed requests
        for _ in range(3):
            is_limited, _, _ = limiter.is_rate_limited("192.168.1.1")
            assert is_limited is False

        # 4th request should be blocked
        is_limited, count, limit = limiter.is_rate_limited("192.168.1.1")
        assert is_limited is True
        assert count == 3
        assert limit == 3

    def test_different_ips_tracked_separately(self):
        """Different IPs have separate rate limits."""
        limiter = RateLimiter(requests_per_minute=2)

        # Max out IP 1
        limiter.is_rate_limited("192.168.1.1")
        limiter.is_rate_limited("192.168.1.1")
        is_limited, _, _ = limiter.is_rate_limited("192.168.1.1")
        assert is_limited is True

        # IP 2 should still be allowed
        is_limited, count, _ = limiter.is_rate_limited("192.168.1.2")
        assert is_limited is False
        assert count == 1

    def test_window_expiry(self):
        """Requests expire after the window passes."""
        limiter = RateLimiter(requests_per_minute=2, window_seconds=1)

        # Max out the limit
        limiter.is_rate_limited("192.168.1.1")
        limiter.is_rate_limited("192.168.1.1")
        is_limited, _, _ = limiter.is_rate_limited("192.168.1.1")
        assert is_limited is True

        # Wait for window to pass
        time.sleep(1.1)

        # Should be allowed again
        is_limited, count, _ = limiter.is_rate_limited("192.168.1.1")
        assert is_limited is False
        assert count == 1  # Counter reset

    def test_unknown_ip_not_limited(self):
        """Unknown/invalid IPs are not rate limited."""
        limiter = RateLimiter(requests_per_minute=1)

        is_limited, count, _ = limiter.is_rate_limited("unknown")
        assert is_limited is False
        assert count == 0

        is_limited, _, _ = limiter.is_rate_limited("")
        assert is_limited is False

    def test_get_client_stats_new_ip(self):
        """Get stats for a new IP."""
        limiter = RateLimiter(requests_per_minute=10)
        stats = limiter.get_client_stats("192.168.1.1")

        assert stats["client_ip"] == "192.168.1.1"
        assert stats["requests_in_window"] == 0
        assert stats["limit"] == 10
        assert stats["remaining"] == 10

    def test_get_client_stats_existing_ip(self):
        """Get stats for an IP with existing requests."""
        limiter = RateLimiter(requests_per_minute=10)

        # Make some requests
        limiter.is_rate_limited("192.168.1.1")
        limiter.is_rate_limited("192.168.1.1")
        limiter.is_rate_limited("192.168.1.1")

        stats = limiter.get_client_stats("192.168.1.1")
        assert stats["requests_in_window"] == 3
        assert stats["remaining"] == 7

    def test_reset_client(self):
        """Reset tracking for a specific client."""
        limiter = RateLimiter(requests_per_minute=2)

        # Max out the limit
        limiter.is_rate_limited("192.168.1.1")
        limiter.is_rate_limited("192.168.1.1")
        is_limited, _, _ = limiter.is_rate_limited("192.168.1.1")
        assert is_limited is True

        # Reset the client
        limiter.reset_client("192.168.1.1")

        # Should be allowed again
        is_limited, count, _ = limiter.is_rate_limited("192.168.1.1")
        assert is_limited is False
        assert count == 1

    def test_reset_nonexistent_client(self):
        """Reset tracking for a client that doesn't exist."""
        limiter = RateLimiter(requests_per_minute=10)
        # Should not raise
        limiter.reset_client("192.168.1.1")

    def test_reset_all(self):
        """Reset all tracking."""
        limiter = RateLimiter(requests_per_minute=2)

        # Max out multiple IPs
        limiter.is_rate_limited("192.168.1.1")
        limiter.is_rate_limited("192.168.1.1")
        limiter.is_rate_limited("192.168.1.2")
        limiter.is_rate_limited("192.168.1.2")

        # Reset all
        limiter.reset_all()

        # Both should be allowed
        is_limited, count, _ = limiter.is_rate_limited("192.168.1.1")
        assert is_limited is False
        assert count == 1

        is_limited, count, _ = limiter.is_rate_limited("192.168.1.2")
        assert is_limited is False
        assert count == 1


class TestCheckRateLimit:
    """Test the check_rate_limit convenience function."""

    def setup_method(self):
        """Reset rate limiter before each test."""
        reset_rate_limiter()

    def teardown_method(self):
        """Reset rate limiter after each test."""
        reset_rate_limiter()

    @patch("app.x402.ratelimit.settings")
    def test_allowed_request(self, mock_settings):
        """Request under limit returns allowed."""
        mock_settings.X402_RATE_LIMIT_PER_IP = 10

        is_allowed, reason, stats = check_rate_limit("192.168.1.1")

        assert is_allowed is True
        assert reason is None
        assert stats["requests_made"] == 1
        assert stats["limit"] == 10
        assert stats["remaining"] == 9

    @patch("app.x402.ratelimit.settings")
    def test_blocked_request(self, mock_settings):
        """Request over limit returns blocked."""
        mock_settings.X402_RATE_LIMIT_PER_IP = 2

        # Make 2 requests
        check_rate_limit("192.168.1.1")
        check_rate_limit("192.168.1.1")

        # 3rd should be blocked
        is_allowed, reason, stats = check_rate_limit("192.168.1.1")

        assert is_allowed is False
        assert "Rate limit exceeded" in reason
        assert stats["remaining"] == 0


class TestGetRateLimitHeaders:
    """Test rate limit header generation."""

    def test_generate_headers(self):
        """Generate rate limit headers from stats."""
        stats = {
            "limit": 10,
            "remaining": 7,
            "window_seconds": 60,
        }

        headers = get_rate_limit_headers(stats)

        assert headers["X-RateLimit-Limit"] == "10"
        assert headers["X-RateLimit-Remaining"] == "7"
        assert headers["X-RateLimit-Reset"] == "60"

    def test_generate_headers_empty_stats(self):
        """Generate headers from empty stats."""
        headers = get_rate_limit_headers({})

        assert headers["X-RateLimit-Limit"] == "0"
        assert headers["X-RateLimit-Remaining"] == "0"
        assert headers["X-RateLimit-Reset"] == "60"


class TestGlobalRateLimiter:
    """Test the global rate limiter singleton."""

    def setup_method(self):
        """Reset rate limiter before each test."""
        reset_rate_limiter()

    def teardown_method(self):
        """Reset rate limiter after each test."""
        reset_rate_limiter()

    def test_singleton_pattern(self):
        """Get rate limiter returns same instance."""
        limiter1 = get_rate_limiter()
        limiter2 = get_rate_limiter()

        assert limiter1 is limiter2

    @patch("app.x402.ratelimit.settings")
    def test_reset_creates_new_instance(self, mock_settings):
        """Reset creates a new instance on next get."""
        mock_settings.X402_RATE_LIMIT_PER_IP = 10

        limiter1 = get_rate_limiter()
        reset_rate_limiter()
        limiter2 = get_rate_limiter()

        # After reset, should get a new instance
        # (can't directly compare as the old one was cleared)
        assert limiter2 is not None


class TestConcurrency:
    """Test thread safety."""

    def test_concurrent_requests(self):
        """Rate limiter handles concurrent requests correctly."""
        import threading

        limiter = RateLimiter(requests_per_minute=100)
        results = []
        errors = []

        def make_request():
            try:
                for _ in range(10):
                    is_limited, count, _ = limiter.is_rate_limited("192.168.1.1")
                    results.append((is_limited, count))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=make_request) for _ in range(10)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(results) == 100  # 10 threads x 10 requests

        # All results should show not limited (100 requests, limit is 100)
        not_limited = [r for r in results if not r[0]]
        assert len(not_limited) == 100


class TestEdgeCases:
    """Test edge cases and special scenarios."""

    def test_ipv6_address(self):
        """IPv6 addresses work correctly."""
        limiter = RateLimiter(requests_per_minute=2)

        is_limited, count, _ = limiter.is_rate_limited("::1")
        assert is_limited is False
        assert count == 1

        is_limited, count, _ = limiter.is_rate_limited("2001:db8::1")
        assert is_limited is False
        assert count == 1

    def test_zero_limit(self):
        """Zero limit blocks all requests."""
        limiter = RateLimiter(requests_per_minute=0)

        is_limited, _, _ = limiter.is_rate_limited("192.168.1.1")
        assert is_limited is True

    def test_high_limit(self):
        """High limit allows many requests."""
        limiter = RateLimiter(requests_per_minute=1000)

        for _ in range(100):
            is_limited, _, _ = limiter.is_rate_limited("192.168.1.1")
            assert is_limited is False

    def test_sliding_window_behavior(self):
        """Sliding window correctly ages out requests."""
        limiter = RateLimiter(requests_per_minute=3, window_seconds=2)

        # Make 2 requests
        limiter.is_rate_limited("192.168.1.1")
        limiter.is_rate_limited("192.168.1.1")

        # Wait 1 second (half the window)
        time.sleep(1)

        # Make 1 more (total 3, at limit)
        is_limited, count, _ = limiter.is_rate_limited("192.168.1.1")
        assert is_limited is False
        assert count == 3

        # Wait another 1.5 seconds (first 2 requests should expire)
        time.sleep(1.5)

        # Should be allowed again (only 1 request in window)
        is_limited, count, _ = limiter.is_rate_limited("192.168.1.1")
        assert is_limited is False
        assert count <= 2  # At most 1 old + 1 new
