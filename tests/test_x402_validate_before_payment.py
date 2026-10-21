"""Protected routes validate the request before asking for payment (#427).

The payment dependency is attached to the routers, so FastAPI runs it before it
validates the route's own parameters. A malformed request therefore got 402
(unpaid), used up a free-tier slot (free tier), or was verified with the
facilitator (paid) before it was rejected with 422. These tests mount the real
routers the way app/main.py does and check that a malformed request gets 422
first, on every protected route.
"""
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from x402.types import VerifyResponse

from app.api.endpoints import chunks, data, pool, stamps, stamps_for_owner
from app.core.config import settings
from app.x402 import dependency
from app.x402.dependency import require_x402_payment, settle_payment_if_offered
from app.x402.middleware import X402Middleware
from app.x402.ratelimit import check_rate_limit, reset_rate_limiter
from tests.test_x402_integration import create_valid_payment_header

PAYER = "0xa1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1"
BATCH = "a" * 64
PRICE = {"price_usd": 0.05, "description": "test"}

# (method, url, kwargs) for one malformed request per protected route.
MALFORMED = [
    pytest.param("POST", "/api/v1/stamps/", {"json": {"depth": 99}}, id="stamps-purchase-body"),
    pytest.param("POST", "/api/v1/stamps/", {"json": {"duration_hours": "soon"}}, id="stamps-purchase-type"),
    pytest.param("PATCH", "/api/v1/stamps/not-a-batch-id/extend", {"json": {}}, id="stamps-extend-path"),
    pytest.param("POST", "/api/v1/stamps/for-owner", {"json": {"owner": "nobody"}}, id="for-owner-body"),
    pytest.param("POST", "/api/v1/data/?stamp_id=bad", {"files": {"file": ("a.txt", b"hi")}}, id="data-query"),
    pytest.param("POST", f"/api/v1/data/?stamp_id={BATCH}", {"data": {"x": "1"}}, id="data-missing-file"),
    pytest.param("POST", f"/api/v1/data/manifest?stamp_id={BATCH}&redundancy=9",
                 {"files": {"file": ("a.tar", b"x")}}, id="manifest-query"),
    pytest.param("POST", "/api/v1/chunks/credit", {}, id="credit-missing-query"),
]


def _app(facilitator=None):
    app = FastAPI()
    deps = [Depends(require_x402_payment)]
    app.include_router(stamps.router, prefix="/api/v1/stamps", dependencies=deps)
    app.include_router(data.router, prefix="/api/v1/data", dependencies=deps)
    app.include_router(chunks.router, prefix="/api/v1/chunks", dependencies=deps)
    app.include_router(stamps_for_owner.router, prefix="/api/v1/stamps", dependencies=deps)
    app.include_router(pool.router, prefix="/api/v1/pool", dependencies=[Depends(settle_payment_if_offered)])
    app.add_middleware(X402Middleware, facilitator_client=facilitator or MagicMock())
    return app


def _facilitator():
    f = MagicMock()
    f.verify = AsyncMock(return_value=VerifyResponse(isValid=True, payer=PAYER))
    f.settle = AsyncMock()
    return f


@pytest.fixture
def x402_on(monkeypatch):
    monkeypatch.setattr(settings, "X402_ENABLED", True)
    monkeypatch.setattr(settings, "X402_FREE_TIER_ENABLED", True)
    monkeypatch.setattr(settings, "X402_PAY_TO_ADDRESS", "0x" + "11" * 20)
    reset_rate_limiter()
    with patch.object(dependency, "_calculate_price_for_request", new=AsyncMock(return_value=PRICE)) as price:
        yield price
    reset_rate_limiter()


@pytest.mark.parametrize("method,url,kwargs", MALFORMED)
def test_malformed_unpaid_request_gets_422_not_402(x402_on, method, url, kwargs):
    r = TestClient(_app()).request(method, url, **kwargs)
    assert r.status_code == 422, r.text
    assert r.json()["detail"], "the 422 lists the invalid fields"
    x402_on.assert_not_awaited()  # never priced


@pytest.mark.parametrize("method,url,kwargs", MALFORMED)
def test_malformed_free_tier_request_does_not_use_a_slot(x402_on, method, url, kwargs):
    spy = MagicMock(side_effect=check_rate_limit)
    with patch.object(dependency, "check_rate_limit", spy):
        r = TestClient(_app()).request(method, url, headers={"X-Payment-Mode": "free"}, **kwargs)
    assert r.status_code == 422, r.text
    spy.assert_not_called()


def test_well_formed_free_tier_request_still_uses_a_slot(x402_on, monkeypatch):
    # The route refuses the request after the dependency (chunk upload is off),
    # which is enough to show the dependency ran and counted it.
    spy = MagicMock(side_effect=check_rate_limit)
    with patch.object(dependency, "check_rate_limit", spy):
        TestClient(_app()).post("/api/v1/chunks/credit?mb=100", headers={"X-Payment-Mode": "free"})
    spy.assert_called_once()


