"""Client-facing details of the payment flow (#385)."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.core.config import settings


def _request(headers, path="/api/v1/stamps/"):
    return Request({"type": "http", "method": "POST", "path": path, "query_string": b"",
                    "scheme": "http", "server": ("gateway.example", 80),
                    "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]})


class TestPublicResourceUrl:
    def test_behind_the_proxy_the_resource_is_https(self, monkeypatch):
        from app.x402.middleware import public_url
        monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)
        req = _request({"Host": "gateway.example", "X-Forwarded-Proto": "https"})
        assert public_url(req) == "https://gateway.example/api/v1/stamps/"

    def test_without_a_trusted_proxy_the_header_is_ignored(self, monkeypatch):
        from app.x402.middleware import public_url
        monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 0)
        req = _request({"Host": "gateway.example", "X-Forwarded-Proto": "https"})
        assert public_url(req).startswith("http://")

    def test_a_nonsense_proto_is_ignored(self, monkeypatch):
        from app.x402.middleware import public_url
        monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)
        req = _request({"Host": "gateway.example", "X-Forwarded-Proto": "javascript"})
        assert public_url(req).startswith("http://")

    def test_the_402_names_the_public_url(self, monkeypatch):
        from app.x402.middleware import create_payment_requirements
        monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)
        req = _request({"Host": "gateway.example", "X-Forwarded-Proto": "https"})
        assert create_payment_requirements(req, 0.05).resource == "https://gateway.example/api/v1/stamps/"


# Every header a browser client must be able to read. The x402 v2 names
# (PAYMENT-REQUIRED, PAYMENT-RESPONSE) are listed before the gateway sends them,
# so v2 browser clients can read them once it does (#603, epic #596).
EXPOSED_HEADERS = (
    "x-payment-response", "x-payment-mode", "x-payment-transaction",
    "retry-after", "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset",
    "x-swarm-reference", "content-disposition",
    "payment-required", "payment-response",
)


def _exposed(response):
    return {h.strip().lower() for h in response.headers.get("access-control-expose-headers", "").split(",")}


def test_cors_exposes_payment_and_rate_limit_headers():
    from app.main import app
    origin = settings.get_cors_origins()[0] if settings.get_cors_origins() != ["*"] else "https://app.example"
    r = TestClient(app).get("/", headers={"Origin": origin})
    exposed = _exposed(r)
    for h in EXPOSED_HEADERS:
        assert h in exposed, h


def test_cors_exposes_v2_payment_headers_on_402_and_success():
    """With x402 on, a browser's 402 and its 200 both expose the v2 names (#603)."""
    import json
    import os
    import subprocess
    import sys
    code = (
        "import json\n"
        "from fastapi.testclient import TestClient\n"
        "from app.main import app\n"
        "c = TestClient(app)\n"
        "h = {'Origin': 'https://example.org'}\n"
        "paid = c.post('/api/v1/data/?stamp_id=' + 'ab' * 32, headers=h,\n"
        "              files={'file': ('a.txt', b'hi')})\n"
        "ok = c.get('/', headers=h)\n"
        "print(json.dumps([[r.status_code, r.headers.get('access-control-expose-headers', '')]\n"
        "                  for r in (paid, ok)]))\n"
    )
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {**os.environ, "X402_ENABLED": "true", "X402_PAY_TO_ADDRESS": "0xpayee",
           "CORS_ALLOWED_ORIGINS": "*", "RATE_LIMIT_ENABLED": "false",
           "STAMP_POOL_ENABLED": "false", "METRICS_ENABLED": "false",
           "SWARM_BEE_API_URL": "http://localhost:1"}
    out = subprocess.run([sys.executable, "-c", code], cwd=repo, env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    (paid_status, paid_exposed), (ok_status, ok_exposed) = json.loads(out.stdout.strip().splitlines()[-1])
    assert paid_status == 402
    assert ok_status == 200
    for raw in (paid_exposed, ok_exposed):
        exposed = {h.strip().lower() for h in raw.split(",")}
        for h in EXPOSED_HEADERS:
            assert h in exposed, (h, raw)


@pytest.mark.parametrize("mode", ["free", "free-tier", "FREE-TIER"])
def test_the_echoed_free_tier_value_is_accepted_as_an_opt_in(monkeypatch, mode):
    """Responses say X-Payment-Mode: free-tier; sending that back must work too."""
    import asyncio
    from unittest.mock import AsyncMock, patch
    from app.x402 import dependency
    monkeypatch.setattr(settings, "X402_ENABLED", True)
    monkeypatch.setattr(settings, "X402_FREE_TIER_ENABLED", True)
    req = _request({"X-Payment-Mode": mode})
    req.state.x402_mode = None
    with patch.object(dependency, "_calculate_price_for_request",
                      new=AsyncMock(return_value={"price_usd": 0.05, "description": "x"})), \
         patch.object(dependency, "check_rate_limit", return_value=(True, None, {"requests_made": 1, "limit": 3})):
        asyncio.run(dependency.require_x402_payment(req))
    assert req.state.x402_mode == "free-tier"


def test_the_free_tier_429_says_when_to_retry(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock, patch
    from fastapi import HTTPException
    from app.x402 import dependency
    monkeypatch.setattr(settings, "X402_ENABLED", True)
    monkeypatch.setattr(settings, "X402_FREE_TIER_ENABLED", True)
    req = _request({"X-Payment-Mode": "free"})
    stats = {"requests_made": 3, "limit": 3, "remaining": 0, "window_seconds": 60}
    with patch.object(dependency, "_calculate_price_for_request",
                      new=AsyncMock(return_value={"price_usd": 0.05, "description": "x"})), \
         patch.object(dependency, "check_rate_limit", return_value=(False, "limit", stats)):
        with pytest.raises(HTTPException) as e:
            asyncio.run(dependency.require_x402_payment(req))
    assert e.value.status_code == 429
    assert e.value.headers["Retry-After"] == "60"
