# app/middleware/client_metrics.py
"""Record which client types the gateway serves, and whether it serves them.

See app/services/client_type.py for why the client type comes from a fixed table
and why no per-caller identity is exported.
"""
import hashlib
import logging
import os
from datetime import datetime, timezone
from threading import Lock
from typing import Set

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.client_ip import get_client_ip
from app.services.client_type import classify, outcome_for
from app.services.metrics import distinct_callers, requests_by_client_total

logger = logging.getLogger(__name__)

# Paths that are not a client being served, and would otherwise dominate the
# counter: Alloy scrapes /metrics every 15s and Docker healthchecks / every 30s,
# which together are the two most frequent requests the gateway ever sees.
EXCLUDED_PATHS = frozenset({"/metrics", "/health", "/", "/docs", "/redoc", "/openapi.json", "/api/v1/openapi.json"})


class _DistinctCallerCounter:
    """Counts distinct callers per UTC day without keeping their addresses.

    Counting distinctness needs to recognise a repeat caller, which needs some
    per-caller value in memory. A salted hash does that and nothing else: the
    salt is random per process, so the stored values cannot be reversed to an
    address, cannot be compared against a precomputed table, and cannot be
    correlated across restarts or between the two gateways.

    Only the COUNT leaves this class. Nothing is persisted — a restart resets the
    day's figure, which is the right trade for not writing caller identities to
    disk in order to make a graph slightly smoother.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._salt = os.urandom(16)
        self._day = self._today()
        self._seen: Set[bytes] = set()

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def observe(self, caller: str) -> None:
        # blake2b truncated to 8 bytes: collisions are irrelevant at this scale
        # and it keeps the set small for a long-running process.
        digest = hashlib.blake2b(caller.encode(), key=self._salt, digest_size=8).digest()
        with self._lock:
            today = self._today()
            if today != self._day:
                self._day = today
                self._seen = set()
            self._seen.add(digest)
            count = len(self._seen)
        distinct_callers.set(count)


distinct_caller_counter = _DistinctCallerCounter()


class ClientMetricsMiddleware:
    """Increments the client-type counters for each served API request.

    Written as plain ASGI rather than subclassing BaseHTTPMiddleware. That base
    class wraps each request in an anyio task group and re-plumbs the
    request/response streams, which deadlocks the whole test suite when stacked
    behind the other middleware here — the first attempt at this hung indefinitely
    rather than failing. It is also the documented-as-avoidable overhead for
    something that only needs to read one response field.

    The status code is captured from the first http.response.start message and
    the response is otherwise passed through untouched.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in EXCLUDED_PATHS:
            await self.app(scope, receive, send)
            return

        status_holder = {"code": 0}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            # In a finally block so an unhandled exception downstream is still
            # counted — a request that blew up is exactly the one worth seeing.
            # A crash before any response start leaves the code at 0, which
            # buckets as "ok"; guard that explicitly instead.
            try:
                headers = Headers(scope=scope)
                requests_by_client_total.labels(
                    client_type=classify(headers.get("user-agent")),
                    outcome=outcome_for(status_holder["code"] or 500),
                ).inc()
                distinct_caller_counter.observe(_caller_from_scope(scope, headers))
            except Exception as e:
                # Never fail a request over a metric. A missing data point is a
                # worse graph; raising here would be a 500 for the caller.
                logger.debug("Could not record client metrics: %s", e)


def _caller_from_scope(scope: Scope, headers: Headers) -> str:
    """The caller address, using the same forwarded-header rules as the limits.

    get_client_ip takes a Request, and constructing one per response purely to
    read headers is wasteful, so the same TRUSTED_PROXY_HOPS logic is reached
    through a minimal stand-in rather than duplicated here — duplicating it is
    how the two copies in #333 came to disagree.
    """
    class _Shim:
        pass

    shim = _Shim()
    shim.headers = headers
    client = scope.get("client")

    class _Client:
        host = client[0] if client else None

    shim.client = _Client() if client else None
    return get_client_ip(shim)  # type: ignore[arg-type]
