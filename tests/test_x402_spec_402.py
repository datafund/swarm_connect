"""402 responses follow the x402 v1 shape (#372).

x402Version, error and accepts at the top level, so standard x402 clients can
parse and pay them. The previous nested copy under "detail" is kept for
existing clients.
"""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient
from x402.types import x402PaymentRequiredResponse

from app.x402.dependency import require_x402_payment
from app.x402.middleware import X402Middleware
from app.x402.ratelimit import reset_rate_limiter
from tests.test_x402_integration import OK_BALANCE, _configure


async def handler():
    return {"ok": True}


async def own_402():
    from fastapi import HTTPException
    raise HTTPException(status_code=402, detail={"code": "CREDIT_REQUIRED", "message": "top up"})


@pytest.fixture
def client():
    reset_rate_limiter()
    app = FastAPI()
    router = APIRouter(dependencies=[Depends(require_x402_payment)])
    router.add_api_route("/api/v1/stamps/", handler, methods=["POST"])
    app.include_router(router)
    app.add_api_route("/api/v1/chunks/", own_402, methods=["POST"])
    app.add_middleware(X402Middleware)
    with patch("app.x402.dependency.check_base_eth_balance", new=AsyncMock(return_value=OK_BALANCE)), \
         patch("app.x402.dependency._calculate_price_for_request",
               new=AsyncMock(return_value={"price_usd": 0.02, "description": "t"})), \
         patch("app.x402.middleware.settings") as mw, patch("app.x402.dependency.settings") as dep:
        _configure(dep, mw, free_tier=True)
        yield TestClient(app)
    reset_rate_limiter()


def test_payment_required_is_top_level_and_parses_with_the_x402_sdk(client):
    r = client.post("/api/v1/stamps/")
    assert r.status_code == 402
    body = r.json()
    parsed = x402PaymentRequiredResponse.model_validate(body)
    assert parsed.x402_version == 1
    assert parsed.accepts[0].max_amount_required == "20000"
    assert "freeTier" in body


def test_nested_copy_is_kept_for_existing_clients(client):
    body = client.post("/api/v1/stamps/").json()
    assert body["detail"]["accepts"] == body["accepts"]


def test_invalid_payment_header_402_is_also_top_level(client):
    r = client.post("/api/v1/stamps/", headers={"X-PAYMENT": "not-base64!"})
    assert r.status_code == 402
    assert x402PaymentRequiredResponse.model_validate(r.json()).error.startswith("Invalid X-PAYMENT")


def test_other_402s_are_left_alone(client):
    r = client.post("/api/v1/chunks/")
    assert r.status_code == 402
    assert r.json() == {"detail": {"code": "CREDIT_REQUIRED", "message": "top up"}}
