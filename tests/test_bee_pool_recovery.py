# tests/test_bee_pool_recovery.py
"""
The shared HTTP client's pool stuck after cancellations, and its recovery (#587).

On staging, after Bee answered 500s and dropped connections under load, every
Bee call failed with PoolTimeout until a restart, while the process held about
11 sockets. The first two tests reproduce the mechanism with the real httpx and
httpcore against a local stub, no mocks:

httpcore 1.0.9 hands a waiting request a NEW connection when a slot frees (a
connection Bee closed). If that request is cancelled before it runs (a client
disconnect, an asyncio.wait_for timeout), the pool forgets the request but
keeps the connection, in the CONNECTING state with no socket. It is never idle,
closed or expired, so nothing removes it, and it holds a max_connections slot
for good. Enough of them and every call times out waiting for the pool.

These two tests describe upstream behaviour, so they pass either way: they
assert that the leak reproduces exactly as described, or that the pool stays
usable (an httpcore release that fixed it), and record which. The recovery
tests further down do not depend on the leak still existing.
"""
import asyncio
import contextlib
import random
import time
import warnings
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import httpx
import pytest
import pytest_asyncio
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


def raw_pool(client: httpx.AsyncClient):
    return client._transport._pool


def stuck_connections(pool):
    """Connections neither idle nor closed that no request in the pool holds."""
    held = {id(r.connection) for r in pool._requests if r.connection is not None}
    return [c for c in pool.connections
            if not c.is_closed() and not c.is_idle() and id(c) not in held]


async def leak_slots(client: httpx.AsyncClient, pool, url_close: str, slots: int, max_connections: int):
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
# 1. Reproduction with the real httpx/httpcore (describes upstream behaviour)
# --------------------------------------------------------------------------

class TestReproduction:

    @pytest.mark.asyncio
    async def test_cancelled_request_keeps_a_slot_without_socket(self, bee, record_property):
        """Deterministic: each cancelled queued request costs one slot for good."""
        max_connections = 3
        client = httpx.AsyncClient(limits=httpx.Limits(max_connections=max_connections),
                                   timeout=httpx.Timeout(5.0, pool=0.3))
        try:
            pool = raw_pool(client)
            await leak_slots(client, pool, bee.url("/close"), max_connections, max_connections)
            stuck = stuck_connections(pool)
            try:
                later = await client.get(bee.url("/ok"))
                outcome = "healthy"
            except httpx.PoolTimeout as e:
                later = e
                outcome = "leak"
            record_property("httpcore_587_outcome", outcome)

            if outcome == "leak":
                # The pool counts itself full ...
                assert len(stuck) == max_connections
                # ... with no socket behind any of the stuck slots ...
                assert all(c.info() == "CONNECTING" for c in stuck), [c.info() for c in stuck]
                assert not pool._requests
                # ... the error has no text, as in the staging log ...
                assert str(later) == ""
                # ... and Bee answers a new client at once.
                async with httpx.AsyncClient() as fresh:
                    assert (await fresh.get(bee.url("/ok"))).status_code == 200
            else:
                warnings.warn("httpcore no longer leaks pool slots on cancellation (#587); "
                              "the self-recovery in app/services/http_client.py may be removable")
                assert later.status_code == 200
                assert not stuck
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_bee_errors_and_cancellations_under_load(self, bee, record_property):
        """Load: 500s, dropped connections, and three kinds of cancellation.

        Runs until a call on the shared client gets PoolTimeout or 20 s pass.
        """
        max_connections = 4
        client = httpx.AsyncClient(limits=httpx.Limits(max_connections=max_connections),
                                   timeout=httpx.Timeout(10.0, connect=5.0, pool=0.5))
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
        deadline = time.monotonic() + 20
        try:
            while time.monotonic() < deadline:
                await asyncio.gather(*(one(i % 3) for i in range(40)))
                try:
                    await client.get(bee.url("/ok"))
                except httpx.PoolTimeout:
                    outcome = "leak"
                    break
                except httpx.HTTPError:
                    pass
            record_property("httpcore_587_load_outcome", outcome)
            await asyncio.sleep(0.1)
            if outcome == "leak":
                stuck = stuck_connections(pool)
                # Every slot is taken, but not by a request being served.
                assert len(stuck) + sum(1 for r in pool._requests if r.connection) >= max_connections
                assert stuck
                async with httpx.AsyncClient() as fresh:
                    assert (await fresh.get(bee.url("/ok"))).status_code == 200
            else:
                warnings.warn("No PoolTimeout within 20 s of load (#587): httpcore may have fixed the leak")
                assert (await client.get(bee.url("/ok"))).status_code == 200
        finally:
            await client.aclose()


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


