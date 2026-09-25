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
