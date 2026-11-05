# tests/test_bee_call_gate.py
"""
The cap on concurrent Bee calls, and the 503 BEE_BUSY it produces (#588).

Against a stub Bee over real TCP (tests/tools/bee_stub.py) and the gateway's
shared client, so the gate is exercised where it sits: in front of the
client's transport.
"""
import asyncio
from unittest.mock import AsyncMock, patch

import httpx2
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError

from app.core.config import Settings, settings
from app.middleware.bee_gate import BeeGateMiddleware
from app.services import bee_gate
from app.services import http_client as hc
from app.services.bee_gate import BeeBusyError, bee_call_gate
from tests.tools.bee_stub import BeeStub


def sample(metric, **labels):
    return (metric.labels(**labels) if labels else metric)._value.get()


@pytest_asyncio.fixture
async def stub():
    s = await BeeStub().start()
    try:
        yield s
    finally:
        s.release.set()
        await s.stop()


@pytest_asyncio.fixture
async def shared(stub):
    """The shared client against the stub, with a small gate: 4 slots, 1 kept
    for background work, 2 for transfers, short waits."""
    saved = {k: getattr(hc, k) for k in (
        "_client", "_transport", "_replace_lock", "_recovery_task", "_last_recovery_attempt")}
    patches = [
        patch.object(settings, "SWARM_BEE_API_URL", stub.base_url),
        patch.object(settings, "BEE_CALL_GATE_ENABLED", True),
        patch.object(settings, "BEE_MAX_CONCURRENT_CALLS", 4),
        patch.object(settings, "BEE_RESERVED_BACKGROUND_CALLS", 1),
        patch.object(settings, "BEE_MAX_CONCURRENT_TRANSFERS", 2),
        patch.object(settings, "BEE_CALL_SLOT_WAIT_SECONDS", 0.1),
        patch.object(settings, "BEE_BACKGROUND_SLOT_WAIT_SECONDS", 2.0),
    ]
    for p in patches:
        p.start()
    bee_call_gate.reset()
    await hc.init_client()
    try:
        yield stub
    finally:
        stub.release.set()
        await hc.close_client()
        bee_call_gate.reset()
        for p in reversed(patches):
            p.stop()
        for k, v in saved.items():
            setattr(hc, k, v)


def url(path):
    return str(settings.SWARM_BEE_API_URL) + path


async def as_request(coro_fn, committed=False):
    """Run coro_fn() in a task marked as serving a request; returns (state, task)."""
    holder = {}

    async def run():
        holder["state"] = bee_gate.begin_request()
        if committed:
            bee_gate.mark_committed()
        return await coro_fn()

    task = asyncio.get_running_loop().create_task(run())
    await asyncio.sleep(0)
    return holder, task


