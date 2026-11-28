"""
Global rate limiting middleware using in-memory sliding window.

Provides per-IP rate limiting on every non-exempt route, whether or not
x402 is enabled. The x402 free-tier limit applies on top for free writes.
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
EXEMPT_PATHS = {"/", "/health", "/docs", "/redoc", "/openapi.json", "/metrics",
                f"{settings.API_V1_STR}/openapi.json"}

# Pre-stamped chunk forwarding. Each request carries one ~4 KB chunk, so a
# per-request limit sized for API calls throttles it to a few hundred KB a
# minute. A chunk upload that spends prepaid bandwidth credit is bounded by that
# credit instead, so it is exempt (#696). Only such uploads are: the exemption
# used to cover every POST here when x402 was on, before the endpoint had
# checked anything, so requests with no token, a bad token or the free path
# were not limited at all.
#
# The exemption is from the per-request count, not from every limit. Each
# exempted upload reserves a slot under its own key (the global limit's size)
# before it runs, and is refused with 429 when there is none. Once answered,
# a billed upload gives its slot back: the endpoint sets
# request.state.chunk_billed only after the forward succeeded, so a refused or
# refunded upload, an exception or a cancellation keeps it. So a funded token
# cannot drive unlimited Bee calls with stamps Bee rejects, even concurrently.
# The cost: one address can have at most the limit's worth of credit-token
# uploads in flight at once.
#
# Only the route's exact path: other spellings (no trailing slash, which only
# redirects, or extra slashes, which are 404) are not the upload route.
CHUNK_UPLOAD_PATH = f"{settings.API_V1_STR}/chunks/"
CHUNK_BILLED_STATE = "chunk_billed"
_UNBILLED_CHUNK_KEY_SUFFIX = "|unbilled-chunk-uploads"


def _is_exempt_path(path: str) -> bool:
    """Check if path is exempt from rate limiting."""
    return path in EXEMPT_PATHS


def _declared_length(request: Request) -> int:
    """The body length the request declares, or 0 when it declares none."""
    try:
        return max(0, int(request.headers.get("content-length") or 0))
    except ValueError:
        return 0


def _is_credit_funded_chunk_upload(request: Request) -> bool:
    """Whether this is a chunk upload that prepaid credit will pay for.

    True only with x402 on, for POST /chunks/ outside the free path, when the
    credit token resolves to an account holding at least the declared body
    length (at least one byte without a Content-Length). Two in-memory lookups:
    nothing is debited and the body is not read. The endpoint still validates
    and debits; one that ends unbilled is counted afterwards (see above).
    """
    if not (
        settings.X402_ENABLED
        and request.method == "POST"
        and request.url.path == CHUNK_UPLOAD_PATH
    ):
        return False
    from app.x402.middleware import is_free_tier_opt_in
    if is_free_tier_opt_in(request):
        return False
    from app.services.bandwidth_credit import CREDIT_TOKEN_HEADER, bandwidth_credit_manager
    address = bandwidth_credit_manager.resolve_token(request.headers.get(CREDIT_TOKEN_HEADER) or "")
    if not address:
        return False
    return bandwidth_credit_manager.balance(address) >= max(_declared_length(request), 1)


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
        entry, stats = self.reserve(key, limit, window_seconds)
        return entry is not None, stats

    def reserve(self, key: str, limit: int, window_seconds: int = 60) -> Tuple[Optional[float], dict]:
        """is_allowed that also returns the entry it added, for release().

        Returns (entry, stats); entry is None when the request is refused.
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
                return now, stats
            else:
                oldest = self._windows[key][0] if self._windows[key] else now
                retry_after = int(oldest + window_seconds - now) + 1
                stats = {
                    "remaining": 0,
                    "limit": limit,
                    "retry_after": max(1, retry_after),
                }
                return None, stats

    def release(self, key: str, entry: float) -> None:
        """Remove exactly the entry reserve() returned, if it is still there.

        Matched by identity, not value, so another request's entry with the
        same timestamp is never the one removed.
        """
        with self._lock:
            window = self._windows.get(key)
            if not window:
                return
            for i, t in enumerate(window):
                if t is entry:
                    del window[i]
                    return

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
from app.core.client_ip import client_key, get_client_ip  # noqa: F401,E402


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Global rate limiting middleware.

    Applies per-IP sliding window rate limiting to all non-exempt endpoints,
    with or without x402.
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
        key = client_key(client_ip)

        if _is_credit_funded_chunk_upload(request):
            return await self._credit_funded_chunk_upload(request, call_next, client_ip, key, limit)

        allowed, stats = self._counter.is_allowed(key, limit)

        if not allowed:
            return _too_many(request, client_ip, stats)

        response = await call_next(request)

        # Add rate limit headers, unless an inner limiter already did: the x402
        # free-tier limit is stricter, and its headers are the ones that tell a
        # free caller when it may write again.
        response.headers.setdefault("X-RateLimit-Limit", str(stats["limit"]))
        response.headers.setdefault("X-RateLimit-Remaining", str(stats["remaining"]))

        return response

    async def _credit_funded_chunk_upload(self, request, call_next, client_ip, key, limit):
        """Reserve a slot under the upload key first; give it back once billed."""
        unbilled_key = key + _UNBILLED_CHUNK_KEY_SUFFIX
        entry, stats = self._counter.reserve(unbilled_key, limit)
        if entry is None:
            return _too_many(request, client_ip, stats)
        try:
            return await call_next(request)
        finally:
            # Only a billed upload releases it. Anything else (a refusal, a
            # refunded Bee failure, an exception, a cancellation) stays counted.
            if getattr(request.state, CHUNK_BILLED_STATE, False):
                self._counter.release(unbilled_key, entry)


def _too_many(request: Request, client_ip: str, stats: dict) -> JSONResponse:
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
