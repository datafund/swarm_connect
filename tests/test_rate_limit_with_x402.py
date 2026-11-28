"""The global rate limiter is active when x402 is enabled (#352).

Production runs with RATE_LIMIT_ENABLED and X402_ENABLED both true. The limiter
used to be installed only when x402 was off, which left every route outside the
x402 free-tier check unlimited.
"""
import os
import subprocess
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.middleware.rate_limit import RateLimitMiddleware, SlidingWindowCounter
from app.x402.middleware import X402Middleware

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_app_installs_the_limiter_when_x402_is_on():
    code = (
        "from app.main import app\n"
        "from app.middleware.rate_limit import RateLimitMiddleware\n"
        "print(any(m.cls is RateLimitMiddleware for m in app.user_middleware))\n"
    )
    env = {**os.environ, "RATE_LIMIT_ENABLED": "true", "X402_ENABLED": "true",
           "X402_PAY_TO_ADDRESS": "0xpayee", "SWARM_BEE_API_URL": "http://localhost:1"}
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.stdout.strip().splitlines()[-1] == "True", out.stderr[-2000:]


def test_unprotected_route_is_limited_alongside_x402(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(settings, "RATE_LIMIT_PER_MINUTE", 3)
    monkeypatch.setattr(settings, "RATE_LIMIT_BURST", 0)
    monkeypatch.setattr(settings, "X402_ENABLED", True)

    app = FastAPI()

    @app.get("/api/v1/data/{ref}")
    async def download(ref: str):
        return {"ref": ref}

    app.add_middleware(RateLimitMiddleware, counter=SlidingWindowCounter())
    app.add_middleware(X402Middleware)
    client = TestClient(app)
    codes = [client.get("/api/v1/data/abc").status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def _composed(monkeypatch, per_minute=3):
    from app.core.config import settings
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(settings, "RATE_LIMIT_PER_MINUTE", per_minute)
    monkeypatch.setattr(settings, "RATE_LIMIT_BURST", 0)
    monkeypatch.setattr(settings, "X402_ENABLED", True)
    return settings


def _chunk_app():
    """A stub upload route that always marks the request billed, so only the
    exemption decision (not the count of unbilled uploads) can limit it."""
    from fastapi import Request
    app = FastAPI()

    @app.post("/api/v1/chunks/")
    async def chunk(request: Request):
        request.state.chunk_billed = True
        return {"ok": True}

    app.add_middleware(RateLimitMiddleware, counter=SlidingWindowCounter())
    return TestClient(app)


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    """A fresh credit ledger in place of the singleton the limiter reads."""
    from app.services import bandwidth_credit
    manager = bandwidth_credit.BandwidthCreditManager(state_file=str(tmp_path / "credit.json"))
    monkeypatch.setattr(bandwidth_credit, "bandwidth_credit_manager", manager)
    return manager


def _funded_token(ledger, balance=1_000_000, address="0xfunded"):
    if balance:
        ledger.credit(address, balance)
    return ledger.issue_token(address)


def test_credit_funded_chunk_uploads_are_not_counted(monkeypatch, ledger):
    _composed(monkeypatch, per_minute=2)
    token = _funded_token(ledger)
    client = _billing_app([201] * 5)
    codes = [client.post("/api/v1/chunks/", content=b"x" * 4104,
                         headers={"X-Bandwidth-Credit-Token": token}).status_code
             for _ in range(5)]
    assert codes == [201] * 5


@pytest.mark.parametrize("headers", [
    pytest.param({}, id="no-token"),
    pytest.param({"X-Payment-Mode": "free"}, id="free-path"),
    pytest.param({"X-Bandwidth-Credit-Token": "not-a-token"}, id="unknown-token"),
])
def test_chunk_uploads_without_funded_credit_are_limited(monkeypatch, ledger, headers):
    """#696: these used to be exempt whenever x402 was on."""
    _composed(monkeypatch, per_minute=2)
    client = _chunk_app()
    codes = [client.post("/api/v1/chunks/", content=b"x", headers=headers).status_code
             for _ in range(3)]
    assert codes == [200, 200, 429]


def test_free_path_with_a_funded_token_is_limited(monkeypatch, ledger):
    """The endpoint takes the free path when it is asked for, so the token pays nothing."""
    _composed(monkeypatch, per_minute=2)
    token = _funded_token(ledger)
    client = _chunk_app()
    h = {"X-Bandwidth-Credit-Token": token, "X-Payment-Mode": "free"}
    codes = [client.post("/api/v1/chunks/", content=b"x", headers=h).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


@pytest.mark.parametrize("balance", [0, 4103], ids=["empty", "short-by-one"])
def test_token_without_enough_balance_is_limited(monkeypatch, ledger, balance):
    """A valid token whose balance cannot pay for the declared body is counted."""
    _composed(monkeypatch, per_minute=2)
    token = _funded_token(ledger, balance=balance)
    client = _chunk_app()
    codes = [client.post("/api/v1/chunks/", content=b"x" * 4104,
                         headers={"X-Bandwidth-Credit-Token": token}).status_code
             for _ in range(3)]
    assert codes == [200, 200, 429]


def test_balance_equal_to_the_declared_length_is_exempt(monkeypatch, ledger):
    _composed(monkeypatch, per_minute=2)
    token = _funded_token(ledger, balance=4104)
    client = _billing_app([201] * 4)
    codes = [client.post("/api/v1/chunks/", content=b"x" * 4104,
                         headers={"X-Bandwidth-Credit-Token": token}).status_code
             for _ in range(4)]
    assert codes == [201] * 4


def test_rotated_token_is_limited(monkeypatch, ledger):
    _composed(monkeypatch, per_minute=2)
    old = _funded_token(ledger)
    ledger.issue_token("0xfunded", rotate=True)
    client = _chunk_app()
    codes = [client.post("/api/v1/chunks/", content=b"x",
                         headers={"X-Bandwidth-Credit-Token": old}).status_code
             for _ in range(3)]
    assert codes == [200, 200, 429]


def _chunked(n=10):
    """A body sent with chunked transfer encoding, so no Content-Length."""
    def gen():
        for _ in range(n):
            yield b"x"
    return gen()


@pytest.mark.parametrize("body", [
    pytest.param("chunked", id="chunked-no-content-length"),
    pytest.param("empty", id="empty-body"),
])
def test_zero_balance_token_without_a_length_is_limited(monkeypatch, ledger, body):
    """The floor of one byte: an empty account is not exempt just because the
    request declares no length, or a zero one."""
    _composed(monkeypatch, per_minute=2)
    token = _funded_token(ledger, balance=0)
    client = _chunk_app()
    h = {"X-Bandwidth-Credit-Token": token}
    codes = [client.post("/api/v1/chunks/", content=_chunked() if body == "chunked" else b"",
                         headers=h).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


@pytest.mark.parametrize("path", ["/api/v1/chunks", "/api/v1/chunks///", "/api/v1/chunks//"])
def test_other_spellings_of_the_chunk_path_are_not_exempt(monkeypatch, ledger, path):
    from fastapi import Request
    _composed(monkeypatch, per_minute=2)
    token = _funded_token(ledger)
    app = FastAPI()

    @app.post(path)
    async def stub(request: Request):
        request.state.chunk_billed = True
        return {"ok": True}

    app.add_middleware(RateLimitMiddleware, counter=SlidingWindowCounter())
    client = TestClient(app)
    codes = [client.post(path, content=b"x", headers={"X-Bandwidth-Credit-Token": token}).status_code
             for _ in range(3)]
    assert codes == [200, 200, 429]


def _billing_app(billed_codes):
    """A stub upload route that answers the next code from `billed_codes`
    and marks the request billed only for 201, as the endpoint does."""
    from fastapi import Request
    from fastapi.responses import JSONResponse
    app = FastAPI()
    codes = iter(billed_codes)

    @app.post("/api/v1/chunks/")
    async def chunk(request: Request):
        code = next(codes)
        if code == 201:
            request.state.chunk_billed = True
        return JSONResponse({"code": code}, status_code=code)

    app.add_middleware(RateLimitMiddleware, counter=SlidingWindowCounter())
    return TestClient(app)


def test_unbilled_funded_uploads_are_counted_afterwards(monkeypatch, ledger):
    _composed(monkeypatch, per_minute=2)
    token = _funded_token(ledger)
    client = _billing_app([502, 400, 201])
    h = {"X-Bandwidth-Credit-Token": token}
    codes = [client.post("/api/v1/chunks/", content=b"x", headers=h).status_code for _ in range(3)]
    assert codes == [502, 400, 429]


def test_billed_uploads_do_not_use_the_unbilled_budget(monkeypatch, ledger):
    _composed(monkeypatch, per_minute=2)
    token = _funded_token(ledger)
    client = _billing_app([201] * 5 + [502, 502, 201])
    h = {"X-Bandwidth-Credit-Token": token}
    codes = [client.post("/api/v1/chunks/", content=b"x", headers=h).status_code for _ in range(9)]
    assert codes == [201] * 5 + [502, 502, 429, 429]


def test_unbilled_funded_uploads_do_not_use_the_general_budget(monkeypatch, ledger):
    """Their own key: a funded client's other requests keep their full budget."""
    _composed(monkeypatch, per_minute=2)
    token = _funded_token(ledger)
    app = FastAPI()

    @app.post("/api/v1/chunks/")
    async def refused():
        from fastapi.responses import JSONResponse
        return JSONResponse({}, status_code=400)

    @app.get("/api/v1/stamps/")
    async def other():
        return {"ok": True}

    app.add_middleware(RateLimitMiddleware, counter=SlidingWindowCounter())
    client = TestClient(app)
    h = {"X-Bandwidth-Credit-Token": token}
    assert [client.post("/api/v1/chunks/", content=b"x", headers=h).status_code
            for _ in range(3)] == [400, 400, 429]
    assert [client.get("/api/v1/stamps/").status_code for _ in range(3)] == [200, 200, 429]


def test_chunk_uploads_are_limited_when_billing_is_off(monkeypatch):
    settings = _composed(monkeypatch, per_minute=2)
    monkeypatch.setattr(settings, "X402_ENABLED", False)
    app = FastAPI()

    @app.post("/api/v1/chunks/")
    async def chunk():
        return {"ok": True}

    app.add_middleware(RateLimitMiddleware, counter=SlidingWindowCounter())
    client = TestClient(app)
    assert [client.post("/api/v1/chunks/").status_code for _ in range(3)] == [200, 200, 429]


def test_inner_limiter_headers_are_not_overwritten(monkeypatch):
    """A free-tier 429 keeps the free-tier limit in its headers."""
    from fastapi import HTTPException
    _composed(monkeypatch, per_minute=50)
    app = FastAPI()

    @app.post("/api/v1/stamps/")
    async def free_tier_refused():
        raise HTTPException(status_code=429, detail="free tier",
                            headers={"X-RateLimit-Limit": "3", "X-RateLimit-Remaining": "0"})

    app.add_middleware(RateLimitMiddleware, counter=SlidingWindowCounter())
    r = TestClient(app).post("/api/v1/stamps/")
    assert r.status_code == 429
    assert r.headers["X-RateLimit-Limit"] == "3"
    assert r.headers["X-RateLimit-Remaining"] == "0"


def test_only_the_real_openapi_path_is_exempt(monkeypatch):
    from app.middleware.rate_limit import _is_exempt_path
    assert _is_exempt_path("/api/v1/openapi.json")
    assert not _is_exempt_path("/api/v1/data/openapi.json")


def test_production_app_limits_with_cors_and_exempt_health():
    code = (
        "from fastapi.testclient import TestClient\n"
        "from app.main import app\n"
        "c = TestClient(app)\n"
        "h = {'Origin': 'https://example.org'}\n"
        "codes = [c.get('/api/v1/pool/status', headers=h) for _ in range(3)]\n"
        "print([r.status_code for r in codes])\n"
        "print(codes[-1].headers.get('access-control-allow-origin'))\n"
        "print([c.get('/health').status_code != 429 for _ in range(4)])\n"
    )
    env = {**os.environ, "RATE_LIMIT_ENABLED": "true", "X402_ENABLED": "true",
           "RATE_LIMIT_PER_MINUTE": "2", "RATE_LIMIT_BURST": "0", "STAMP_POOL_ENABLED": "false",
           "METRICS_ENABLED": "false", "X402_PAY_TO_ADDRESS": "0xpayee",
           "SWARM_BEE_API_URL": "http://localhost:1"}
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env,
                         capture_output=True, text=True, timeout=60)
    lines = out.stdout.strip().splitlines()
    assert lines[-3].endswith("429]"), out.stdout + out.stderr[-2000:]
    assert lines[-2] == "*"
    assert lines[-1] == "[True, True, True, True]"


def test_production_app_limits_chunk_uploads_without_credit(tmp_path):
    """#696 on the real app: unpaid chunk POSTs reach 429, funded ones do not."""
    code = (
        "from unittest.mock import AsyncMock, patch\n"
        "from fastapi.testclient import TestClient\n"
        "from app.main import app\n"
        "from app.services.bandwidth_credit import bandwidth_credit_manager as m\n"
        "m.credit('0xfunded', 1_000_000)\n"
        "token = m.issue_token('0xfunded')\n"
        "c = TestClient(app)\n"
        "body = b'x' * 4104\n"
        "stamp = {'Swarm-Postage-Stamp': 'a' * 226}\n"
        "with patch('app.api.endpoints.chunks.upload_chunk_to_swarm',\n"
        "           new=AsyncMock(return_value='ab' * 32)):\n"
        "    print([c.post('/api/v1/chunks/', content=body, headers=stamp).status_code\n"
        "           for _ in range(3)])\n"
        "    print([c.post('/api/v1/chunks/', content=body,\n"
        "                  headers={**stamp, 'X-Bandwidth-Credit-Token': token}).status_code\n"
        "           for _ in range(4)])\n"
        "print(m.balance('0xfunded'))\n"
    )
    env = {**os.environ, "RATE_LIMIT_ENABLED": "true", "X402_ENABLED": "true",
           "RATE_LIMIT_PER_MINUTE": "2", "RATE_LIMIT_BURST": "0", "STAMP_POOL_ENABLED": "false",
           "METRICS_ENABLED": "false", "X402_PAY_TO_ADDRESS": "0xpayee",
           "CHUNK_UPLOAD_ENABLED": "true",
           "BANDWIDTH_CREDIT_STATE_FILE": str(tmp_path / "credit.json"),
           "SWARM_BEE_API_URL": "http://localhost:1"}
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env,
                         capture_output=True, text=True, timeout=60)
    lines = out.stdout.strip().splitlines()
    assert lines[-3:] == ["[402, 402, 429]", "[201, 201, 201, 201]",
                          str(1_000_000 - 4 * 4104)], out.stdout + out.stderr[-2000:]


def test_production_app_counts_funded_uploads_that_end_unbilled(tmp_path):
    """#696 review: a funded token sending stamps Bee rejects (502, refunded) or
    malformed stamps (400) is limited; Bee is called only until then."""
    code = (
        "import httpx2\n"
        "from unittest.mock import AsyncMock, patch\n"
        "from fastapi.testclient import TestClient\n"
        "from app.main import app\n"
        "from app.services.bandwidth_credit import bandwidth_credit_manager as m\n"
        "m.credit('0xfunded', 1_000_000)\n"
        "token = m.issue_token('0xfunded')\n"
        "c = TestClient(app)\n"
        "h = {'Swarm-Postage-Stamp': 'a' * 226, 'X-Bandwidth-Credit-Token': token}\n"
        "bee = AsyncMock(side_effect=httpx2.HTTPError('stamp rejected'))\n"
        "with patch('app.api.endpoints.chunks.upload_chunk_to_swarm', new=bee):\n"
        "    print([c.post('/api/v1/chunks/', content=b'x' * 4104, headers=h).status_code\n"
        "           for _ in range(4)])\n"
        "print(bee.await_count, m.balance('0xfunded'))\n"
    )
    code_bad_stamp = (
        "from fastapi.testclient import TestClient\n"
        "from app.main import app\n"
        "from app.services.bandwidth_credit import bandwidth_credit_manager as m\n"
        "m.credit('0xfunded', 1_000_000)\n"
        "token = m.issue_token('0xfunded')\n"
        "c = TestClient(app)\n"
        "h = {'Swarm-Postage-Stamp': 'zz', 'X-Bandwidth-Credit-Token': token}\n"
        "print([c.post('/api/v1/chunks/', content=b'x' * 4104, headers=h).status_code\n"
        "       for _ in range(4)])\n"
    )
    env = {**os.environ, "RATE_LIMIT_ENABLED": "true", "X402_ENABLED": "true",
           "RATE_LIMIT_PER_MINUTE": "2", "RATE_LIMIT_BURST": "0", "STAMP_POOL_ENABLED": "false",
           "METRICS_ENABLED": "false", "X402_PAY_TO_ADDRESS": "0xpayee",
           "CHUNK_UPLOAD_ENABLED": "true", "SWARM_BEE_API_URL": "http://localhost:1"}
    for i, (src, expected) in enumerate([
        (code, ["[502, 502, 429, 429]", "2 1000000"]),
        (code_bad_stamp, ["[400, 400, 429, 429]"]),
    ]):
        env["BANDWIDTH_CREDIT_STATE_FILE"] = str(tmp_path / f"credit{i}.json")
        out = subprocess.run([sys.executable, "-c", src], cwd=REPO, env=env,
                             capture_output=True, text=True, timeout=60)
        lines = out.stdout.strip().splitlines()
        assert lines[-len(expected):] == expected, out.stdout + out.stderr[-2000:]


# --------------------------------------------------------------------------- #
# Concurrent credit-token uploads: the slot is reserved before the request runs
# (#696 review round 2). Checking first and counting afterwards let every
# request already in flight through, however many there were.
# --------------------------------------------------------------------------- #
def _real_upload_app(monkeypatch, ledger, forward, per_minute=2):
    """The real chunk router behind the real limiter, with Bee replaced by `forward`."""
    import app.api.endpoints.chunks as chunks
    settings = _composed(monkeypatch, per_minute=per_minute)
    monkeypatch.setattr(settings, "CHUNK_UPLOAD_ENABLED", True)
    monkeypatch.setattr(chunks, "bandwidth_credit_manager", ledger)
    monkeypatch.setattr(chunks, "upload_chunk_to_swarm", forward)
    app = FastAPI()
    app.include_router(chunks.router, prefix="/api/v1/chunks")
    app.add_middleware(RateLimitMiddleware, counter=SlidingWindowCounter())
    return app


def _concurrent_posts(app, n, headers):
    import asyncio
    import httpx2

    async def run():
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as c:
            rs = await asyncio.gather(*(c.post("/api/v1/chunks/", content=b"x" * 4104, headers=headers)
                                        for _ in range(n)))
        return sorted(r.status_code for r in rs)

    return asyncio.run(run())


def _slow_forward(fail, calls):
    import asyncio
    import httpx2

    async def forward(chunk, stamp, deferred=False):
        calls.append(1)
        await asyncio.sleep(0.5)
        if fail:
            raise httpx2.HTTPError("stamp rejected")
        return "ab" * 32

    return forward


def test_concurrent_unbilled_uploads_reach_bee_at_most_the_limit(monkeypatch, ledger):
    calls = []
    token = _funded_token(ledger)
    app = _real_upload_app(monkeypatch, ledger, _slow_forward(fail=True, calls=calls))
    codes = _concurrent_posts(app, 30, {"Swarm-Postage-Stamp": "a" * 226,
                                        "X-Bandwidth-Credit-Token": token})
    assert codes == [429] * 28 + [502] * 2
    assert len(calls) == 2
    assert ledger.balance("0xfunded") == 1_000_000


def test_sequential_billed_uploads_release_their_slot(monkeypatch, ledger):
    calls = []
    token = _funded_token(ledger)

    async def forward(chunk, stamp, deferred=False):
        calls.append(1)
        return "ab" * 32

    from fastapi.testclient import TestClient as TC
    client = TC(_real_upload_app(monkeypatch, ledger, forward))
    h = {"Swarm-Postage-Stamp": "a" * 226, "X-Bandwidth-Credit-Token": token}
    codes = [client.post("/api/v1/chunks/", content=b"x" * 4104, headers=h).status_code
             for _ in range(10)]
    assert codes == [201] * 10
    assert len(calls) == 10
    assert ledger.balance("0xfunded") == 1_000_000 - 10 * 4104


def test_concurrent_billed_uploads_are_capped_at_the_limit_in_flight(monkeypatch, ledger):
    """The trade-off of reserving first: one address can have at most the
    limit's worth of credit-token uploads in flight at once."""
    calls = []
    token = _funded_token(ledger)
    app = _real_upload_app(monkeypatch, ledger, _slow_forward(fail=False, calls=calls))
    codes = _concurrent_posts(app, 10, {"Swarm-Postage-Stamp": "a" * 226,
                                        "X-Bandwidth-Credit-Token": token})
    assert codes == [201] * 2 + [429] * 8
    assert len(calls) == 2


def test_release_removes_only_the_reserved_entry():
    counter = SlidingWindowCounter()
    first, _ = counter.reserve("k", 3)
    second, _ = counter.reserve("k", 3)
    counter.release("k", first)
    counter.release("k", first)  # a second release of the same entry is a no-op
    counter.release("k", second + 0.0)  # an equal timestamp that is not the entry
    assert counter._windows["k"] == [second]
    assert counter._windows["k"][0] is second
