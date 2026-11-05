# tests/test_bee_pool_recovery.py
"""
The shared HTTP client's pool stuck after cancellations, and its recovery (#587).

On staging, after Bee answered 500s and dropped connections under load, every
Bee call failed with PoolTimeout until a restart, while the process held about
11 sockets. The first two tests reproduce the mechanism with a real client
against a local stub, no mocks, once per library:

httpcore 1.0.9 (httpx 0.28.1, still installed for x402) hands a waiting
request a NEW connection when a slot frees (a connection Bee closed). If that
request is cancelled before it runs (a client disconnect, an asyncio.wait_for
timeout), the pool forgets the request but keeps the connection, in the
CONNECTING state with no socket. It is never idle, closed or expired, so
nothing removes it, and it holds a max_connections slot for good. Enough of
them and every call times out waiting for the pool.

httpcore2 (httpx2, Pydantic's maintained fork, which the gateway uses since
#595) drops such connections (pydantic/httpx2#983, in 2.6.0 and later).

On httpcore 1.0.9 and older the deterministic test requires the leak, so a
reproduction that silently stops triggering fails. On httpcore2 2.6.0 and
later both tests require a healthy pool. On any other version the
deterministic test accepts either the leak exactly as described or a usable
pool (with a warning), and records which. The load test depends on timing, so
on httpcore 1.0.9 it accepts either and records which.

The gateway keeps its self-recovery as a backstop (#618 decides from a soak).
httpcore2 would clean up the CONNECTING connections the leak left, so the
recovery tests further down inject connections it keeps: connected, never
idle, and held by no request. The pool counts them against max_connections,
so the PoolTimeouts and the pool snapshot in those tests are real.
"""
import asyncio
import contextlib
import gc
import random
import time
import warnings
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import httpcore
import httpcore2
import httpx
import httpx2
import pytest
import pytest_asyncio
from packaging.version import Version
from prometheus_client import REGISTRY

import app.services.http_client as hc
from app.core.config import settings


# --------------------------------------------------------------------------
# A local Bee stub
# --------------------------------------------------------------------------

class BeeStub:
    """Minimal HTTP/1.1 server on 127.0.0.1.

    /close   500 with "Connection: close" (Bee erroring, connection dropped after)
    /flaky   after a short random delay: drops the connection, or answers 500
    /slow    200 once release is set
    anything else (/health, /ok, /wallet): 200, keep-alive
    """

    def __init__(self, seed: int = 587):
        self.release = asyncio.Event()
        self.rng = random.Random(seed)
        self.server = None
        self.port = None

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader, writer):
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                path = head.split(b" ", 2)[1]
                if path == b"/close":
                    writer.write(b"HTTP/1.1 500 Internal Server Error\r\n"
                                 b"Connection: close\r\nContent-Length: 3\r\n\r\nerr")
                    await writer.drain()
                    return
                if path == b"/flaky":
                    await asyncio.sleep(self.rng.uniform(0, 0.03))
                    if self.rng.random() < 0.3:
                        return  # "Server disconnected without sending a response"
                    writer.write(b"HTTP/1.1 500 Internal Server Error\r\n"
                                 b"Content-Length: 3\r\n\r\nerr")
                    await writer.drain()
                    continue
                if path == b"/slow":
                    await self.release.wait()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()


@pytest_asyncio.fixture
async def bee():
    stub = await BeeStub().start()
    try:
        yield stub
    finally:
        stub.release.set()
        await stub.stop()


# The last httpcore release known to leak (#587). On it and older ones the
# reproduction must reproduce. httpcore2 fixed the leak in 2.6.0
# (pydantic/httpx2#983): from there on the pool must stay healthy. Any other
# version accepts either, with a warning.
LAST_KNOWN_LEAKING_HTTPCORE = Version("1.0.9")
FIRST_FIXED_HTTPCORE2 = Version("2.6.0")


def known_to_leak(core) -> bool:
    return core is httpcore and Version(core.__version__) <= LAST_KNOWN_LEAKING_HTTPCORE


def known_fixed(core) -> bool:
    return core is httpcore2 and Version(core.__version__) >= FIRST_FIXED_HTTPCORE2


