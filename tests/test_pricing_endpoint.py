"""GET /api/v1/pricing (#381): quotes without paying, equal to what the 402 asks."""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

from app.api.endpoints import pricing
from app.core.config import settings
from app.middleware.rate_limit import _is_exempt_path
from app.x402.dependency import require_x402_payment
from app.x402.middleware import is_protected_endpoint
from app.x402.ratelimit import reset_rate_limiter

# Large enough that depth and duration move the price above the minimum.
CHAINSTATE = {"currentPrice": "240000", "chainTip": 1, "block": 1}
OK_BALANCE = {"ok": True, "is_critical": False, "balance_eth": 0.01}


async def _ok():
    return {"ok": True}


@pytest.fixture
def x402_on(monkeypatch):
    for k, v in (("X402_ENABLED", True), ("X402_FREE_TIER_ENABLED", True),
                 ("STAMP_POOL_ENABLED", True), ("CHUNK_UPLOAD_ENABLED", True),
                 ("X402_PAY_TO_ADDRESS", "0xpayee"), ("X402_NETWORK", "base-sepolia")):
        monkeypatch.setattr(settings, k, v)
    reset_rate_limiter()
    with patch("app.x402.pricing.get_chainstate", new=AsyncMock(return_value=CHAINSTATE)), \
         patch("app.x402.dependency.check_base_eth_balance", new=AsyncMock(return_value=OK_BALANCE)):
        yield
    reset_rate_limiter()


@pytest.fixture
def client(x402_on):
    """The pricing router next to the paid routes, each behind the real x402 check."""
    app = FastAPI()
    app.include_router(pricing.router, prefix="/api/v1")
    paid = APIRouter(dependencies=[Depends(require_x402_payment)])
    for path in ("/api/v1/stamps/", "/api/v1/data/", "/api/v1/chunks/credit"):
        paid.add_api_route(path, _ok, methods=["POST"])
    app.include_router(paid)
    return TestClient(app)


def _402_amount(client, path, **kw):
    r = client.post(path, **kw)
    assert r.status_code == 402
    return r.json()["detail"]["accepts"][0]["maxAmountRequired"]


def test_quotes_every_enabled_operation_without_payment(client):
    r = client.get("/api/v1/pricing")
    assert r.status_code == 200
    body = r.json()
    assert body["x402_enabled"] is True
    assert body["currency"] == "USDC" and body["pay_to"] == "0xpayee"
    assert set(body["quotes"]) == {"stamp_purchase", "pool_acquire", "data_upload", "bandwidth_credit"}
    for q in body["quotes"].values():
        assert q["price_usd"] >= settings.X402_MIN_PRICE_USD
        assert q["max_amount_required"] == str(int(q["price_usd"] * 1_000_000))


@pytest.mark.parametrize("query,path,kw", [
    ("", "/api/v1/stamps/", {"json": {}}),
    ("size=large&duration_hours=200", "/api/v1/stamps/", {"json": {"size": "large", "duration_hours": 200}}),
    ("upload_bytes=5000", "/api/v1/data/", {"content": b"x" * 5000}),
    ("mb=5000", "/api/v1/chunks/credit?mb=5000", {}),
])
def test_quote_equals_what_the_402_asks(client, query, path, kw):
    """Parity: the pricing endpoint and the 402 come from the same pricer."""
    op = {"stamps": "stamp_purchase", "data": "data_upload", "chunks": "bandwidth_credit"}[path.split("/")[3]]
    quote = client.get(f"/api/v1/pricing?{query}").json()["quotes"][op]
    assert quote["max_amount_required"] == _402_amount(client, path, **kw)


def test_pool_quote_includes_the_pool_premium(client):
    q = client.get("/api/v1/pricing?size=medium").json()["quotes"]
    assert "pool premium" in q["pool_acquire"]["description"]


def test_disabled_operations_are_not_listed(client, monkeypatch):
    monkeypatch.setattr(settings, "STAMP_POOL_ENABLED", False)
    monkeypatch.setattr(settings, "CHUNK_UPLOAD_ENABLED", False)
    assert set(client.get("/api/v1/pricing").json()["quotes"]) == {"stamp_purchase", "data_upload"}


def test_invalid_query_is_422(client):
    assert client.get("/api/v1/pricing?depth=99").status_code == 422
    assert client.get("/api/v1/pricing?duration_hours=1").status_code == 422
    assert client.get("/api/v1/pricing?size=huge").status_code == 422


def test_x402_disabled_returns_no_quotes(monkeypatch):
    monkeypatch.setattr(settings, "X402_ENABLED", False)
    app = FastAPI()
    app.include_router(pricing.router, prefix="/api/v1")
    assert TestClient(app).get("/api/v1/pricing").json() == {"x402_enabled": False, "quotes": {}}


def test_chain_price_unavailable_is_503_with_code(x402_on):
    app = FastAPI()
    app.include_router(pricing.router, prefix="/api/v1")
    with patch("app.x402.pricing.get_chainstate", new=AsyncMock(side_effect=RuntimeError("bee down"))):
        r = TestClient(app).get("/api/v1/pricing")
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "PRICING_UNAVAILABLE"


def test_never_payment_gated_and_rate_limited_like_other_gets():
    assert not is_protected_endpoint("GET", "/api/v1/pricing")
    assert not _is_exempt_path("/api/v1/pricing")


def test_mounted_on_the_real_app_without_x402_dependency(x402_on):
    from app.main import app
    assert pricing.router.dependencies == []
    r = TestClient(app).get("/api/v1/pricing")
    assert r.status_code == 200 and r.json()["x402_enabled"] is True
