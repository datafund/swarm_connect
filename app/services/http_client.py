# app/services/http_client.py
"""
Shared httpx.AsyncClient for all outbound HTTP requests.

Initialized during FastAPI lifespan startup, closed on shutdown.
Provides connection pooling and non-blocking I/O for Swarm Bee API calls.

Self-recovery (#587). httpcore 1.0.9 (the latest release when this was
written) leaks a pool slot when a request is cancelled, or its pool wait times
out, after the pool has handed it a NEW connection but before the request has
used it. The connection stays in the pool in the CONNECTING state (no socket)
or NEW state, is never idle, closed or expired, so nothing removes it, and it
counts against max_connections forever. Under load with client disconnects and
asyncio.wait_for timeouts the pool fills with such slots and every call fails
with PoolTimeout while almost no sockets are open. tests/test_bee_pool_recovery.py
reproduces it.

The shared client's transport counts PoolTimeouts. After
BEE_HTTP_POOL_RECOVERY_THRESHOLD in a row, and when the pool shows enough stuck
slots and a probe of Bee on a new connection answers, the client is replaced
under a lock. The old client is closed in the background once the requests it
is still serving have finished. Call sites take the client from get_client()
for each request, so they move to the new one on their next call.
"""
import asyncio
import logging
import math
import time
from typing import Optional, Set
from urllib.parse import urljoin

import httpx

logger = logging.getLogger(__name__)

_client: Optional[httpx.AsyncClient] = None
_transport: Optional["ObservedTransport"] = None
_generation = 0
_replace_lock: Optional[asyncio.Lock] = None
_recovery_task: Optional[asyncio.Task] = None
_last_recovery_attempt: Optional[float] = None
# Old clients waiting for their in-flight requests before they are closed.
_retiring: Set[asyncio.Task] = set()
_retired_clients: Set[httpx.AsyncClient] = set()

# How often a retiring client is checked for in-flight requests.
RETIRE_POLL_SECONDS = 1.0
# Headroom past the longest Bee request before a retiring client is closed
# even with requests still counted (a response nobody closed).
RETIRE_EXTRA_SECONDS = 60.0
PROBE_TIMEOUT_SECONDS = 5.0


def exc_text(e: BaseException) -> str:
    """The exception for a log line: its type, then its message if it has one.

    str() of an httpx timeout (PoolTimeout, ReadTimeout, ...) is empty, which
    left log lines such as "Error fetching wallet info ...: " during #587.
    """
    message = str(e)
    name = type(e).__name__
    return f"{name}: {message}" if message else name


class _CountedStream(httpx.AsyncByteStream):
    """A response body that marks its request finished when it is closed."""

    def __init__(self, stream, transport: "ObservedTransport") -> None:
        self._stream = stream
        self._transport = transport
        self._done = False

    async def __aiter__(self):
        async for chunk in self._stream:
            yield chunk

    async def aclose(self) -> None:
        try:
            await self._stream.aclose()
        finally:
            if not self._done:
                self._done = True
                self._transport.in_flight -= 1


class ObservedTransport(httpx.AsyncBaseTransport):
    """httpx's transport, counting PoolTimeouts and requests in flight."""

    def __init__(self, limits: httpx.Limits, generation: int) -> None:
        self.inner = httpx.AsyncHTTPTransport(limits=limits)
        self.limits = limits
        self.generation = generation
        self.in_flight = 0
        self.consecutive_pool_timeouts = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.in_flight += 1
        try:
            response = await self.inner.handle_async_request(request)
        except httpx.PoolTimeout:
            self.in_flight -= 1
            self.consecutive_pool_timeouts += 1
            _on_pool_timeout(self)
            raise
        except BaseException:
            self.in_flight -= 1
            raise
        # The pool gave this request a connection, so it is not stuck.
        self.consecutive_pool_timeouts = 0
        response.stream = _CountedStream(response.stream, self)
        return response

    async def aclose(self) -> None:
        await self.inner.aclose()