# Each reproduction runs on encode's httpx/httpcore 1.0.9 (still installed:
# x402 uses it), which must leak, and on httpx2/httpcore2, the gateway's client
# since #595, which must not. The load test runs 20 s on httpcore 1.0.9, where
# timing decides how soon it leaks, and 5 s on httpcore2, which cannot leak by
# chance, so a longer run adds only time.
LIBRARIES = [
    pytest.param(httpx, httpcore, 20.0, id="httpcore-1.0.9"),
    pytest.param(httpx2, httpcore2, 5.0, id="httpcore2"),
]


def raw_pool(client):
    return client._transport._pool


class RecordingBackend:
    """httpcore's network backend, keeping every stream it opens.

    A request cancelled under load can drop a socket without closing it: a
    connection that leaves the pool unclosed, or a connect cancelled after
    the socket was made. client.aclose() reaches neither. Each connect runs in
    its own task, so a cancellation of the request still reaches httpcore at
    once (the same CancelledError, at the same await) while the connect
    finishes and its stream is recorded. Teardown closes everything recorded,
    so the test leaves no socket for the garbage collector to warn about.
    """

    def __init__(self, inner):
        self._inner = inner
        self.streams = []
        self._connects = set()

    async def connect_tcp(self, *args, **kwargs):
        task = asyncio.ensure_future(self._inner.connect_tcp(*args, **kwargs))
        self._connects.add(task)
        task.add_done_callback(self._record)
        return await asyncio.shield(task)

    def _record(self, task):
        self._connects.discard(task)
        if not task.cancelled() and task.exception() is None:
            self.streams.append(task.result())

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def aclose_all(self):
        if self._connects:
            await asyncio.gather(*self._connects, return_exceptions=True)
        for stream in self.streams:
            with contextlib.suppress(Exception):
                await stream.aclose()


class PassThroughBackend(RecordingBackend):
    """httpcore's own connect path, unchanged (no task, no shield), as the
    gateway runs it; it only keeps the streams that connects return, for
    teardown. A connect cancelled before it returns is closed by the backend
    itself."""

    async def connect_tcp(self, *args, **kwargs):
        stream = await self._inner.connect_tcp(*args, **kwargs)
        self.streams.append(stream)
        return stream


def recording_client(lib, backend=RecordingBackend, **kwargs):
    """A plain client of `lib` (httpx or httpx2) whose sockets the test can close in teardown."""
    client = lib.AsyncClient(**kwargs)
    pool = raw_pool(client)
    pool._network_backend = backend(pool._network_backend)
    return client


async def close_recording_client(client):
    await client.aclose()
    await raw_pool(client)._network_backend.aclose_all()
    # A socket made by a connect that was cancelled before it returned never
    # reaches a PassThroughBackend, and is closed only by the garbage
    # collector; until then the stub's wait_closed() would wait for it.
    gc.collect()


def stuck_connections(pool):
    """Connections neither idle nor closed that no request in the pool holds."""
    held = {id(r.connection) for r in pool._requests if r.connection is not None}
    return [c for c in pool.connections
            if not c.is_closed() and not c.is_idle() and id(c) not in held]


async def leak_slots(client, pool, url_close: str, slots: int, max_connections: int):
    """Leak pool slots the way #587 did, deterministically.

    Each round fills every usable slot with a response whose connection the
    server closes, queues one more request, closes one response (freeing the
    slot, so the pool hands the queued request a new connection), and cancels
    the queued request before it runs: a client disconnect at the wrong moment.
    """
    for _ in range(slots):
        free = max_connections - len(stuck_connections(pool))
        if free <= 0:
            return
        responses = [await client.send(client.build_request("GET", url_close), stream=True)
                     for _ in range(free)]
        queued = asyncio.ensure_future(client.get(url_close))
        for _ in range(5):
            await asyncio.sleep(0)  # let it reach the pool's wait
        await responses[0].aclose()
        queued.cancel()  # no await in between: it has not run since it got the connection
        with contextlib.suppress(BaseException):
            await queued
        for response in responses[1:]:
            await response.aclose()


def sample(name, labels=None):
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


# --------------------------------------------------------------------------
# 1. Reproduction with the real libraries (describes upstream behaviour)
# --------------------------------------------------------------------------