async def until(cond, timeout=3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        assert asyncio.get_running_loop().time() < deadline, "condition not reached"
        await asyncio.sleep(0.01)


# --------------------------------------------------------------------------
# The cap
# --------------------------------------------------------------------------

class TestCap:

    @pytest.mark.asyncio
    async def test_background_calls_never_exceed_the_cap(self, shared):
        shared.release.clear()
        client = hc.get_client()
        tasks = [asyncio.create_task(client.get(url("wallet"))) for _ in range(12)]
        await until(lambda: shared.in_flight == 4)
        await asyncio.sleep(0.1)
        assert shared.in_flight == 4
        shared.release.set()
        results = await asyncio.gather(*tasks)
        assert all(r.status_code == 200 for r in results)
        assert shared.peak_in_flight == 4
        assert sum(bee_call_gate.held.values()) == 0

    @pytest.mark.asyncio
    async def test_request_call_over_its_share_is_refused_quickly(self, shared):
        from app.services.metrics import bee_call_rejections_total
        shared.release.clear()
        client = hc.get_client()
        held = [await as_request(lambda: client.get(url("wallet"))) for _ in range(3)]
        await until(lambda: shared.in_flight == 3)
        before = sample(bee_call_rejections_total, lane="user")

        holder, task = await as_request(lambda: client.get(url("wallet")))
        with pytest.raises(BeeBusyError):
            await task
        assert holder["state"].refused is True
        assert sample(bee_call_rejections_total, lane="user") == before + 1
        assert shared.in_flight == 3  # the refused call never reached Bee

        shared.release.set()
        for _, t in held:
            assert (await t).status_code == 200
        assert all(h["state"].refused is False for h, _ in held)

    @pytest.mark.asyncio
    async def test_background_work_is_not_starved_by_requests(self, shared):
        shared.release.clear()
        client = hc.get_client()
        held = [await as_request(lambda: client.get(url("wallet"))) for _ in range(3)]
        await until(lambda: shared.in_flight == 3)
        # The reserved slot: background work gets in at once.
        background = asyncio.create_task(client.get(url("status")))
        await until(lambda: shared.in_flight == 4)
        shared.release.set()
        assert (await background).status_code == 200
        for _, t in held:
            await t

    @pytest.mark.asyncio
    async def test_transfers_cannot_starve_short_calls(self, shared):
        shared.release.clear()
        client = hc.get_client()
        transfers = [await as_request(lambda: client.get(url("bzz/" + "ab" * 32))) for _ in range(2)]
        await until(lambda: shared.in_flight == 2)
        # A third transfer is over the transfer cap...
        _, third = await as_request(lambda: client.get(url("bzz/" + "cd" * 32)))
        with pytest.raises(BeeBusyError):
            await third
        # ...while a short call still gets a slot.
        _, short = await as_request(lambda: client.get(url("wallet")))
        await until(lambda: shared.in_flight == 3)
        shared.release.set()
        assert (await short).status_code == 200
        for _, t in transfers:
            await t

    @pytest.mark.asyncio
    async def test_committed_work_waits_instead_of_failing(self, shared):
        shared.release.clear()
        client = hc.get_client()
        held = [await as_request(lambda: client.get(url("wallet"))) for _ in range(3)]
        await until(lambda: shared.in_flight == 3)
        holder, committed = await as_request(lambda: client.get(url("wallet")), committed=True)
        await asyncio.sleep(0.3)  # longer than the request wait (0.1 s)
        assert not committed.done()
        shared.release.set()
        assert (await committed).status_code == 200
        assert holder["state"].refused is False
        for _, t in held:
            await t

    @pytest.mark.asyncio
    async def test_other_hosts_are_not_gated(self, shared):
        other = await BeeStub().start()
        try:
            shared.release.clear()
            client = hc.get_client()
            held = [asyncio.create_task(client.get(url("wallet"))) for _ in range(4)]
            await until(lambda: shared.in_flight == 4)
            # The gate is full; a call to another host (Base RPC) is not counted.
            _, rpc = await as_request(lambda: client.post(other.base_url + "rpc", json={"id": 1}))
            assert (await rpc).status_code == 200
            shared.release.set()
            await asyncio.gather(*held)
        finally:
            await other.stop()


# --------------------------------------------------------------------------
# Slots are always given back
# --------------------------------------------------------------------------

class TestRelease:

    @pytest.mark.asyncio
    async def test_released_after_streamed_body_is_closed(self, shared):
        client = hc.get_client()
        async with client.stream("GET", url("bzz/" + "ab" * 32)) as response:
            assert bee_call_gate.held["background"] == 1
            await response.aread()
        assert sum(bee_call_gate.held.values()) == 0

    @pytest.mark.asyncio
    async def test_released_on_error(self, shared):
        await shared.stop()  # nothing listening any more
        client = hc.get_client()
        with pytest.raises(httpx2.ConnectError):
            await client.get(url("wallet"))
        assert sum(bee_call_gate.held.values()) == 0

    @pytest.mark.asyncio
    async def test_cancellation_leaks_no_slot(self, shared):
        shared.release.clear()
        client = hc.get_client()
        tasks = []
        for i in range(40):
            if i % 2:
                tasks.append((await as_request(lambda: client.get(url("wallet"))))[1])
            else:
                tasks.append(asyncio.create_task(client.get(url("status"))))
        await until(lambda: shared.in_flight == 4)
        await asyncio.sleep(0.02)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert sum(bee_call_gate.held.values()) == 0
        shared.release.set()
        # And the full capacity is usable again.
        results = await asyncio.gather(*[client.get(url("wallet")) for _ in range(4)])
        assert all(r.status_code == 200 for r in results)


    @pytest.mark.asyncio
    async def test_replaced_clients_calls_keep_their_slots_until_closed(self, shared, monkeypatch):
        """#587 replaces the shared client; the gate is not per client, so calls
        still streaming on the old one keep their slots until their bodies close,
        and the new client's calls count against the same cap."""
        old_client = hc.get_client()
        streams = []
        for _ in range(4):  # every slot, on the old client
            cm = old_client.stream("GET", url("bzz/" + "ab" * 32))
            streams.append((cm, await cm.__aenter__()))
        assert sum(bee_call_gate.held.values()) == 4

        monkeypatch.setattr(hc, "probe_bee", AsyncMock(return_value=True))
        monkeypatch.setattr(hc, "pool_snapshot", lambda t: None)
        assert await hc.recover_pool(hc._transport.generation) == "replaced_unverified"
        new_client = hc.get_client()
        assert new_client is not old_client
        assert sum(bee_call_gate.held.values()) == 4  # replacement freed nothing

        # The new client's call waits for a slot the old client's streams hold.
        call = asyncio.create_task(new_client.get(url("wallet")))
        await asyncio.sleep(0.2)
        assert not call.done()
        for cm, response in streams[:1]:
            await response.aread()
            await cm.__aexit__(None, None, None)
        assert (await call).status_code == 200
        for cm, response in streams[1:]:
            await response.aread()
            await cm.__aexit__(None, None, None)
        assert sum(bee_call_gate.held.values()) == 0


# --------------------------------------------------------------------------
# A slot granted in the same moment the wait ends is never lost (#588, #617)
# --------------------------------------------------------------------------

@pytest.fixture
def one_slot(monkeypatch):
    """A gate of its own with one slot; waits long enough never to run out by
    themselves, so each test ends the wait itself, at a chosen moment."""
    monkeypatch.setattr(settings, "BEE_CALL_GATE_ENABLED", True)
    monkeypatch.setattr(settings, "BEE_MAX_CONCURRENT_CALLS", 1)
    monkeypatch.setattr(settings, "BEE_RESERVED_BACKGROUND_CALLS", 0)
    monkeypatch.setattr(settings, "BEE_BACKGROUND_SLOT_WAIT_SECONDS", 30.0)
    return bee_gate.BeeCallGate()


async def _waiting(gate):
    """A task waiting in gate.acquire for the slot the caller holds."""
    task = asyncio.get_running_loop().create_task(gate.acquire("background"))
    await until(lambda: len(gate._waiters) == 1)
    return task


class TestGrantRace:
    """asyncio.wait_for was reimplemented on asyncio.timeout() in Python 3.12.
    acquire() must still keep a slot that is granted in the same loop step as
    its wait ends, or give it back: never refuse or cancel while holding it."""

    @pytest.mark.asyncio
    async def test_slot_granted_as_the_wait_times_out_is_kept(self, one_slot, monkeypatch):
        import asyncio.timeouts
        import sys
        gate = one_slot
        blocker = await gate.acquire("background")
        timeouts = []
        real_timeout = asyncio.timeouts.timeout

        def spy(delay):
            t = real_timeout(delay)
            timeouts.append(t)
            return t

        # wait_for opens its deadline through asyncio.timeouts.timeout().
        monkeypatch.setattr(asyncio.timeouts, "timeout", spy)
        waiter = await _waiting(gate)
        assert len(timeouts) == 1, "acquire() no longer waits through asyncio.wait_for"
        deadline = timeouts[0]
        if not callable(getattr(deadline, "_on_timeout", None)):
            pytest.fail("asyncio.timeouts.Timeout._on_timeout is missing on Python "
                        f"{sys.version.split()[0]}; this test fires the deadline through it "
                        "and must be adapted", pytrace=False)

        def grant_and_expire():
            blocker.release()           # grants the slot to the waiter
            deadline.reschedule(None)   # drop the real timer: it fires here instead
            deadline._on_timeout()      # the wait runs out in the same loop step

        asyncio.get_running_loop().call_soon(grant_and_expire)
        slot = await waiter
        assert slot.cls == "background" and not slot.released
        assert gate.held["background"] == 1
        slot.release()
        assert gate.held["background"] == 0

    @pytest.mark.asyncio
    async def test_slot_granted_as_the_waiter_is_cancelled_is_released(self, one_slot):
        gate = one_slot
        blocker = await gate.acquire("background")
        waiter = await _waiting(gate)

        def grant_and_cancel():
            blocker.release()           # grants the slot to the waiter
            waiter.cancel()             # and its request is cut off in the same loop step

        asyncio.get_running_loop().call_soon(grant_and_cancel)
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert gate.held["background"] == 0
        # The slot is usable again at once.
        again = await asyncio.wait_for(gate.acquire("background"), timeout=1)
        assert gate.held["background"] == 1
        again.release()


# --------------------------------------------------------------------------
# A refusal is not a Bee error and not a pool timeout
# --------------------------------------------------------------------------

class TestNotCountedAsBeeFailure:

    @pytest.mark.asyncio
    async def test_refusal_counts_neither_bee_error_nor_pool_timeout(self, shared):
        from app.services import swarm_api
        from app.services.metrics import bee_api_errors_total, bee_http_pool_timeouts_total
        shared.release.clear()
        client = hc.get_client()
        held = [await as_request(lambda: client.get(url("wallet"))) for _ in range(3)]
        await until(lambda: shared.in_flight == 3)
        errors = sample(bee_api_errors_total, endpoint="wallet")
        timeouts = sample(bee_http_pool_timeouts_total)

        _, task = await as_request(swarm_api.get_wallet_info)
        with pytest.raises(BeeBusyError):
            await task
        assert sample(bee_api_errors_total, endpoint="wallet") == errors
        assert sample(bee_http_pool_timeouts_total) == timeouts
        assert hc._transport.consecutive_pool_timeouts == 0

        shared.release.set()
        for _, t in held:
            await t


# --------------------------------------------------------------------------
# The 503 rewrite: only for this request's own refused Bee call
# --------------------------------------------------------------------------

def _gate_app():
    """A small app with the gate middleware inside CORS, as in app.main."""
    app = FastAPI()

    @app.get("/bee")
    async def bee_call():
        r = await hc.get_client().get(url("wallet"))
        return {"status": r.status_code}

    @app.get("/bee-then-502")
    async def bee_then_502():
        try:
            await hc.get_client().get(url("wallet"))
        except httpx2.HTTPError:
            raise HTTPException(status_code=502, detail="Bee node request failed")
        return {"ok": True}

    @app.get("/bee-swallowed")
    async def bee_swallowed():
        try:
            await hc.get_client().get(url("wallet"))
        except httpx2.HTTPError:
            pass
        return {"ok": True, "partial": True}

    @app.get("/bee-ok-then-422")
    async def bee_ok_then_422():
        await hc.get_client().get(url("wallet"))
        raise HTTPException(status_code=422, detail="bad input")

    @app.get("/no-bee/{code}")
    async def no_bee(code: int):
        raise HTTPException(status_code=code, detail={"code": f"X_{code}", "message": "no Bee call"})

    @app.get("/bee-committed-then-502")
    async def committed():
        bee_gate.mark_committed()
        try:
            await hc.get_client().get(url("wallet"))
        except httpx2.HTTPError:
            raise HTTPException(status_code=502, detail={"code": "PAID_BUT_FAILED", "message": "refund"})
        return {"ok": True}

    @app.post("/api/v1/stamps/")
    async def paid_route():
        return {"ran": True}

    app.add_middleware(BeeGateMiddleware)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
                       expose_headers=["Retry-After"])
    return app


