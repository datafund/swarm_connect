# app/x402/ratelimit.py
"""
Rate limiting for x402 payment gateway.

This module provides IP-based rate limiting to prevent abuse of the gateway.
Uses a sliding window algorithm with in-memory storage.

Configuration:
- X402_RATE_LIMIT_PER_IP: Maximum requests per minute per IP (default: 10)

Rate limiting is applied BEFORE access control checks and payment verification.
This protects the gateway from DoS attacks even when x402 is disabled.
"""
import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from app.core.config import settings

logger = logging.getLogger(__name__)


@dataclass
class RateLimitWindow:
    """Stores request timestamps for a single IP within the sliding window."""
    requests: List[float] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


class RateLimiter:
    """
    In-memory sliding window rate limiter.

    Uses a sliding window of 60 seconds to track requests per IP.
    Thread-safe for concurrent access.
    """

    def __init__(
        self,
        requests_per_minute: Optional[int] = None,
        window_seconds: int = 60
    ):
        """
        Initialize the rate limiter.

        Args:
            requests_per_minute: Max requests allowed per window. If None, uses config.
            window_seconds: Size of the sliding window in seconds.
        """
        self._requests_per_minute = requests_per_minute
        self._window_seconds = window_seconds
        self._windows: Dict[str, RateLimitWindow] = defaultdict(RateLimitWindow)
        self._cleanup_lock = threading.Lock()
        self._last_cleanup = time.time()

    @property
    def requests_per_minute(self) -> int:
        """Get the rate limit (lazy load from settings if not set)."""
        if self._requests_per_minute is not None:
            return self._requests_per_minute
        return settings.X402_RATE_LIMIT_PER_IP

    @property
    def window_seconds(self) -> int:
        """Get the window size in seconds."""
        return self._window_seconds

    def is_rate_limited(self, client_ip: str) -> Tuple[bool, int, int]:
        """
        Check if a client IP is rate limited.

        Args:
            client_ip: The client's IP address

        Returns:
            Tuple of (is_limited, requests_made, limit):
            - is_limited: True if the client should be blocked
            - requests_made: Number of requests in current window
            - limit: The configured rate limit
        """
        if not client_ip or client_ip == "unknown":
            # Don't rate limit unknown IPs (they'll fail other checks)
            return (False, 0, self.requests_per_minute)

        now = time.time()
        window_start = now - self._window_seconds

        # Periodically cleanup old entries
        self._maybe_cleanup(now)

        window = self._windows[client_ip]

        with window.lock:
            # Remove old requests outside the window
            window.requests = [
                ts for ts in window.requests
                if ts > window_start
            ]

            requests_in_window = len(window.requests)
            limit = self.requests_per_minute

            if requests_in_window >= limit:
                logger.warning(
                    f"Rate limit exceeded for {client_ip}: "
                    f"{requests_in_window}/{limit} requests in {self._window_seconds}s"
                )
                return (True, requests_in_window, limit)

            # Record this request
            window.requests.append(now)

            return (False, requests_in_window + 1, limit)

    def get_client_stats(self, client_ip: str) -> Dict[str, any]:
        """
        Get rate limit statistics for a client IP.

        Args:
            client_ip: The client's IP address

        Returns:
            Dict with current request count, limit, and window info
        """
        now = time.time()
        window_start = now - self._window_seconds

        if client_ip not in self._windows:
            return {
                "client_ip": client_ip,
                "requests_in_window": 0,
                "limit": self.requests_per_minute,
                "window_seconds": self._window_seconds,
                "remaining": self.requests_per_minute,
            }

        window = self._windows[client_ip]

        with window.lock:
            # Count requests in window without modifying
            requests_in_window = len([
                ts for ts in window.requests
                if ts > window_start
            ])

        return {
            "client_ip": client_ip,
            "requests_in_window": requests_in_window,
            "limit": self.requests_per_minute,
            "window_seconds": self._window_seconds,
            "remaining": max(0, self.requests_per_minute - requests_in_window),
        }

    def reset_client(self, client_ip: str) -> None:
        """
        Reset rate limit tracking for a client IP.

        Args:
            client_ip: The client's IP address to reset
        """
        if client_ip in self._windows:
            window = self._windows[client_ip]
            with window.lock:
                window.requests.clear()
            logger.debug(f"Reset rate limit for {client_ip}")

    def reset_all(self) -> None:
        """Reset all rate limit tracking."""
        self._windows.clear()
        logger.info("Reset all rate limits")

    def _maybe_cleanup(self, now: float) -> None:
        """
        Periodically clean up stale entries to prevent memory growth.

        Runs cleanup every 5 minutes.
        """
        cleanup_interval = 300  # 5 minutes

        if now - self._last_cleanup < cleanup_interval:
            return

        with self._cleanup_lock:
            # Double-check after acquiring lock
            if now - self._last_cleanup < cleanup_interval:
                return

            self._last_cleanup = now
            window_start = now - self._window_seconds
            stale_ips = []

            for ip, window in self._windows.items():
                with window.lock:
                    # Remove old requests
                    window.requests = [
                        ts for ts in window.requests
                        if ts > window_start
                    ]
                    # Mark for removal if empty
                    if not window.requests:
                        stale_ips.append(ip)

            # Remove empty windows
            for ip in stale_ips:
                del self._windows[ip]

            if stale_ips:
                logger.debug(f"Cleaned up {len(stale_ips)} stale rate limit entries")