class TestReproduction:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("lib, core, load_seconds", LIBRARIES)
    async def test_cancelled_request_keeps_a_slot_without_socket(self, bee, record_property,
                                                                 lib, core, load_seconds):
        """Deterministic: on httpcore 1.0.9 each cancelled queued request costs one slot for good."""
        max_connections = 3
        client = recording_client(lib, limits=lib.Limits(max_connections=max_connections),
                                  timeout=lib.Timeout(5.0, pool=0.3))
        try:
            pool = raw_pool(client)
            await leak_slots(client, pool, bee.url("/close"), max_connections, max_connections)
            stuck = stuck_connections(pool)
            try:
                later = await client.get(bee.url("/ok"))
                outcome = "healthy"
            except lib.PoolTimeout as e:
                later = e
                outcome = "leak"
            record_property("httpcore_587_outcome", f"{core.__name__} {core.__version__}: {outcome}")
            if known_to_leak(core):
                assert outcome == "leak", (
                    f"httpcore {core.__version__} is known to leak (#587), but the "
                    "deterministic reproduction did not trigger it: the reproduction is broken")
            if known_fixed(core):
                assert outcome == "healthy", (
                    f"{core.__name__} {core.__version__} should not leak (pydantic/httpx2#983), "
                    f"but the deterministic reproduction left {len(stuck)} stuck slots")

            if outcome == "leak":
                # The pool counts itself full ...
                assert len(stuck) == max_connections
                # ... with no socket behind any of the stuck slots ...
                assert all(c.info() == "CONNECTING" for c in stuck), [c.info() for c in stuck]
                assert not pool._requests
                # ... the error has no text, as in the staging log ...
                assert str(later) == ""
                # ... and Bee answers a new client at once.
                async with lib.AsyncClient() as fresh:
                    assert (await fresh.get(bee.url("/ok"))).status_code == 200
            else:
                if not known_fixed(core):
                    warnings.warn(f"{core.__name__} {core.__version__} no longer leaks pool slots on "
                                  "cancellation (#587)")
                assert later.status_code == 200
                assert not stuck
        finally:
            await close_recording_client(client)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("lib, core, load_seconds", LIBRARIES)
    async def test_bee_errors_and_cancellations_under_load(self, bee, record_property,
                                                           lib, core, load_seconds):
        """Load: 500s, dropped connections, and three kinds of cancellation.

        Runs until a call on the client gets PoolTimeout or load_seconds pass.
        """
        max_connections = 4
        # httpcore2 runs on its own connect path, as in the gateway. The
        # shielded connect of RecordingBackend left "HTTP/1.1 NEW" connections
        # stuck on httpcore2 under anyio cancel scopes (up to 3 of 4 slots,
        # most runs); none on the pass-through path. Tracked in #629.
        backend = PassThroughBackend if known_fixed(core) else RecordingBackend
        client = recording_client(lib, backend, limits=lib.Limits(max_connections=max_connections),
                                  timeout=lib.Timeout(10.0, connect=5.0, pool=0.5))
        rng = random.Random(5870)
        pool = raw_pool(client)

        async def one(kind: int):
            try:
                if kind == 0:  # asyncio.wait_for timeout, as in the stamp and download paths
                    await asyncio.wait_for(client.get(bee.url("/flaky")), rng.uniform(0.001, 0.05))
                elif kind == 1:  # anyio cancel scope, as Starlette's BaseHTTPMiddleware on disconnect
                    with anyio.move_on_after(rng.uniform(0.001, 0.05)):
                        await client.get(bee.url("/flaky"))
                else:  # the task cancelled outright
                    task = asyncio.ensure_future(client.get(bee.url("/flaky")))
                    await asyncio.sleep(rng.uniform(0, 0.05))
                    task.cancel()
                    await task
            except BaseException:
                pass

        outcome = "healthy"
        deadline = time.monotonic() + load_seconds
        try:
            while time.monotonic() < deadline:
                await asyncio.gather(*(one(i % 3) for i in range(40)))
                try:
                    await client.get(bee.url("/ok"))
                except lib.PoolTimeout:
                    outcome = "leak"
                    break
                except lib.HTTPError:
                    pass
            record_property("httpcore_587_load_outcome", f"{core.__name__} {core.__version__}: {outcome}")
            # Not pinned to "leak" on the known version, unlike the deterministic
            # test above: whether random load hits the window depends on timing.
            # Measured: it leaks within about 2 s in a normal run on Python 3.10
            # and 3.13, but never in 120 s under tracemalloc, which slows every
            # allocation. Pinning it would make a slow CI runner fail.
            # Pinned on a fixed httpcore2: a library without the leak cannot
            # leak by chance, so a later call must get a connection and no
            # connection may be left stuck.
            await asyncio.sleep(0.1)
            leftover = [c.info() for c in stuck_connections(pool)]
            record_property("httpcore_587_load_stuck", len(leftover))
            if known_fixed(core):
                assert outcome == "healthy", (
                    f"{core.__name__} {core.__version__} should not leak (pydantic/httpx2#983)")
                assert not leftover, (
                    f"{core.__name__} {core.__version__} kept {len(leftover)} stuck connections "
                    f"after load: {leftover}")
            if outcome == "leak":
                stuck = stuck_connections(pool)
                # Every slot is taken, but not by a request being served.
                assert len(stuck) + sum(1 for r in pool._requests if r.connection) >= max_connections
                assert stuck
                async with lib.AsyncClient() as fresh:
                    assert (await fresh.get(bee.url("/ok"))).status_code == 200
            else:
                if not known_fixed(core):
                    warnings.warn(f"No PoolTimeout within {load_seconds:.0f} s of load (#587): "
                                  f"{core.__name__} {core.__version__} may have fixed the leak")
                assert (await client.get(bee.url("/ok"))).status_code == 200
        finally:
            await close_recording_client(client)