SHORT_POOL_WAIT = httpx.Timeout(5.0, pool=0.2)


async def leak_shared_pool():
    """Leak every slot of the shared client's pool; inject them if httpcore no longer leaks."""
    client = hc.get_client()
    pool = hc._transport.inner._pool
    url = str(settings.SWARM_BEE_API_URL) + "close"
    await leak_slots(client, pool, url, settings.BEE_HTTP_MAX_CONNECTIONS, settings.BEE_HTTP_MAX_CONNECTIONS)
    missing = settings.BEE_HTTP_MAX_CONNECTIONS - len(stuck_connections(pool))
    if missing > 0:  # pragma: no cover - only once httpcore is fixed
        import httpcore
        origin = httpcore.URL(url).origin
        for _ in range(missing):
            pool._connections.append(httpcore.AsyncHTTPConnection(origin=origin))
    assert len(stuck_connections(pool)) == settings.BEE_HTTP_MAX_CONNECTIONS


async def hit_pool_timeouts(times: int):
    for _ in range(times):
        with pytest.raises(httpx.PoolTimeout):
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
        default_pool = httpx.AsyncClient()._transport._pool
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
        transport = hc.ObservedTransport(httpx.Limits(), generation=-1)
        inner = MagicMock()

        async def handle(request):
            outcome = outcomes[handle.calls]
            handle.calls += 1
            if isinstance(outcome, BaseException):
                raise outcome
            return httpx.Response(200, stream=httpx.ByteStream(b"ok"))
        handle.calls = 0
        inner.handle_async_request = handle
        transport.inner = inner
        return transport

    @pytest.mark.asyncio
    async def test_a_response_resets_the_consecutive_count(self):
        transport = self._transport(httpx.PoolTimeout(""), httpx.PoolTimeout(""), None, httpx.PoolTimeout(""))
        request = httpx.Request("GET", "http://bee.test/")
        for _ in range(2):
            with pytest.raises(httpx.PoolTimeout):
                await transport.handle_async_request(request)
        assert transport.consecutive_pool_timeouts == 2
        response = await transport.handle_async_request(request)
        assert transport.consecutive_pool_timeouts == 0
        assert transport.in_flight == 1
        await response.aclose()
        assert transport.in_flight == 0
        with pytest.raises(httpx.PoolTimeout):
            await transport.handle_async_request(request)
        assert transport.consecutive_pool_timeouts == 1
        assert transport.in_flight == 0

    @pytest.mark.asyncio
    async def test_cancelled_request_is_not_left_in_flight(self):
        transport = self._transport(asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await transport.handle_async_request(httpx.Request("GET", "http://bee.test/"))
        assert transport.in_flight == 0

    @pytest.mark.asyncio
    async def test_old_client_timeouts_do_not_start_a_recovery(self, shared):
        old_transport = hc.ObservedTransport(httpx.Limits(), generation=-1)
        old_transport.consecutive_pool_timeouts = 99
        hc._on_pool_timeout(old_transport)
        assert hc._recovery_task is None


class TestExceptionText:

    def test_empty_message_gives_the_type(self):
        assert hc.exc_text(httpx.PoolTimeout("")) == "PoolTimeout"

    def test_message_is_kept_after_the_type(self):
        e = httpx.RemoteProtocolError("Server disconnected without sending a response.")
        assert hc.exc_text(e) == "RemoteProtocolError: Server disconnected without sending a response."

    @pytest.mark.asyncio
    async def test_swarm_api_logs_the_type_of_an_empty_error(self, caplog):
        from app.services import swarm_api
        client = MagicMock()
        client.get = AsyncMock(side_effect=httpx.PoolTimeout(""))
        with patch("app.services.swarm_api.get_client", return_value=client), \
             caplog.at_level("ERROR", logger="app.services.swarm_api"):
            with pytest.raises(httpx.PoolTimeout):
                await swarm_api.get_wallet_info()
        assert any(r.getMessage().endswith(": PoolTimeout") for r in caplog.records), \
            [r.getMessage() for r in caplog.records]

    @pytest.mark.asyncio
    async def test_preflight_logs_the_type_of_an_empty_error(self, caplog):
        from app.x402 import preflight
        with patch("app.x402.preflight.get_wallet_info", AsyncMock(side_effect=httpx.PoolTimeout(""))), \
             caplog.at_level("ERROR", logger="app.x402.preflight"):
            await preflight.check_xbzz_balance()
        assert any(r.getMessage() == "Failed to check xBZZ balance: PoolTimeout" for r in caplog.records), \
            [r.getMessage() for r in caplog.records]