def _fill_request_slots():
    """Mark every request slot as held (4 total - 1 reserved = 3)."""
    bee_call_gate._bind_loop()
    bee_call_gate.held["user"] = 3


@pytest_asyncio.fixture
async def gate_client(shared):
    transport = httpx2.ASGITransport(app=_gate_app(), raise_app_exceptions=False)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        yield c


ORIGIN = {"Origin": "https://example.org"}
PAYMENT = {"X-PAYMENT": "e30="}


def assert_bee_busy(response):
    assert response.status_code == 503
    assert response.headers["retry-after"] == str(settings.BEE_BUSY_RETRY_AFTER_SECONDS)
    body = response.json()
    assert body["code"] == "BEE_BUSY"
    assert body["detail"]["code"] == "BEE_BUSY"
    assert body["message"] == body["detail"]["message"]


class TestRewrite:

    @pytest.mark.asyncio
    async def test_own_refused_call_becomes_503_with_cors_and_envelope(self, gate_client):
        _fill_request_slots()
        r = await gate_client.get("/bee-then-502", headers=ORIGIN)
        assert_bee_busy(r)
        assert r.headers["access-control-allow-origin"] == "*"
        assert "retry-after" in r.headers.get("access-control-expose-headers", "").lower()

    @pytest.mark.asyncio
    async def test_unhandled_refusal_becomes_503_not_500(self, gate_client):
        _fill_request_slots()
        r = await gate_client.get("/bee", headers=ORIGIN)
        assert_bee_busy(r)

    @pytest.mark.asyncio
    async def test_a_2xx_is_never_turned_into_503(self, gate_client):
        _fill_request_slots()
        r = await gate_client.get("/bee-swallowed")
        assert r.status_code == 200
        assert r.json() == {"ok": True, "partial": True}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("code", [402, 422, 429, 502])
    async def test_errors_of_requests_without_bee_calls_pass_through(self, gate_client, code):
        _fill_request_slots()  # even with the gate full
        r = await gate_client.get(f"/no-bee/{code}")
        assert r.status_code == code
        assert r.json()["detail"]["code"] == f"X_{code}"
        assert "retry-after" not in r.headers

    @pytest.mark.asyncio
    async def test_error_after_a_bee_call_that_got_its_slot_passes_through(self, gate_client):
        r = await gate_client.get("/bee-ok-then-422")
        assert r.status_code == 422
        assert r.json()["detail"] == "bad input"

    @pytest.mark.asyncio
    async def test_a_settled_request_keeps_its_own_response(self, gate_client, monkeypatch):
        monkeypatch.setattr(settings, "BEE_BACKGROUND_SLOT_WAIT_SECONDS", 0.1)
        _fill_request_slots()
        r = await gate_client.get("/bee-committed-then-502")
        assert r.status_code == 502
        assert r.json()["detail"]["code"] == "PAID_BUT_FAILED"

    @pytest.mark.asyncio
    async def test_another_requests_refusal_does_not_touch_this_one(self, shared):
        """Two requests at once: one refused, one fine. Only the refused one is 503."""
        transport = httpx2.ASGITransport(app=_gate_app(), raise_app_exceptions=False)
        async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
            shared.release.clear()
            busy = [asyncio.create_task(c.get("/bee")) for _ in range(3)]
            await until(lambda: shared.in_flight == 3)
            refused = await c.get("/bee-then-502")
            assert_bee_busy(refused)
            no_bee = await c.get("/no-bee/422")
            assert no_bee.status_code == 422
            shared.release.set()
            for t in busy:
                assert (await t).status_code == 200