# --------------------------------------------------------------------------
# 2. The gateway's shared client recovers
# --------------------------------------------------------------------------

@pytest_asyncio.fixture
async def shared(bee):
    """The module's shared client, built against the stub with a small pool."""
    saved = {k: getattr(hc, k) for k in (
        "_client", "_transport", "_replace_lock", "_recovery_task", "_last_recovery_attempt")}
    patches = [
        patch.object(settings, "BEE_HTTP_MAX_CONNECTIONS", 4),
        patch.object(settings, "BEE_HTTP_POOL_RECOVERY_THRESHOLD", 2),
        patch.object(settings, "BEE_HTTP_POOL_STUCK_FRACTION", 0.5),
        patch.object(settings, "BEE_HTTP_POOL_RECOVERY_COOLDOWN_SECONDS", 0.0),
        patch.object(settings, "SWARM_BEE_API_URL", bee.url("/")),
        patch.object(hc, "RETIRE_POLL_SECONDS", 0.01),
    ]
    for p in patches:
        p.start()
    await hc.init_client()
    try:
        yield bee
    finally:
        await hc.close_client()
        for p in reversed(patches):
            p.stop()
        for k, v in saved.items():
            setattr(hc, k, v)


SHORT_POOL_WAIT = httpx2.Timeout(5.0, pool=0.2)


class StuckConnection(httpcore2.AsyncConnectionInterface):
    """A pool slot that serves nothing and that httpcore2 keeps.

    The leak in httpcore 1.0.9 left CONNECTING connections, which httpcore2
    drops (only connected or reserved connections are retained). This one is
    connected, never idle, available, expired or closed, and no request holds
    it: the state a future leak would leave, which the backstop exists for.
    The pool counts it against max_connections, so a call that needs a slot
    really times out, and pool_snapshot() really reads it as stuck.
    """

    def __init__(self, origin):
        self._origin = origin
        self._closed = False

    async def handle_async_request(self, request):  # pragma: no cover - never assigned
        raise AssertionError("a stuck connection is never given a request")

    async def aclose(self) -> None:
        self._closed = True

    def info(self) -> str:
        return "STUCK (injected)"

    def can_handle_request(self, origin) -> bool:
        return origin == self._origin

    def is_connected(self) -> bool:
        return True

    def is_available(self) -> bool:
        return False

    def has_expired(self) -> bool:
        return False

    def is_idle(self) -> bool:
        return False

    def can_multiplex(self) -> bool:
        return False

    def is_closed(self) -> bool:
        return self._closed


async def leak_shared_pool():
    """Fill every slot of the shared client's pool with stuck connections."""
    pool = hc._transport.inner._pool
    url = str(settings.SWARM_BEE_API_URL) + "close"
    origin = httpcore2.URL(url).origin
    missing = settings.BEE_HTTP_MAX_CONNECTIONS - len(stuck_connections(pool))
    for _ in range(missing):
        pool._connections.append(StuckConnection(origin))
    assert len(stuck_connections(pool)) == settings.BEE_HTTP_MAX_CONNECTIONS