def test_well_formed_unpaid_request_still_gets_402(x402_on):
    r = TestClient(_app()).post("/api/v1/stamps/", json={"depth": 17, "duration_hours": 24})
    assert r.status_code == 402


@pytest.mark.parametrize("method,url,kwargs", MALFORMED)
def test_malformed_paid_request_is_not_verified_or_settled(x402_on, method, url, kwargs):
    from app.x402.settlement import authorization_key, replay_guard
    from app.x402.middleware import decode_payment_header
    header = create_valid_payment_header(payer=PAYER)
    fac = _facilitator()
    with patch.object(dependency, "_get_facilitator_client", return_value=fac):
        r = TestClient(_app(fac)).request(method, url, headers={"X-PAYMENT": header}, **kwargs)
    assert r.status_code == 422, r.text
    fac.verify.assert_not_awaited()
    fac.settle.assert_not_awaited()
    key = authorization_key(decode_payment_header(header))
    assert replay_guard.reserve(key), "the authorization was never reserved"
    replay_guard.release(key)


def test_malformed_paid_pool_acquire_is_not_verified(x402_on):
    fac = _facilitator()
    with patch.object(dependency, "_get_facilitator_client", return_value=fac):
        r = TestClient(_app(fac)).post(
            "/api/v1/pool/acquire", json={"depth": 18},
            headers={"X-PAYMENT": create_valid_payment_header(payer=PAYER)},
        )
    assert r.status_code == 422, r.text
    fac.verify.assert_not_awaited()


class TestFailOpen:
    """If FastAPI's internals change shape, the pre-check steps aside: the request
    is handled as before (FastAPI still validates it later), never a 500."""

    @pytest.fixture(autouse=True)
    def _reset_warning(self, monkeypatch):
        monkeypatch.setattr(dependency, "_PREVALIDATION_WARNED", False)

    def test_broken_internals_fall_back_without_a_500(self, x402_on, caplog):
        with patch.object(dependency, "_route_validation_errors", side_effect=TypeError("changed")):
            client = TestClient(_app())
            with caplog.at_level(logging.WARNING, logger=dependency.logger.name):
                r1 = client.post("/api/v1/stamps/", json={"depth": 99})
                r2 = client.post("/api/v1/stamps/", json={"depth": 99})
        assert r1.status_code == 402 and r2.status_code == 402  # the old order, not 500
        warnings = [rec for rec in caplog.records if "validate" in rec.getMessage().lower()
                    and rec.levelno == logging.WARNING]
        assert len(warnings) == 1, "warned once per process, not per request"

    def test_validation_errors_are_not_swallowed_by_the_fallback(self, x402_on):
        # A RequestValidationError is the intended outcome, not an internals failure.
        with patch.object(dependency, "_route_validation_errors",
                          new=AsyncMock(return_value=[{"type": "x", "loc": ("body",), "msg": "m", "input": None}])):
            r = TestClient(_app()).post("/api/v1/stamps/", json={"depth": 17})
        assert r.status_code == 422


def _app_without_x402():
    app = FastAPI()
    app.include_router(stamps.router, prefix="/api/v1/stamps")
    app.include_router(data.router, prefix="/api/v1/data")
    app.include_router(chunks.router, prefix="/api/v1/chunks")
    app.include_router(stamps_for_owner.router, prefix="/api/v1/stamps")
    return app


@pytest.mark.parametrize("method,url,kwargs", MALFORMED + [
    # A JSON body sent without a Content-Type: FastAPI 0.127 parses it, 0.141
    # (strict_content_type) does not. The early check must agree either way.
    pytest.param("POST", "/api/v1/stamps/", {"content": b'{"depth": 99}'}, id="no-content-type"),
])
def test_the_early_422_matches_the_one_fastapi_gives_without_payment(x402_on, method, url, kwargs):
    early = TestClient(_app()).request(method, url, **kwargs)
    plain = TestClient(_app_without_x402()).request(method, url, **kwargs)
    assert early.status_code == plain.status_code == 422
    assert early.json()["detail"] == plain.json()["detail"]


def test_the_early_check_does_not_fall_back_on_this_fastapi(x402_on, monkeypatch, caplog):
    """The installed FastAPI is handled explicitly, not through the fallback."""
    monkeypatch.setattr(dependency, "_PREVALIDATION_WARNED", False)
    with caplog.at_level(logging.WARNING, logger=dependency.logger.name):
        TestClient(_app()).post("/api/v1/stamps/", json={"depth": 17})
    assert dependency._PREVALIDATION_WARNED is False
