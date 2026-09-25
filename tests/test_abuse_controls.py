"""Blocklist, IPv6 grouping and body limits (#354, #367, #379)."""
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.core.client_ip import client_key
from app.middleware.access_list import AccessListMiddleware
from app.middleware.body_limit import BodyLimitMiddleware


@pytest.mark.parametrize("ip,key", [
    ("203.0.113.7", "203.0.113.7"),
    ("2001:db8:1:2:aaaa::1", "2001:db8:1:2::/64"),
    ("2001:db8:1:2:ffff:ffff:ffff:ffff", "2001:db8:1:2::/64"),
    ("::ffff:203.0.113.7", "203.0.113.7"),
    ("testclient", "testclient"),
])
def test_client_key_groups_ipv6_by_64(ip, key):
    assert client_key(ip) == key


def _app(mw, **kw):
    app = FastAPI()

    @app.post("/echo")
    async def echo(request: Request):
        return {"n": len(await request.body())}

    @app.get("/x")
    async def x():
        return {"ok": True}

    app.add_middleware(mw, **kw)
    return app


def test_blocked_addresses_are_refused_everywhere(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)
    client = TestClient(_app(AccessListMiddleware, blocked="203.0.113.0/24, 2001:db8:1:2::/64"))
    assert client.get("/x", headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 403
    assert client.get("/x", headers={"X-Forwarded-For": "2001:db8:1:2::77"}).status_code == 403
    assert client.get("/x", headers={"X-Forwarded-For": "198.51.100.1"}).status_code == 200


@pytest.mark.parametrize("headers", [{}, {"content-type": "application/merge-patch+json"}])
def test_json_limits_apply_without_or_with_variant_content_type(monkeypatch, headers):
    from app.core.config import settings
    monkeypatch.setattr(settings, "MAX_JSON_BODY_BYTES", 100)
    monkeypatch.setattr(settings, "MAX_JSON_DEPTH", 3)
    client = TestClient(_app(BodyLimitMiddleware))
    assert client.post("/echo", content=b"[" * 10 + b"]" * 10, headers=headers).status_code == 400
    assert client.post("/echo", content=b"x" * 200, headers=headers).status_code == 413


def test_binary_and_multipart_bodies_are_not_inspected(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "MAX_JSON_BODY_BYTES", 100)
    client = TestClient(_app(BodyLimitMiddleware))
    r = client.post("/echo", content=b"[" * 500, headers={"content-type": "application/octet-stream"})
    assert r.status_code == 200


def test_ipv6_rotation_shares_one_spend_budget(monkeypatch, tmp_path):
    """Two addresses from one /64 draw on the same daily budget."""
    from unittest.mock import AsyncMock, patch
    from app.core.config import settings
    from app.main import app
    from app.services.spend_budget import SpendBudgetTracker
    import app.api.endpoints.stamps as stamps_ep
    tracker = SpendBudgetTracker(state_file=str(tmp_path / "s.json"))
    monkeypatch.setattr(stamps_ep, "spend_budget_tracker", tracker)
    monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", 0.008)   # one ~0.0057 BZZ batch
    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)
    codes = []
    for ip in ["2001:db8:1:2::1", "2001:db8:1:2::2"]:
        with patch("app.services.swarm_api.get_chainstate",
                   new=AsyncMock(return_value={"currentPrice": "24000", "minimumValidityBlocks": 17280})), \
             patch("app.services.swarm_api.check_sufficient_funds",
                   new=AsyncMock(return_value={"sufficient": True, "wallet_balance_bzz": 1.0,
                                               "required_bzz": 0.01, "shortfall_bzz": 0.0})), \
             patch("app.services.swarm_api.purchase_postage_stamp", new=AsyncMock(return_value="b" * 64)):
            codes.append(TestClient(app).post("/api/v1/stamps/", json={"depth": 17, "duration_hours": 24},
                                              headers={"X-Forwarded-For": ip}).status_code)
    assert codes == [201, 429]



def test_binary_chunks_without_content_type_are_not_depth_scanned(monkeypatch):
    """Random chunk bytes can look like deeply nested JSON; the chunk route is exempt."""
    from app.core.config import settings
    monkeypatch.setattr(settings, "MAX_JSON_DEPTH", 3)
    app = FastAPI()

    @app.post("/api/v1/chunks/")
    async def chunk(request: Request):
        return {"n": len(await request.body())}

    app.add_middleware(BodyLimitMiddleware)
    r = TestClient(app).post("/api/v1/chunks/", content=b"[" * 200, headers={"content-type": ""})
    assert r.status_code == 200


def test_ipv4_mapped_addresses_match_ipv4_blocks(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)
    client = TestClient(_app(AccessListMiddleware, blocked="203.0.113.0/24"))
    assert client.get("/x", headers={"X-Forwarded-For": "::ffff:203.0.113.9"}).status_code == 403


def test_blocklist_is_wired_into_the_app():
    import os, subprocess, sys
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = ("from fastapi.testclient import TestClient\n"
            "from app.main import app\n"
            "c = TestClient(app)\n"
            "print(c.get('/api/v1/pool/status', headers={'X-Forwarded-For': '203.0.113.9'}).status_code)\n")
    env = {**os.environ, "X402_BLACKLIST_IPS": "203.0.113.0/24", "TRUSTED_PROXY_HOPS": "1",
           "SWARM_BEE_API_URL": "http://localhost:1",
           "STAMP_POOL_ENABLED": "false", "METRICS_ENABLED": "false"}
    out = subprocess.run([sys.executable, "-c", code], cwd=repo, env=env, capture_output=True, text=True, timeout=60)
    assert out.stdout.strip().splitlines()[-1] == "403", out.stderr[-1500:]


def test_free_tier_rate_limit_groups_ipv6(monkeypatch):
    from app.x402.ratelimit import check_rate_limit, reset_rate_limiter
    from app.core.client_ip import client_key
    from app.core.config import settings
    monkeypatch.setattr(settings, "X402_FREE_TIER_RATE_LIMIT", 2)
    reset_rate_limiter()
    results = [check_rate_limit(client_key(f"2001:db8:9:9::{i}"), is_free_tier=True)[0] for i in range(1, 4)]
    reset_rate_limiter()
    assert results == [True, True, False]