async def hit_pool_timeouts(times: int):
    for _ in range(times):
        with pytest.raises(httpx2.PoolTimeout):
            await hc.get_client().get(str(settings.SWARM_BEE_API_URL) + "ok", timeout=SHORT_POOL_WAIT)


async def recovery_result():
    task = hc._recovery_task
    assert task is not None, "no recovery was started"
    return await asyncio.wait_for(task, 10)


class TestRecovery:

    @pytest.mark.asyncio
    async def test_stuck_pool_is_replaced_and_calls_work_again(self, shared):
        timeouts_before = sample("gateway_bee_http_pool_timeouts_total")
        replaced_before = sample("gateway_bee_http_client_replacements_total", {"result": "replaced"})
        old = hc.get_client()
        await leak_shared_pool()
        assert sample("gateway_bee_http_pool_connections", {"state": "stuck"}) == 4

        await hit_pool_timeouts(2)
        assert await recovery_result() == "replaced"

        assert hc.get_client() is not old
        response = await hc.get_client().get(shared.url("/wallet"), timeout=SHORT_POOL_WAIT)
        assert response.status_code == 200
        assert sample("gateway_bee_http_pool_timeouts_total") - timeouts_before == 2
        assert sample("gateway_bee_http_client_replacements_total", {"result": "replaced"}) - replaced_before == 1
        assert sample("gateway_bee_http_pool_connections", {"state": "stuck"}) == 0
        # The old client had nothing in flight, so it is closed promptly.
        for _ in range(200):
            if old.is_closed:
                break
            await asyncio.sleep(0.01)
        assert old.is_closed

    @pytest.mark.asyncio
    async def test_one_pool_timeout_below_threshold_does_nothing(self, shared):
        old = hc.get_client()
        await leak_shared_pool()
        await hit_pool_timeouts(1)
        assert hc._recovery_task is None
        assert hc.get_client() is old

    @pytest.mark.asyncio
    async def test_bee_down_means_no_replacement(self, shared):
        before = sample("gateway_bee_http_client_replacements_total", {"result": "probe_failed"})
        old = hc.get_client()
        await leak_shared_pool()
        await shared.stop()  # Bee stops answering on any connection
        await hit_pool_timeouts(2)
        assert await recovery_result() == "probe_failed"
        assert hc.get_client() is old
        assert sample("gateway_bee_http_client_replacements_total", {"result": "probe_failed"}) - before == 1
        shared.server = await asyncio.start_server(shared._handle, "127.0.0.1", shared.port)

    @pytest.mark.asyncio
    async def test_busy_pool_is_not_replaced(self, shared):
        """Real saturation (Bee slow but alive): replacing would only double the connections."""
        before = sample("gateway_bee_http_client_replacements_total", {"result": "not_stuck"})
        old = hc.get_client()
        slow = [asyncio.ensure_future(old.get(shared.url("/slow"))) for _ in range(4)]
        for _ in range(50):
            await asyncio.sleep(0.01)
            if sample("gateway_bee_http_pool_connections", {"state": "active"}) == 4:
                break
        with patch.object(hc, "probe_bee", AsyncMock(return_value=True)) as probe:
            await hit_pool_timeouts(2)
            assert await recovery_result() == "not_stuck"
            probe.assert_not_awaited()
        assert hc.get_client() is old
        assert sample("gateway_bee_http_client_replacements_total", {"result": "not_stuck"}) - before == 1
        shared.release.set()
        assert [r.status_code for r in await asyncio.gather(*slow)] == [200] * 4

    @pytest.mark.asyncio
    async def test_unreadable_pool_falls_back_to_the_probe(self, shared):
        before = sample("gateway_bee_http_client_replacements_total", {"result": "replaced_unverified"})
        old = hc.get_client()
        with patch.object(hc, "pool_snapshot", return_value=None):
            result = await hc.recover_pool(hc._transport.generation)
        assert result == "replaced_unverified"
        assert hc.get_client() is not old
        assert sample("gateway_bee_http_client_replacements_total",
                      {"result": "replaced_unverified"}) - before == 1

    @pytest.mark.asyncio
    async def test_two_recoveries_at_once_replace_once(self, shared):
        before = sample("gateway_bee_http_client_replacements_total", {"result": "replaced"})
        await leak_shared_pool()
        generation = hc._transport.generation

        async def slow_probe():
            await asyncio.sleep(0.05)
            return True

        with patch.object(hc, "probe_bee", side_effect=slow_probe):
            results = await asyncio.gather(hc.recover_pool(generation), hc.recover_pool(generation))
        assert sorted(results) == ["replaced", "skipped"]
        assert sample("gateway_bee_http_client_replacements_total", {"result": "replaced"}) - before == 1
        assert hc._transport.generation == generation + 1

    @pytest.mark.asyncio
    async def test_cooldown_spaces_attempts(self, shared):
        await leak_shared_pool()
        with patch.object(settings, "BEE_HTTP_POOL_RECOVERY_COOLDOWN_SECONDS", 3600.0):
            assert await hc.recover_pool(hc._transport.generation) == "replaced"
            await leak_shared_pool()
            await hit_pool_timeouts(2)
            assert hc._recovery_task is None  # not even started
            assert await hc.recover_pool(hc._transport.generation) == "skipped"

    @pytest.mark.asyncio
    async def test_inflight_request_on_old_client_completes(self, shared):
        old = hc.get_client()
        pending = asyncio.ensure_future(old.get(shared.url("/slow")))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if hc._transport.in_flight == 1:
                break
        old_transport = hc._transport
        with patch.object(hc, "pool_snapshot", return_value={"active": 1, "idle": 0, "stuck": 3, "requests": 1}):
            assert await hc.recover_pool(old_transport.generation) == "replaced"
        assert hc.get_client() is not old
        await asyncio.sleep(0.1)
        assert not old.is_closed  # still serving the slow request
        shared.release.set()
        assert (await pending).status_code == 200
        assert old_transport.in_flight == 0
        for _ in range(200):
            if old.is_closed:
                break
            await asyncio.sleep(0.01)
        assert old.is_closed

    @pytest.mark.asyncio
    async def test_close_client_closes_replaced_clients(self, shared):
        old = hc.get_client()
        pending = asyncio.ensure_future(old.get(shared.url("/slow")))
        await asyncio.sleep(0.05)
        with patch.object(hc, "pool_snapshot", return_value={"active": 1, "idle": 0, "stuck": 3, "requests": 1}):
            await hc.recover_pool(hc._transport.generation)
        await hc.close_client()
        assert old.is_closed
        assert not hc._retiring and not hc._retired_clients
        pending.cancel()
        with contextlib.suppress(BaseException):
            await pending


