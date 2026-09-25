"""The global rate limiter is active when x402 is enabled (#352).

Production runs with RATE_LIMIT_ENABLED and X402_ENABLED both true. The limiter
used to be installed only when x402 was off, which left every route outside the
x402 free-tier check unlimited.
"""
import os
import subprocess
import sys

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


def test_billed_chunk_uploads_are_not_counted(monkeypatch):
    _composed(monkeypatch, per_minute=2)
    app = FastAPI()

    @app.post("/api/v1/chunks/")
    async def chunk():
        return {"ok": True}

    app.add_middleware(RateLimitMiddleware, counter=SlidingWindowCounter())
    client = TestClient(app)
    assert [client.post("/api/v1/chunks/").status_code for _ in range(5)] == [200] * 5


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