class TestAdmission:

    @pytest.mark.asyncio
    async def test_paid_route_refused_before_it_runs_when_full(self, gate_client, monkeypatch):
        from app.services.metrics import bee_call_rejections_total
        monkeypatch.setattr(settings, "X402_ENABLED", True)
        before = sample(bee_call_rejections_total, lane="admission")
        _fill_request_slots()
        r = await gate_client.post("/api/v1/stamps/", headers=PAYMENT)
        assert_bee_busy(r)
        assert sample(bee_call_rejections_total, lane="admission") == before + 1

    @pytest.mark.asyncio
    async def test_unpaid_request_to_a_paid_route_is_not_refused_up_front(self, gate_client, monkeypatch):
        """It cannot be charged, so it gets its own answer (here the handler's)."""
        monkeypatch.setattr(settings, "X402_ENABLED", True)
        _fill_request_slots()
        r = await gate_client.post("/api/v1/stamps/")
        assert r.status_code == 200 and r.json() == {"ran": True}

    @pytest.mark.asyncio
    async def test_paid_route_runs_when_there_is_room(self, gate_client, monkeypatch):
        monkeypatch.setattr(settings, "X402_ENABLED", True)
        r = await gate_client.post("/api/v1/stamps/", headers=PAYMENT)
        assert r.status_code == 200 and r.json() == {"ran": True}

    @pytest.mark.asyncio
    async def test_no_admission_check_without_x402(self, gate_client, monkeypatch):
        monkeypatch.setattr(settings, "X402_ENABLED", False)
        _fill_request_slots()
        r = await gate_client.post("/api/v1/stamps/", headers=PAYMENT)
        assert r.status_code == 200