# --------------------------------------------------------------------------
# 3. Settings, counting, and log text
# --------------------------------------------------------------------------

class TestClientSettings:

    @pytest.mark.asyncio
    async def test_limits_come_from_settings(self, shared):
        with patch.object(settings, "BEE_HTTP_MAX_KEEPALIVE_CONNECTIONS", 3), \
             patch.object(settings, "BEE_HTTP_KEEPALIVE_EXPIRY_SECONDS", 7.0):
            await hc.close_client()
            await hc.init_client()
        pool = hc._transport.inner._pool
        assert pool._max_connections == 4
        assert pool._max_keepalive_connections == 3
        assert pool._keepalive_expiry == 7.0

    def test_defaults_are_the_httpx_client_defaults(self):
        """Behaviour does not change unless the limits are configured."""
        from app.core.config import Settings
        fields = Settings.model_fields
        default_pool = httpx2.AsyncClient()._transport._pool
        assert fields["BEE_HTTP_MAX_CONNECTIONS"].default == default_pool._max_connections == 100
        assert fields["BEE_HTTP_MAX_KEEPALIVE_CONNECTIONS"].default == default_pool._max_keepalive_connections == 20
        assert fields["BEE_HTTP_KEEPALIVE_EXPIRY_SECONDS"].default == default_pool._keepalive_expiry == 5.0

    @pytest.mark.asyncio
    async def test_pool_of_this_httpx_version_is_readable(self, shared):
        """Fails when an httpx/httpcore upgrade moves the pool internals the gauges read."""
        assert hc.current_pool_snapshot() == {"active": 0, "idle": 0, "stuck": 0, "requests": 0}

    def test_unreadable_pool_is_none_not_an_error(self):
        transport = MagicMock()
        transport.inner = object()  # no _pool
        assert hc.pool_snapshot(transport) is None