# Global rate limiter instance
_rate_limiter: Optional[RateLimiter] = None
_rate_limiter_lock = threading.Lock()


def get_rate_limiter() -> RateLimiter:
    """
    Get the global rate limiter instance.

    Returns:
        The singleton RateLimiter instance
    """
    global _rate_limiter

    if _rate_limiter is None:
        with _rate_limiter_lock:
            if _rate_limiter is None:
                _rate_limiter = RateLimiter()

    return _rate_limiter


def check_rate_limit(client_ip: str, is_free_tier: bool = False) -> Tuple[bool, Optional[str], Dict[str, any]]:
    """
    Check if a client IP is rate limited.

    This is the main entry point for rate limiting in the middleware.

    Args:
        client_ip: The client's IP address
        is_free_tier: If True, use stricter free tier rate limit

    Returns:
        Tuple of (is_allowed, reason, stats):
        - is_allowed: True if the request should proceed
        - reason: Error message if rate limited, None otherwise
        - stats: Rate limit statistics for headers
    """
    limiter = get_rate_limiter()

    # Use appropriate limit based on tier
    if is_free_tier:
        effective_limit = settings.X402_FREE_TIER_RATE_LIMIT
    else:
        effective_limit = limiter.requests_per_minute

    is_limited, requests_made, _ = limiter.is_rate_limited(client_ip)

    # For free tier, check against the stricter limit
    if is_free_tier and requests_made > effective_limit:
        is_limited = True

    stats = {
        "requests_made": requests_made,
        "limit": effective_limit,
        "remaining": max(0, effective_limit - requests_made),
        "window_seconds": limiter.window_seconds,
        "is_free_tier": is_free_tier,
    }

    if is_limited:
        tier_name = "free tier" if is_free_tier else "paid"
        return (
            False,
            f"Rate limit exceeded ({tier_name}): {requests_made}/{effective_limit} requests per minute",
            stats
        )

    return (True, None, stats)


def get_rate_limit_headers(stats: Dict[str, any]) -> Dict[str, str]:
    """
    Generate rate limit headers for HTTP responses.

    Args:
        stats: Rate limit statistics from check_rate_limit

    Returns:
        Dict of HTTP headers to add to the response
    """
    return {
        "X-RateLimit-Limit": str(stats.get("limit", 0)),
        "X-RateLimit-Remaining": str(stats.get("remaining", 0)),
        "X-RateLimit-Reset": str(stats.get("window_seconds", 60)),
    }


def reset_rate_limiter() -> None:
    """Reset the global rate limiter (useful for testing)."""
    global _rate_limiter
    with _rate_limiter_lock:
        if _rate_limiter is not None:
            _rate_limiter.reset_all()
        _rate_limiter = None