def pool_snapshot(transport: Optional[ObservedTransport]) -> Optional[dict]:
    """Connections and requests in the transport's httpcore pool, or None.

    Reads httpx's private AsyncHTTPTransport._pool and httpcore's
    pool._requests; any change there makes this return None instead of
    raising. A connection is "stuck" when it is neither idle nor closed and no
    request in the pool holds it: the slots leaked by #587.
    """
    if transport is None:
        return None
    try:
        pool = transport.inner._pool
        connections = list(pool.connections)
        held = {id(r.connection) for r in list(pool._requests) if r.connection is not None}
        idle = stuck = active = 0
        for conn in connections:
            if conn.is_closed():
                continue
            if conn.is_idle():
                idle += 1
            elif id(conn) in held:
                active += 1
            else:
                stuck += 1
        return {
            "active": active,
            "idle": idle,
            "stuck": stuck,
            "requests": len(pool._requests),
        }
    except Exception as e:
        logger.debug(f"Could not read the HTTP client pool: {exc_text(e)}")
        return None


def current_pool_snapshot() -> Optional[dict]:
    """pool_snapshot() of the current shared client."""
    return pool_snapshot(_transport)


def _limits():
    from app.core.config import settings
    return httpx.Limits(
        max_connections=settings.BEE_HTTP_MAX_CONNECTIONS,
        max_keepalive_connections=settings.BEE_HTTP_MAX_KEEPALIVE_CONNECTIONS,
        keepalive_expiry=settings.BEE_HTTP_KEEPALIVE_EXPIRY_SECONDS,
    )


def _new_client() -> "tuple[httpx.AsyncClient, ObservedTransport]":
    """A new shared client and its transport.

    Passing transport= means httpx does not apply proxies from the
    HTTP_PROXY, HTTPS_PROXY or ALL_PROXY environment variables to this
    client. That is intended: Bee is on the compose network and must be
    reached directly.
    """
    global _generation
    _generation += 1
    transport = ObservedTransport(_limits(), _generation)
    from app.services import bee_gate  # cap on Bee calls in flight (#588)
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, connect=5.0),
        follow_redirects=True,
        transport=bee_gate.wrap(transport),
    )
    return client, transport


async def init_client():
    """Initialize the shared HTTP client. Called during app startup."""
    global _client, _transport, _replace_lock, _last_recovery_attempt
    _client, _transport = _new_client()
    _replace_lock = asyncio.Lock()
    _last_recovery_attempt = None
    logger.info("Initialized shared httpx.AsyncClient")


async def close_client():
    """Close the shared HTTP client. Called during app shutdown.

    Also closes old clients still waiting for their requests, and stops a
    recovery in progress.
    """
    global _client, _transport, _recovery_task
    task = _recovery_task
    _recovery_task = None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except BaseException:
            pass
    for retiring in list(_retiring):
        retiring.cancel()
    for retiring in list(_retiring):
        try:
            await retiring
        except BaseException:
            pass
    for old in list(_retired_clients):
        _retired_clients.discard(old)
        try:
            await old.aclose()
        except Exception as e:
            logger.warning(f"Could not close a replaced HTTP client: {exc_text(e)}")
    if _client:
        await _client.aclose()
        _client = None
        _transport = None
        logger.info("Closed shared httpx.AsyncClient")


def get_client() -> httpx.AsyncClient:
    """Get the shared HTTP client instance."""
    if _client is None:
        raise RuntimeError("HTTP client not initialized — call init_client() first")
    return _client


def _count(metric: str, **labels) -> None:
    """Increment a recovery metric; metrics must never break a request."""
    try:
        from app.services import metrics
        counter = getattr(metrics, metric)
        (counter.labels(**labels) if labels else counter).inc()
    except Exception as e:  # pragma: no cover - defensive
        logger.debug(f"Metrics: could not count {metric}: {exc_text(e)}")


def _on_pool_timeout(transport: ObservedTransport) -> None:
    """Count a PoolTimeout and start a recovery check when one is due."""
    global _recovery_task
    _count("bee_http_pool_timeouts_total")
    from app.core.config import settings
    if transport is not _transport:
        return  # an old client still finishing its requests
    if transport.consecutive_pool_timeouts < settings.BEE_HTTP_POOL_RECOVERY_THRESHOLD:
        return
    if _recovery_task is not None and not _recovery_task.done():
        return
    if not _cooldown_passed():
        return
    try:
        _recovery_task = asyncio.get_running_loop().create_task(
            recover_pool(transport.generation)
        )
    except RuntimeError:  # pragma: no cover - no running loop
        pass