class TestTransportCounting:

    def _transport(self, *outcomes):
        transport = hc.ObservedTransport(httpx2.Limits(), generation=-1)
        inner = MagicMock()

        async def handle(request):
            outcome = outcomes[handle.calls]
            handle.calls += 1
            if isinstance(outcome, BaseException):
                raise outcome
            return httpx2.Response(200, stream=httpx2.ByteStream(b"ok"))
        handle.calls = 0
        inner.handle_async_request = handle
        transport.inner = inner
        return transport

    @pytest.mark.asyncio
    async def test_a_response_resets_the_consecutive_count(self):
        transport = self._transport(httpx2.PoolTimeout(""), httpx2.PoolTimeout(""), None, httpx2.PoolTimeout(""))
        request = httpx2.Request("GET", "http://bee.test/")
        for _ in range(2):
            with pytest.raises(httpx2.PoolTimeout):
                await transport.handle_async_request(request)
        assert transport.consecutive_pool_timeouts == 2
        response = await transport.handle_async_request(request)
        assert transport.consecutive_pool_timeouts == 0
        assert transport.in_flight == 1
        await response.aclose()
        assert transport.in_flight == 0
        with pytest.raises(httpx2.PoolTimeout):
            await transport.handle_async_request(request)
        assert transport.consecutive_pool_timeouts == 1
        assert transport.in_flight == 0

    @pytest.mark.asyncio
    async def test_cancelled_request_is_not_left_in_flight(self):
        transport = self._transport(asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await transport.handle_async_request(httpx2.Request("GET", "http://bee.test/"))
        assert transport.in_flight == 0

    @pytest.mark.asyncio
    async def test_old_client_timeouts_do_not_start_a_recovery(self, shared):
        old_transport = hc.ObservedTransport(httpx2.Limits(), generation=-1)
        old_transport.consecutive_pool_timeouts = 99
        hc._on_pool_timeout(old_transport)
        assert hc._recovery_task is None


class TestExceptionText:

    def test_empty_message_gives_the_type(self):
        assert hc.exc_text(httpx2.PoolTimeout("")) == "PoolTimeout"

    def test_message_is_kept_after_the_type(self):
        e = httpx2.RemoteProtocolError("Server disconnected without sending a response.")
        assert hc.exc_text(e) == "RemoteProtocolError: Server disconnected without sending a response."

    @pytest.mark.asyncio
    async def test_swarm_api_logs_the_type_of_an_empty_error(self, caplog):
        from app.services import swarm_api
        client = MagicMock()
        client.get = AsyncMock(side_effect=httpx2.PoolTimeout(""))
        with patch("app.services.swarm_api.get_client", return_value=client), \
             caplog.at_level("ERROR", logger="app.services.swarm_api"):
            with pytest.raises(httpx2.PoolTimeout):
                await swarm_api.get_wallet_info()
        assert any(r.getMessage().endswith(": PoolTimeout") for r in caplog.records), \
            [r.getMessage() for r in caplog.records]

    @pytest.mark.asyncio
    async def test_preflight_logs_the_type_of_an_empty_error(self, caplog):
        from app.x402 import preflight
        with patch("app.x402.preflight.get_wallet_info", AsyncMock(side_effect=httpx2.PoolTimeout(""))), \
             caplog.at_level("ERROR", logger="app.x402.preflight"):
            await preflight.check_xbzz_balance()
        assert any(r.getMessage() == "Failed to check xBZZ balance: PoolTimeout" for r in caplog.records), \
            [r.getMessage() for r in caplog.records]

    def test_data_download_logs_the_type_of_an_empty_error(self, caplog):
        from fastapi.testclient import TestClient
        from app.main import app
        with patch("app.api.endpoints.data.download_data_from_swarm",
                   AsyncMock(side_effect=httpx2.PoolTimeout(""))), \
             caplog.at_level("ERROR", logger="app.api.endpoints.data"):
            response = TestClient(app).get("/api/v1/data/" + "a" * 64)
        assert response.status_code == 502
        # The response text is unchanged; only the log names the type.
        assert response.json()["detail"] == \
            "Failed to download data from Swarm. The Bee node may be unavailable."
        assert any(r.getMessage() == "Swarm API error during download: PoolTimeout"
                   for r in caplog.records), [r.getMessage() for r in caplog.records]

    def test_debug_proxy_logs_the_type_of_an_empty_error(self, caplog):
        import time as _time
        from eth_account import Account
        from eth_account.messages import encode_defunct
        from fastapi.testclient import TestClient
        from app.main import app
        key = "0x" + "22" * 32
        debug_settings = MagicMock()
        debug_settings.get_debug_allowed_addresses.return_value = [Account.from_key(key).address.lower()]
        debug_settings.DEBUG_SIG_MAX_AGE_SECONDS = 300
        debug_settings.SWARM_BEE_API_URL = "http://bee.test/"
        ts = int(_time.time())
        signed = Account.sign_message(encode_defunct(text=f"swarm-connect-debug:{ts}"), private_key=key)
        headers = {"X-Debug-Timestamp": str(ts), "X-Debug-Signature": "0x" + bytes(signed.signature).hex()}
        bee_client = MagicMock()
        bee_client.get = AsyncMock(side_effect=httpx2.PoolTimeout(""))
        with patch("app.api.endpoints.debug.settings", debug_settings), \
             patch("app.api.endpoints.debug.get_client", return_value=bee_client), \
             caplog.at_level("WARNING", logger="app.api.endpoints.debug"):
            response = TestClient(app).get("/api/v1/debug/bee/topology", headers=headers)
        assert response.status_code == 502
        assert response.json()["detail"] == "Bee node request failed"
        assert any(r.getMessage().endswith("): PoolTimeout") for r in caplog.records), \
            [r.getMessage() for r in caplog.records]

    def test_stamp_health_check_logs_the_type_of_an_empty_error(self, caplog):
        from fastapi.testclient import TestClient
        from app.main import app
        with patch("app.services.swarm_api.get_stamp_health_check",
                   AsyncMock(side_effect=httpx2.PoolTimeout(""))), \
             caplog.at_level("ERROR", logger="app.api.endpoints.stamps"):
            response = TestClient(app).get(f"/api/v1/stamps/{'a' * 64}/check")
        assert response.status_code == 502
        assert response.json()["detail"] == "Could not fetch stamp data. The Bee node may be unavailable."
        assert any(r.getMessage() == "Failed to check stamp health from Swarm API: PoolTimeout"
                   for r in caplog.records), [r.getMessage() for r in caplog.records]

    @pytest.mark.asyncio
    async def test_stamp_pool_replenish_logs_the_type_of_an_empty_error(self, caplog, tmp_path):
        from app.services.stamp_pool import StampPoolManager
        manager = StampPoolManager(state_file=str(tmp_path / "pool_state.json"))
        with patch.object(manager, "_immediate_replenish_paused", return_value=False), \
             patch.object(manager, "_purchase_stamp", AsyncMock(side_effect=httpx2.PoolTimeout(""))), \
             caplog.at_level("ERROR", logger="app.services.stamp_pool"):
            await manager._async_replenish_one(17)
        assert any(r.getMessage() == "Immediate replenishment failed for depth 17: PoolTimeout"
                   for r in caplog.records), [r.getMessage() for r in caplog.records]


class TestProxyEnvironment:
    """Bee is reached directly, whatever proxy the environment names."""

    PROXY_ENV = {"HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
                 "ALL_PROXY": "http://127.0.0.1:9", "NO_PROXY": ""}

    @pytest.mark.asyncio
    async def test_shared_client_and_probe_ignore_proxy_variables(self, shared, monkeypatch):
        for name, value in self.PROXY_ENV.items():
            monkeypatch.setenv(name, value)
            monkeypatch.setenv(name.lower(), value)
        # Control: a default client does go to the (closed) proxy and fails.
        async with httpx2.AsyncClient(timeout=2.0) as proxied:
            with pytest.raises(httpx2.HTTPError):
                await proxied.get(shared.url("/ok"))
        # The shared client, rebuilt with the variables set, and the probe go direct.
        await hc.close_client()
        await hc.init_client()
        response = await hc.get_client().get(shared.url("/ok"), timeout=SHORT_POOL_WAIT)
        assert response.status_code == 200
        assert await hc.probe_bee() is True
