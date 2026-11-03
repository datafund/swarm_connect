# app/services/bee_gate.py
"""
A cap on Bee calls in flight at once (#588).

The per-IP rate limits do not bound the load on Bee: many callers, or one
behind many addresses, could fill the shared HTTP client's pool and Bee itself.
During the 2026-11-03 flood the pool then answered every call with PoolTimeout.
This gate sits in front of the shared client's transport and counts only calls
to SWARM_BEE_API_URL. Its cap is below the pool's max_connections, so under load
a call is refused here, quickly and visibly, instead of queueing in the pool.

Each call takes a slot of one class:

- "background": anything not made while serving a request (the /health
  snapshot refresh, the stamp pool, the metrics poller). It may use every slot,
  and BEE_RESERVED_BACKGROUND_CALLS of them are kept for it alone.
- "user": a call made while serving a request. Calls of the request classes
  together may use at most BEE_MAX_CONCURRENT_CALLS - BEE_RESERVED_BACKGROUND_CALLS.
- "transfer": a request's upload or download (bzz, bytes, chunks). Also capped
  by BEE_MAX_CONCURRENT_TRANSFERS, so long transfers cannot starve short calls.
- "committed": Bee work of a request whose payment has settled. Same pool as
  "user", but it waits as long as background work rather than failing fast,
  because refusing it after the payment would only start a refund.

A request's call that gets no slot within BEE_CALL_SLOT_WAIT_SECONDS raises
BeeBusyError, and BeeGateMiddleware turns that request's error response into a
503 BEE_BUSY with Retry-After, except where a payment is involved (see
app/middleware/bee_gate.py). A BeeBusyError means the call never reached Bee.

A slot is held from the start of the request to Bee until its response body is
closed, so a streamed download holds it for the whole transfer. It is released
on errors and cancellation too.
"""
import asyncio
import contextvars
import logging
from collections import deque
from typing import Deque, Dict, Optional, Tuple
from urllib.parse import urlsplit

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

SLOT_CLASSES = ("user", "transfer", "committed", "background")
REQUEST_CLASSES = ("user", "transfer", "committed")
# Bee paths whose calls move data: uploads, and downloads that stream a body.
TRANSFER_PATHS = ("bzz", "bytes", "chunks")


class BeeBusyError(httpx.TransportError):
    """No Bee call slot was free in time.

    A TransportError, so the existing `except httpx.HTTPError` paths fail the
    call as they would for any other transport failure. It is not a
    PoolTimeout, so the #587 recovery does not count it.
    """


class RequestGateState:
    """Per-request gate state, shared with tasks the request starts."""

    __slots__ = ("refused", "committed", "payment_attempted")

    def __init__(self) -> None:
        self.refused = False      # one of this request's Bee calls was refused
        self.committed = False    # this request's payment has settled
        # Settlement of this request's payment was started: from here on its
        # outcome may be unknown, so its answer must never be replaced.
        self.payment_attempted = False


_request_state: contextvars.ContextVar[Optional[RequestGateState]] = contextvars.ContextVar(
    "bee_gate_request_state", default=None
)


def begin_request() -> RequestGateState:
    """Mark the current context as serving a request; returns its state."""
    state = RequestGateState()
    _request_state.set(state)
    return state


def enter_background() -> None:
    """Mark the current context (a task of its own) as background work."""
    _request_state.set(None)


def mark_committed() -> None:
    """The current request's payment has settled; see "committed" above."""
    state = _request_state.get()
    if state is not None:
        state.committed = True


def mark_payment_attempted() -> None:
    """Settlement of the current request's payment is about to start.

    Called immediately before every facilitator settle call. The 503 BEE_BUSY
    rewrite says nothing was charged, which is no longer known to be true.
    """
    state = _request_state.get()
    if state is not None:
        state.payment_attempted = True


def current_request_state() -> Optional[RequestGateState]:
    return _request_state.get()


def _bee_origin() -> Tuple[str, str, int]:
    parts = urlsplit(str(settings.SWARM_BEE_API_URL))
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.scheme, (parts.hostname or "").lower(), port


def _bee_base_path() -> str:
    return urlsplit(str(settings.SWARM_BEE_API_URL)).path.rstrip("/")


def is_bee_request(request: httpx.Request) -> bool:
    url = request.url
    port = url.port or (443 if url.scheme == "https" else 80)
    return (url.scheme, (url.host or "").lower(), port) == _bee_origin()


def is_transfer(request: httpx.Request) -> bool:
    path = request.url.path
    base = _bee_base_path()
    if base and path.startswith(base):
        path = path[len(base):]
    first = path.lstrip("/").split("/", 1)[0]
    return first in TRANSFER_PATHS


def slot_class(request: httpx.Request) -> str:
    state = _request_state.get()
    if state is None:
        return "background"
    if state.committed:
        return "committed"
    return "transfer" if is_transfer(request) else "user"


class Slot:
    """One held slot; release() is idempotent."""

    __slots__ = ("gate", "cls", "generation", "released")

    def __init__(self, gate: "BeeCallGate", cls: str, generation: int) -> None:
        self.gate = gate
        self.cls = cls
        self.generation = generation
        self.released = False

    def release(self) -> None:
        if not self.released:
            self.released = True
            self.gate._release(self)