def _cooldown_passed() -> bool:
    from app.core.config import settings
    if _last_recovery_attempt is None:
        return True
    return time.monotonic() - _last_recovery_attempt >= settings.BEE_HTTP_POOL_RECOVERY_COOLDOWN_SECONDS


async def probe_bee() -> bool:
    """True when Bee answers GET /health on a new connection (status < 500)."""
    from app.core.config import settings
    url = urljoin(str(settings.SWARM_BEE_API_URL), "health")
    try:
        # trust_env=False: no proxy from HTTP(S)_PROXY/ALL_PROXY, the same
        # path as the shared client, whose explicit transport ignores them.
        async with httpx.AsyncClient(timeout=httpx.Timeout(PROBE_TIMEOUT_SECONDS),
                                     trust_env=False) as probe:
            response = await probe.get(url)
        return response.status_code < 500
    except Exception as e:
        logger.warning(f"HTTP client recovery: Bee probe on a new connection failed: {exc_text(e)}")
        return False


async def recover_pool(generation: int) -> str:
    """Replace the shared client if its pool is stuck and Bee answers.

    Returns the outcome, also counted in
    gateway_bee_http_client_replacements_total{result}: "replaced",
    "replaced_unverified" (the pool could not be read, so the probe alone
    decided), "probe_failed", "not_stuck", or "skipped" (another replacement
    or the cooldown got there first; not counted).
    """
    global _client, _transport, _last_recovery_attempt, _replace_lock
    from app.core.config import settings
    if _replace_lock is None:
        _replace_lock = asyncio.Lock()
    async with _replace_lock:
        transport = _transport
        if _client is None or transport is None or transport.generation != generation:
            return "skipped"
        if not _cooldown_passed():
            return "skipped"
        _last_recovery_attempt = time.monotonic()
        transport.consecutive_pool_timeouts = 0

        snapshot = pool_snapshot(transport)
        if snapshot is not None:
            needed = max(1, math.ceil(
                settings.BEE_HTTP_POOL_STUCK_FRACTION * transport.limits.max_connections
            ))
            if snapshot["stuck"] < needed:
                logger.warning(
                    f"HTTP client recovery: PoolTimeouts, but only {snapshot['stuck']} stuck "
                    f"connections ({needed} needed); the pool is busy, not stuck. Not replacing. "
                    f"Pool: {snapshot}"
                )
                _count("bee_http_client_replacements_total", result="not_stuck")
                return "not_stuck"

        if not await probe_bee():
            logger.error(
                "HTTP client recovery: the shared client's pool times out and Bee does not "
                "answer on a new connection either; not replacing the client"
            )
            _count("bee_http_client_replacements_total", result="probe_failed")
            return "probe_failed"

        old_client = _client
        _client, _transport = _new_client()
        result = "replaced" if snapshot is not None else "replaced_unverified"
        _count("bee_http_client_replacements_total", result=result)
        logger.error(
            f"HTTP client recovery: replaced the shared HTTP client ({result}). Pool before: "
            f"{snapshot if snapshot is not None else 'unreadable'}; "
            f"{transport.in_flight} requests still in flight on the old client"
        )
        _retire(old_client, transport)
        return result


def _retire(client: httpx.AsyncClient, transport: ObservedTransport) -> None:
    """Close an old client once its in-flight requests finish, without blocking."""
    _retired_clients.add(client)
    task = asyncio.get_running_loop().create_task(_close_when_drained(client, transport))
    _retiring.add(task)
    task.add_done_callback(_retiring.discard)


async def _close_when_drained(client: httpx.AsyncClient, transport: ObservedTransport) -> None:
    from app.core.config import settings
    cap = max(
        float(settings.STAMP_PURCHASE_BEE_TIMEOUT_SECONDS),
        float(settings.DOWNLOAD_TIMEOUT_SECONDS),
    ) + RETIRE_EXTRA_SECONDS
    deadline = time.monotonic() + cap
    while transport.in_flight > 0 and time.monotonic() < deadline:
        await asyncio.sleep(RETIRE_POLL_SECONDS)
    if transport.in_flight > 0:
        logger.warning(
            f"Closing a replaced HTTP client with {transport.in_flight} requests still counted "
            f"after {cap:.0f} s"
        )
    _retired_clients.discard(client)
    try:
        await client.aclose()
    except Exception as e:
        logger.warning(f"Could not close a replaced HTTP client: {exc_text(e)}")
