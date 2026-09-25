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



def _full_app(verify_ok=True, settle_ok=True, free_tier=True):
    """Stamps route with the payment dependency, the pool-style optional route,
    x402 middleware and CORS outermost, as in app/main.py."""
    from fastapi.middleware.cors import CORSMiddleware
    from x402.types import SettleResponse, VerifyResponse
    from unittest.mock import MagicMock
    from app.x402.dependency import settle_payment_if_offered
    from app.x402.settlement import settle_payment

    async def settled(request: "Request"):
        await settle_payment(request)
        return {"ok": True}

    fac = MagicMock()
    fac.verify = AsyncMock(return_value=VerifyResponse(isValid=verify_ok, invalidReason=None if verify_ok else "bad",
                                                       payer="0xp"))
    fac.settle = AsyncMock(return_value=SettleResponse(success=settle_ok, errorReason=None if settle_ok else "no funds"))
    app = FastAPI()
    paid = APIRouter(dependencies=[Depends(require_x402_payment)])
    paid.add_api_route("/api/v1/stamps/", settled, methods=["POST"])
    optional = APIRouter(dependencies=[Depends(settle_payment_if_offered)])
    optional.add_api_route("/api/v1/pool/acquire", settled, methods=["POST"])
    app.include_router(paid)
    app.include_router(optional)
    app.add_middleware(X402Middleware, facilitator_client=fac)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    return app, fac


from fastapi import Request  # noqa: E402
from tests.test_x402_integration import create_valid_payment_header  # noqa: E402


@pytest.mark.parametrize("case", ["no_header", "free_tier_disabled", "invalid_header", "verify_failed",
                                  "replay", "pool_verify_failed", "settle_refused"])
def test_every_payment_required_is_spec_shaped_and_keeps_cors(case):
    reset_rate_limiter()
    app, fac = _full_app(verify_ok=case not in ("verify_failed", "pool_verify_failed"),
                         settle_ok=case != "settle_refused")
    headers = {"Origin": "https://example.org"}
    path = "/api/v1/pool/acquire" if case.startswith("pool") else "/api/v1/stamps/"
    if case == "free_tier_disabled":
        headers["X-Payment-Mode"] = "free"
    elif case == "invalid_header":
        headers["X-PAYMENT"] = "not-base64!"
    elif case != "no_header":
        headers["X-PAYMENT"] = create_valid_payment_header()
    with patch("app.x402.dependency.check_base_eth_balance", new=AsyncMock(return_value=OK_BALANCE)), \
         patch("app.x402.dependency._calculate_price_for_request",
               new=AsyncMock(return_value={"price_usd": 0.02, "description": "t"})), \
         patch("app.x402.dependency._get_facilitator_client", return_value=fac), \
         patch("app.x402.middleware.settings") as mw, patch("app.x402.dependency.settings") as dep:
        _configure(dep, mw, free_tier=case != "free_tier_disabled")
        client = TestClient(app)
        if case == "replay":
            assert client.post(path, headers=headers).status_code == 200
        r = client.post(path, headers=headers)
    reset_rate_limiter()
    assert r.status_code == 402, (case, r.text)
    x402PaymentRequiredResponse.model_validate(r.json())
    assert r.headers.get("access-control-allow-origin") == "*"
    assert int(r.headers["content-length"]) == len(r.content)