class BeeCallGate:
    def __init__(self) -> None:
        self._generation = 0
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.held: Dict[str, int] = {c: 0 for c in SLOT_CLASSES}
        self._waiters: Deque[Tuple[asyncio.Future, str]] = deque()

    def reset(self) -> None:
        """Forget every slot and waiter (tests; or a new event loop)."""
        for fut, _ in self._waiters:
            if not fut.done():
                fut.cancel()
        self._generation += 1
        self._loop = None
        self.held = {c: 0 for c in SLOT_CLASSES}
        self._waiters = deque()

    # -- limits -------------------------------------------------------------

    @staticmethod
    def _limits() -> Tuple[int, int, int]:
        total = settings.BEE_MAX_CONCURRENT_CALLS
        request_cap = max(1, total - settings.BEE_RESERVED_BACKGROUND_CALLS)
        return total, request_cap, settings.BEE_MAX_CONCURRENT_TRANSFERS

    def _fits(self, cls: str) -> bool:
        total, request_cap, transfer_cap = self._limits()
        if sum(self.held.values()) >= total:
            return False
        if cls == "background":
            return True
        if sum(self.held[c] for c in REQUEST_CLASSES) >= request_cap:
            return False
        if cls == "transfer" and self.held["transfer"] >= transfer_cap:
            return False
        return True

    def request_pool_full(self) -> bool:
        """True when a request's call could not get a slot right now."""
        return not self._fits("user")

    # -- acquire / release --------------------------------------------------

    def _bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            # Only ever one loop in production. Tests run several in turn; slots
            # of a finished loop can never be released by it.
            if self._loop is not None:
                self.reset()
            self._loop = loop

    async def acquire(self, cls: str) -> Slot:
        self._bind_loop()
        # Waiters are granted as soon as a release lets them fit, and whether a
        # call fits depends only on its class, so a call that fits now jumps no
        # waiter of its own class.
        if self._fits(cls):
            return self._grant(cls)
        wait = (settings.BEE_BACKGROUND_SLOT_WAIT_SECONDS
                if cls in ("background", "committed") else settings.BEE_CALL_SLOT_WAIT_SECONDS)
        fut: asyncio.Future = self._loop.create_future()
        entry = (fut, cls)
        self._waiters.append(entry)
        try:
            return await asyncio.wait_for(fut, timeout=wait)
        except asyncio.TimeoutError:
            # Granted in the same moment the wait ran out: keep the slot
            # rather than drop it unreleased.
            if fut.done() and not fut.cancelled() and fut.exception() is None:
                return fut.result()
            raise self._refuse(cls, wait) from None
        except BaseException:
            # Cancelled: give back a slot granted at the same moment.
            if fut.done() and not fut.cancelled() and fut.exception() is None:
                fut.result().release()
            raise
        finally:
            try:
                self._waiters.remove(entry)
            except ValueError:
                pass

    def _grant(self, cls: str) -> Slot:
        self.held[cls] += 1
        return Slot(self, cls, self._generation)

    def _refuse(self, cls: str, wait: float) -> BeeBusyError:
        _count_rejection(cls)
        state = _request_state.get()
        if state is not None:
            state.refused = True
        return BeeBusyError(f"no Bee call slot free within {wait:g} s ({cls})")

    def _release(self, slot: Slot) -> None:
        if slot.generation != self._generation:
            return
        self.held[slot.cls] = max(0, self.held[slot.cls] - 1)
        self._wake()

    def _wake(self) -> None:
        """Grant freed slots to waiters, oldest first, skipping ones that do not fit."""
        for fut, cls in list(self._waiters):
            if fut.done():
                continue
            if self._fits(cls):
                fut.set_result(self._grant(cls))


bee_call_gate = BeeCallGate()


def _count_rejection(cls: str) -> None:
    try:
        from app.services.metrics import bee_call_rejections_total
        bee_call_rejections_total.labels(lane=cls).inc()
    except Exception as e:  # pragma: no cover - defensive
        logger.debug(f"Metrics: could not count a Bee call refusal: {e}")


# --- Transport wrapper -------------------------------------------------------


class _ReleasingStream(httpx.AsyncByteStream):
    """A response body that gives its slot back when closed."""

    def __init__(self, stream, slot: Slot) -> None:
        self._stream = stream
        self._slot = slot

    async def __aiter__(self):
        async for chunk in self._stream:
            yield chunk

    async def aclose(self) -> None:
        try:
            await self._stream.aclose()
        finally:
            self._slot.release()


class GatedTransport(httpx.AsyncBaseTransport):
    """Takes a gate slot for each Bee request, then hands it to `inner`."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not settings.BEE_CALL_GATE_ENABLED or not is_bee_request(request):
            return await self.inner.handle_async_request(request)
        slot = await bee_call_gate.acquire(slot_class(request))
        try:
            response = await self.inner.handle_async_request(request)
        except BaseException:
            slot.release()
            raise
        response.stream = _ReleasingStream(response.stream, slot)
        return response

    async def aclose(self) -> None:
        await self.inner.aclose()


def wrap(transport: httpx.AsyncBaseTransport) -> GatedTransport:
    return GatedTransport(transport)
