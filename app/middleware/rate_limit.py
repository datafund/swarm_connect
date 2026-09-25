"""
Global rate limiting middleware using in-memory sliding window.

Provides per-IP rate limiting independent of x402 payment middleware.
When x402 is enabled, this middleware is skipped (x402 has its own limiter).
"""
import logging
import time
from collections import defaultdict
from threading import Lock
from typing import Optional, Tuple

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.core.config import settings
from app.services.metrics import rate_limit_hits_total

logger = logging.getLogger(__name__)

# Paths exempt from rate limiting
EXEMPT_PATHS = {"/", "/health", "/docs", "/redoc", "/openapi.json", "/metrics"}


def _is_exempt_path(path: str) -> bool:
    """Check if path is exempt from rate limiting."""
    # Exact match for exempt paths
    if path in EXEMPT_PATHS:
        return True
    # OpenAPI spec path
    if path.endswith("/openapi.json"):
        return True
    return False


class SlidingWindowCounter:
    """Thread-safe sliding window rate limiter."""

    def __init__(self):
        self._windows: dict = defaultdict(list)
        self._lock = Lock()
        self._last_cleanup = time.monotonic()

    def is_allowed(self, key: str, limit: int, window_seconds: int = 60) -> Tuple[bool, dict]:
        """
        Check if a request is allowed under the rate limit.

        Returns:
            Tuple of (allowed: bool, stats: dict with remaining, reset_at, limit)
        """
        now = time.monotonic()
        cutoff = now - window_seconds

        with self._lock:
            # Remove expired entries
            self._windows[key] = [t for t in self._windows[key] if t > cutoff]

            current_count = len(self._windows[key])

            if current_count < limit:
                self._windows[key].append(now)
                stats = {
                    "remaining": limit - current_count - 1,
                    "limit": limit,
                    "reset_after": int(window_seconds - (now - self._windows[key][0]) if self._windows[key] else window_seconds),
                }
                return True, stats
            else:
                oldest = self._windows[key][0] if self._windows[key] else now
                retry_after = int(oldest + window_seconds - now) + 1
                stats = {
                    "remaining": 0,
                    "limit": limit,
                    "retry_after": max(1, retry_after),
                }
                return False, stats

    def cleanup_stale(self, max_age: float = 300):
        """Remove entries for IPs that haven't made requests recently."""
        now = time.monotonic()
        cutoff = now - max_age

        with self._lock:
            stale_keys = [
                k for k, timestamps in self._windows.items()
                if not timestamps or max(timestamps) < cutoff
            ]
            for key in stale_keys:
                del self._windows[key]

            if stale_keys:
                logger.debug(f"Rate limiter cleanup: removed {len(stale_keys)} stale entries")


# Module-level instance
_counter = SlidingWindowCounter()


# Single implementation in app/core/client_ip. Re-exported here because
# callers and tests import it from this module. Both copies used to take the
# FIRST X-Forwarded-For entry, which is the one furthest from us and is
# caller-controlled if any proxy appends rather than replaces.
from app.core.client_ip import get_client_ip  # noqa: F401,E402


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Global rate limiting middleware.

    Applies per-IP sliding window rate limiting to all non-exempt endpoints.
    Skipped when x402 is enabled (x402 has its own rate limiter).
    """

    def __init__(self, app, counter: Optional[SlidingWindowCounter] = None):
        super().__init__(app)
        self._counter = counter or _counter
        self._cleanup_interval = 60  # seconds
        self._last_cleanup = time.monotonic()

    async def dispatch(self, request: Request, call_next) -> Response:
        # Skip if rate limiting is disabled
        if not settings.RATE_LIMIT_ENABLED:
            return await call_next(request)

        # Skip exempt paths
        if _is_exempt_path(request.url.path):
            return await call_next(request)

        # Periodic cleanup of stale entries
        now = time.monotonic()
        if now - self._last_cleanup > self._cleanup_interval:
            self._counter.cleanup_stale()
            self._last_cleanup = now

        client_ip = get_client_ip(request)
        limit = settings.RATE_LIMIT_PER_MINUTE + settings.RATE_LIMIT_BURST

        allowed, stats = self._counter.is_allowed(client_ip, limit)

        if not allowed:
            rate_limit_hits_total.inc()
            retry_after = stats.get("retry_after", 60)
            logger.warning(f"Rate limit exceeded for {client_ip}: {request.method} {request.url.path}")
            return JSONResponse(
                status_code=429,
                content={
                    "error": "Rate limit exceeded",
                    "detail": f"Too many requests. Try again in {retry_after} seconds.",
                    "retry_after": retry_after,
                    # Same top-level code/message as every other error (#381).
                    "code": "RATE_LIMIT_EXCEEDED",
                    "message": f"Too many requests. Try again in {retry_after} seconds.",
                },
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(stats["limit"]),
                    "X-RateLimit-Remaining": "0",
                }
            )

        response = await call_next(request)

        # Add rate limit headers to successful responses
        response.headers["X-RateLimit-Limit"] = str(stats["limit"])
        response.headers["X-RateLimit-Remaining"] = str(stats["remaining"])

        return response