class TestDisabled:

    @pytest.mark.asyncio
    async def test_disabled_gate_is_exactly_the_old_behaviour(self, gate_client, monkeypatch):
        monkeypatch.setattr(settings, "BEE_CALL_GATE_ENABLED", False)
        monkeypatch.setattr(settings, "X402_ENABLED", True)
        _fill_request_slots()  # would refuse everything if the gate were on
        before = dict(bee_call_gate.held)

        assert (await gate_client.get("/bee")).json() == {"status": 200}
        assert (await gate_client.get("/bee-then-502")).json() == {"ok": True}
        # No pre-check on paid routes.
        assert (await gate_client.post("/api/v1/stamps/", headers=PAYMENT)).json() == {"ran": True}
        # No rewrite.
        r = await gate_client.get("/no-bee/502")
        assert r.status_code == 502 and "retry-after" not in r.headers
        # No slot accounting.
        assert bee_call_gate.held == before

    @pytest.mark.asyncio
    async def test_disabled_gate_does_not_cap_calls(self, shared, monkeypatch):
        monkeypatch.setattr(settings, "BEE_CALL_GATE_ENABLED", False)
        shared.release.clear()
        client = hc.get_client()
        tasks = [(await as_request(lambda: client.get(url("wallet"))))[1] for _ in range(10)]
        await until(lambda: shared.in_flight == 10)
        shared.release.set()
        assert all(r.status_code == 200 for r in await asyncio.gather(*tasks))


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

class TestSettings:

    def _settings(self, **kw):
        return Settings(_env_file=None, SWARM_BEE_API_URL="http://localhost:1633", **kw)

    def test_defaults_are_valid_and_below_the_pool(self):
        s = self._settings()
        assert s.BEE_CALL_GATE_ENABLED is True
        assert s.BEE_MAX_CONCURRENT_CALLS < s.BEE_HTTP_MAX_CONNECTIONS
        assert s.BEE_MAX_CONCURRENT_TRANSFERS <= s.BEE_MAX_CONCURRENT_CALLS - s.BEE_RESERVED_BACKGROUND_CALLS

    @pytest.mark.parametrize("kw, text", [
        ({"BEE_MAX_CONCURRENT_CALLS": 100}, "below BEE_HTTP_MAX_CONNECTIONS"),
        ({"BEE_MAX_CONCURRENT_CALLS": 20, "BEE_HTTP_MAX_CONNECTIONS": 20}, "below BEE_HTTP_MAX_CONNECTIONS"),
        ({"BEE_RESERVED_BACKGROUND_CALLS": 64}, "BEE_RESERVED_BACKGROUND_CALLS"),
        ({"BEE_MAX_CONCURRENT_TRANSFERS": 57}, "BEE_MAX_CONCURRENT_TRANSFERS"),
    ])
    def test_inconsistent_sizes_refuse_to_start(self, kw, text):
        with pytest.raises(ValidationError) as exc:
            self._settings(**kw)
        assert text in str(exc.value)

    @pytest.mark.parametrize("pool, sizes", [(100, (64, 8, 24)), (40, (26, 3, 10)), (10, (6, 1, 2)),
                                             (2, (1, 0, 1))])
    def test_sizes_follow_a_lowered_pool_when_not_set(self, pool, sizes):
        """A deployment that lowers BEE_HTTP_MAX_CONNECTIONS still starts (#591 sets 40)."""
        s = self._settings(BEE_HTTP_MAX_CONNECTIONS=pool)
        got = (s.BEE_MAX_CONCURRENT_CALLS, s.BEE_RESERVED_BACKGROUND_CALLS, s.BEE_MAX_CONCURRENT_TRANSFERS)
        assert got == sizes
        assert got[0] < pool and got[1] < got[0] and got[2] <= got[0] - got[1]

    def test_explicit_sizes_are_kept(self):
        s = self._settings(BEE_HTTP_MAX_CONNECTIONS=40, BEE_MAX_CONCURRENT_CALLS=30,
                           BEE_RESERVED_BACKGROUND_CALLS=5, BEE_MAX_CONCURRENT_TRANSFERS=12)
        assert (s.BEE_MAX_CONCURRENT_CALLS, s.BEE_RESERVED_BACKGROUND_CALLS,
                s.BEE_MAX_CONCURRENT_TRANSFERS) == (30, 5, 12)

    @pytest.mark.parametrize("kw, text", [
        ({"BEE_HTTP_MAX_CONNECTIONS": 40, "BEE_MAX_CONCURRENT_CALLS": 64}, "below BEE_HTTP_MAX_CONNECTIONS"),
        ({"BEE_HTTP_MAX_CONNECTIONS": 40, "BEE_MAX_CONCURRENT_TRANSFERS": 30}, "BEE_MAX_CONCURRENT_TRANSFERS"),
        ({"BEE_HTTP_MAX_CONNECTIONS": 40, "BEE_RESERVED_BACKGROUND_CALLS": 26}, "BEE_RESERVED_BACKGROUND_CALLS"),
        ({"BEE_HTTP_MAX_CONNECTIONS": 1}, "below BEE_HTTP_MAX_CONNECTIONS"),
    ])
    def test_explicit_invalid_sizes_with_a_lowered_pool_refuse_to_start(self, kw, text):
        with pytest.raises(ValidationError) as exc:
            self._settings(**kw)
        assert text in str(exc.value)

    def test_the_40_connection_pool_starts_the_app(self):
        """The same check as #591's alert test, at the settings level and in a new process."""
        import os
        import subprocess
        import sys
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        code = ("from app.core.config import settings as s; "
                "print(s.BEE_MAX_CONCURRENT_CALLS, s.BEE_RESERVED_BACKGROUND_CALLS, s.BEE_MAX_CONCURRENT_TRANSFERS)")
        env = dict(os.environ, BEE_HTTP_MAX_CONNECTIONS="40")
        for k in ("BEE_MAX_CONCURRENT_CALLS", "BEE_RESERVED_BACKGROUND_CALLS", "BEE_MAX_CONCURRENT_TRANSFERS"):
            env.pop(k, None)
        out = subprocess.run([sys.executable, "-c", code], cwd=root, env=env,
                             capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        assert out.stdout.split() == ["26", "3", "10"]

    def test_disabled_gate_skips_the_size_checks(self):
        s = self._settings(BEE_CALL_GATE_ENABLED=False, BEE_MAX_CONCURRENT_CALLS=500)
        assert s.BEE_MAX_CONCURRENT_CALLS == 500
