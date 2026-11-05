"""A v2 payment header is never taken for "no payment" (#600).

x402 v2 clients pay with a PAYMENT-SIGNATURE header; this gateway reads only
v1's X-PAYMENT. Before #600 a PAYMENT-SIGNATURE request was treated as unpaid
and, where the free tier is on, was served free: the client believed it had
paid, and nothing was settled.

Until v2 is enabled, on every paid route:
- PAYMENT-SIGNATURE alone: 402 X402_VERSION_UNSUPPORTED, a v1 Payment Required
  body (with accepts) that a v1 client can still pay;
- X-PAYMENT and PAYMENT-SIGNATURE together: 400 PAYMENT_HEADER_CONFLICT;
- neither reaches the free tier, the pool allowance, the facilitator or the
  handler. With x402 disabled the header is ignored, as X-PAYMENT is.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel
from x402.types import SettleResponse, VerifyResponse, x402PaymentRequiredResponse

from app.services.metrics import x402_payments_total
from app.x402.dependency import require_x402_payment, settle_payment_if_offered
from app.x402.middleware import X402Middleware
from app.x402.ratelimit import reset_rate_limiter
from app.x402.settlement import settle_payment
from tests.test_x402_integration import _configure, create_valid_payment_header

PAYER = "0xa1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1"
V2 = "eyJ4NDAyVmVyc2lvbiI6Mn0="  # base64 of {"x402Version":2}; never decoded here
FREE = {"X-Payment-Mode": "free"}

# Every route that takes a per-request payment, as mounted in app.main.
PAID = [
    ("POST", "/api/v1/stamps/"),                         # stamp purchase
    ("PATCH", "/api/v1/stamps/" + "a" * 64 + "/extend"),  # stamp extend
    ("POST", "/api/v1/stamps/for-owner"),                 # buy-batch-for-owner
    ("POST", "/api/v1/data/"),                            # upload
    ("POST", "/api/v1/data/manifest"),                    # manifest upload
    ("POST", "/api/v1/chunks/credit"),                    # bandwidth credit top-up
]
POOL = ("POST", "/api/v1/pool/acquire")                   # optional payment
ALL = PAID + [POOL]

RAN = []


async def paid_handler(request: Request):
    mode = getattr(request.state, "x402_mode", None)
    if mode == "paid":
        await settle_payment(request)
    RAN.append(mode)
    return {"mode": mode}


class Purchase(BaseModel):
    depth: int


async def validated_handler(request: Request, body: Purchase):
    RAN.append(getattr(request.state, "x402_mode", None))
    return {"depth": body.depth}


async def chunk_upload(request: Request):
    RAN.append(getattr(request.state, "x402_mode", None))
    return {"ran": True}


def _facilitator():
    fac = MagicMock()
    fac.verify = AsyncMock(return_value=VerifyResponse(isValid=True, payer=PAYER))
    fac.settle = AsyncMock(return_value=SettleResponse(success=True, transaction="0x" + "ab" * 32,
                                                       network="base-sepolia", payer=PAYER))
    return fac


def _app(fac):
    """The paid routers as app.main mounts them: the mandatory dependency on
    stamps, data and chunks, the optional one on the pool; the chunk upload
    itself has no per-request payment."""
    app = FastAPI()
    paid = APIRouter(dependencies=[Depends(require_x402_payment)])
    paid.add_api_route("/api/v1/stamps/", paid_handler, methods=["POST"])
    paid.add_api_route("/api/v1/stamps/{stamp_id}/extend", paid_handler, methods=["PATCH"])
    paid.add_api_route("/api/v1/stamps/for-owner", paid_handler, methods=["POST"])
    paid.add_api_route("/api/v1/data/", paid_handler, methods=["POST"])
    paid.add_api_route("/api/v1/data/manifest", paid_handler, methods=["POST"])
    paid.add_api_route("/api/v1/chunks/credit", paid_handler, methods=["POST"])
    paid.add_api_route("/api/v1/chunks/", chunk_upload, methods=["POST"])
    # A paid route with a validated body, for the order of checks (#427 before #600).
    paid.add_api_route("/api/v1/stamps/validated", validated_handler, methods=["POST"])
    optional = APIRouter(dependencies=[Depends(settle_payment_if_offered)])
    optional.add_api_route("/api/v1/pool/acquire", paid_handler, methods=["POST"])
    app.include_router(paid)
    app.include_router(optional)
    app.add_middleware(X402Middleware, facilitator_client=fac)
    # The real error envelope (top-level code and message).
    from app.main import http_exception_handler
    from starlette.exceptions import HTTPException as StarletteHTTPException
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    return app


@pytest.fixture
def gw():
    RAN.clear()
    reset_rate_limiter()
    fac = _facilitator()
    with patch("app.x402.dependency._calculate_price_for_request",
               new=AsyncMock(return_value={"price_usd": 0.02, "description": "t"})), \
         patch("app.x402.dependency._get_facilitator_client", return_value=fac), \
         patch("app.x402.middleware.settings") as mw, patch("app.x402.dependency.settings") as dep:
        _configure(dep, mw, free_tier=True)
        yield TestClient(_app(fac), raise_server_exceptions=False), fac, dep, mw
    reset_rate_limiter()
    RAN.clear()


def _send(client, route, headers):
    method, path = route
    return client.request(method, path, headers=headers)


def _count(mode):
    return x402_payments_total.labels(mode=mode)._value.get()


def _assert_version_unsupported(r):
    assert r.status_code == 402, r.text
    body = r.json()
    assert body["code"] == "X402_VERSION_UNSUPPORTED"
    assert body["detail"]["code"] == "X402_VERSION_UNSUPPORTED"
    assert "PAYMENT-SIGNATURE" in body["message"]
    # A v1 Payment Required body at the top level, so a v1 client can pay it.
    parsed = x402PaymentRequiredResponse.model_validate(body)
    assert parsed.x402_version == 1
    assert parsed.accepts[0].max_amount_required == "20000"
    assert body["detail"]["accepts"] == body["accepts"]


def _assert_conflict(r):
    assert r.status_code == 400, r.text
    body = r.json()
    assert body["code"] == "PAYMENT_HEADER_CONFLICT"
    assert body["detail"]["code"] == "PAYMENT_HEADER_CONFLICT"
    assert "accepts" not in body


# --------------------------------------------------------------------------
# The bug: a PAYMENT-SIGNATURE request was served as unpaid
# --------------------------------------------------------------------------

@pytest.mark.parametrize("route", ALL, ids=lambda r: f"{r[0]} {r[1]}")
@pytest.mark.parametrize("free", [False, True], ids=["no-mode", "free-mode"])
def test_v2_header_alone_gets_402_version_unsupported(gw, route, free):
    client, fac, _, _ = gw
    before_free, before = _count("free"), _count("version_unsupported")
    r = _send(client, route, {"PAYMENT-SIGNATURE": V2, **(FREE if free else {})})
    _assert_version_unsupported(r)
    assert RAN == []                      # no handler, no free tier, no pool allowance
    fac.verify.assert_not_called()
    fac.settle.assert_not_called()
    assert _count("free") == before_free
    assert _count("version_unsupported") == before + 1


@pytest.mark.parametrize("route", ALL, ids=lambda r: f"{r[0]} {r[1]}")
@pytest.mark.parametrize("free", [False, True], ids=["no-mode", "free-mode"])
def test_both_headers_get_400_conflict(gw, route, free):
    client, fac, _, _ = gw
    before = _count("header_conflict")
    r = _send(client, route, {"X-PAYMENT": create_valid_payment_header(), "PAYMENT-SIGNATURE": V2,
                              **(FREE if free else {})})
    _assert_conflict(r)
    assert RAN == []
    fac.verify.assert_not_called()
    fac.settle.assert_not_called()
    assert _count("header_conflict") == before + 1


def test_v2_header_does_not_use_up_the_free_tier(gw):
    client, _, _, _ = gw
    for _ in range(5):  # the free-tier limit is 3
        _assert_version_unsupported(client.post("/api/v1/stamps/", headers={"PAYMENT-SIGNATURE": V2, **FREE}))
    r = client.post("/api/v1/stamps/", headers=FREE)
    assert r.status_code == 200 and r.json() == {"mode": "free-tier"}


def test_free_tier_disabled_still_names_the_version(gw):
    client, _, dep, _ = gw
    dep.X402_FREE_TIER_ENABLED = False
    r = client.post("/api/v1/stamps/", headers={"PAYMENT-SIGNATURE": V2, **FREE})
    _assert_version_unsupported(r)
    assert "freeTier" not in r.json()


def test_version_402_offers_the_free_tier_like_the_plain_402(gw):
    client, _, _, _ = gw
    plain = client.post("/api/v1/stamps/").json()
    v2 = client.post("/api/v1/stamps/", headers={"PAYMENT-SIGNATURE": V2}).json()
    assert "freeTier" in plain and "freeTier" in v2
    assert v2["accepts"] == plain["accepts"]


# --------------------------------------------------------------------------
# Order of checks, as docs/error-codes.md states it: request validation (422)
# first, then the header conflict (400), then the payments gate (503, #589,
# skipped with free mode), then pricing, then the version 402.
# --------------------------------------------------------------------------

BOTH = {"X-PAYMENT": create_valid_payment_header(), "PAYMENT-SIGNATURE": V2}


def _payments_unavailable():
    return patch("app.x402.accept_gate.payments_refusal", return_value="balances_unreadable")


@pytest.mark.parametrize("headers", [BOTH, {"PAYMENT-SIGNATURE": V2}], ids=["both", "v2"])
def test_invalid_body_gets_422_before_the_header_checks(gw, headers):
    client, fac, _, _ = gw
    before = (_count("header_conflict"), _count("version_unsupported"))
    r = client.post("/api/v1/stamps/validated", json={"depth": "not-a-number"}, headers=headers)
    assert r.status_code == 422, r.text
    assert isinstance(r.json()["detail"], list)
    assert RAN == []
    fac.verify.assert_not_called()
    assert (_count("header_conflict"), _count("version_unsupported")) == before


def test_valid_body_then_gets_the_header_answer(gw):
    # The 422 above is the body, not the route: with a valid body the same
    # requests get the conflict and the version answers.
    client, _, _, _ = gw
    _assert_conflict(client.post("/api/v1/stamps/validated", json={"depth": 17}, headers=BOTH))
    _assert_version_unsupported(client.post("/api/v1/stamps/validated", json={"depth": 17},
                                            headers={"PAYMENT-SIGNATURE": V2}))
    assert RAN == []


def test_payments_unavailable_comes_before_the_version_402(gw):
    client, fac, _, _ = gw
    before = _count("version_unsupported")
    with _payments_unavailable():
        r = client.post("/api/v1/stamps/", headers={"PAYMENT-SIGNATURE": V2})
    assert r.status_code == 503, r.text
    assert r.json()["code"] == "PAYMENTS_UNAVAILABLE"
    assert _count("version_unsupported") == before
    assert RAN == []
    fac.verify.assert_not_called()


def test_free_mode_skips_the_payments_gate_and_gets_the_version_402(gw):
    client, fac, _, _ = gw
    with _payments_unavailable():
        r = client.post("/api/v1/stamps/", headers={"PAYMENT-SIGNATURE": V2, **FREE})
    _assert_version_unsupported(r)
    assert RAN == []
    fac.verify.assert_not_called()


@pytest.mark.parametrize("free", [False, True], ids=["no-mode", "free-mode"])
def test_header_conflict_comes_before_the_payments_gate(gw, free):
    client, fac, _, _ = gw
    with _payments_unavailable():
        r = client.post("/api/v1/stamps/", headers={**BOTH, **(FREE if free else {})})
    _assert_conflict(r)
    assert RAN == []
    fac.verify.assert_not_called()


# --------------------------------------------------------------------------
# Header names are case-insensitive; an empty value fails closed
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["payment-signature", "Payment-Signature", "PAYMENT-signature"])
def test_v2_header_name_any_case(gw, name):
    client, _, _, _ = gw
    _assert_version_unsupported(client.post("/api/v1/stamps/", headers={name: V2, **FREE}))
    _assert_version_unsupported(client.post("/api/v1/pool/acquire", headers={name: V2}))
    assert RAN == []


@pytest.mark.parametrize("v1, v2", [("x-payment", "payment-signature"), ("X-Payment", "Payment-Signature")])
def test_both_headers_any_case(gw, v1, v2):
    client, _, _, _ = gw
    _assert_conflict(client.post("/api/v1/stamps/", headers={v1: create_valid_payment_header(), v2: V2}))


def test_empty_v2_header_fails_closed(gw):
    client, _, _, _ = gw
    _assert_version_unsupported(client.post("/api/v1/stamps/", headers={"PAYMENT-SIGNATURE": "", **FREE}))
    _assert_version_unsupported(client.post("/api/v1/pool/acquire", headers={"PAYMENT-SIGNATURE": ""}))
    assert RAN == []


# --------------------------------------------------------------------------
# v1 behaviour is unchanged
# --------------------------------------------------------------------------

@pytest.mark.parametrize("route", ALL, ids=lambda r: f"{r[0]} {r[1]}")
def test_v1_payment_still_pays(gw, route):
    client, fac, _, _ = gw
    r = _send(client, route, {"X-PAYMENT": create_valid_payment_header()})
    assert r.status_code == 200, r.text
    assert r.json() == {"mode": "paid"}
    assert fac.verify.await_count == 1 and fac.settle.await_count == 1


@pytest.mark.parametrize("route", PAID, ids=lambda r: f"{r[0]} {r[1]}")
def test_no_header_still_gets_the_plain_402(gw, route):
    client, _, _, _ = gw
    r = _send(client, route, {})
    assert r.status_code == 402
    assert r.json()["code"] == "HTTP_402"
    assert RAN == []


def test_no_header_on_the_pool_still_uses_the_allowance(gw):
    client, _, _, _ = gw
    r = client.post("/api/v1/pool/acquire")
    assert r.status_code == 200 and r.json() == {"mode": None}


def test_empty_v1_header_on_the_pool_still_uses_the_allowance(gw):
    # Unchanged: an empty X-PAYMENT alone is no payment offered.
    client, fac, _, _ = gw
    r = client.post("/api/v1/pool/acquire", headers={"X-PAYMENT": ""})
    assert r.status_code == 200 and r.json() == {"mode": None}
    fac.verify.assert_not_called()


@pytest.mark.parametrize("route", PAID, ids=lambda r: f"{r[0]} {r[1]}")
def test_free_mode_alone_still_gets_the_free_tier(gw, route):
    client, _, _, _ = gw
    r = _send(client, route, FREE)
    assert r.status_code == 200 and r.json() == {"mode": "free-tier"}


# --------------------------------------------------------------------------
# Where X-PAYMENT is not read, PAYMENT-SIGNATURE is not read either
# --------------------------------------------------------------------------

def test_chunk_upload_ignores_the_v2_header_as_it_ignores_v1(gw):
    client, _, _, _ = gw
    for headers in ({"X-PAYMENT": create_valid_payment_header()}, {"PAYMENT-SIGNATURE": V2}):
        r = client.post("/api/v1/chunks/", headers=headers)
        assert r.status_code == 200 and r.json() == {"ran": True}


def test_get_requests_are_not_affected(gw):
    client, _, _, _ = gw
    app = client.app
    router = APIRouter(dependencies=[Depends(require_x402_payment)])

    async def read():
        return {"read": True}
    router.add_api_route("/api/v1/stamps/{stamp_id}", read, methods=["GET"])
    app.include_router(router)
    r = client.get("/api/v1/stamps/abc", headers={"PAYMENT-SIGNATURE": V2, "X-PAYMENT": "x"})
    assert r.status_code == 200


@pytest.mark.parametrize("route", ALL, ids=lambda r: f"{r[0]} {r[1]}")
def test_x402_disabled_ignores_the_v2_header_as_it_ignores_v1(gw, route):
    client, fac, dep, mw = gw
    dep.X402_ENABLED = False
    mw.X402_ENABLED = False
    for headers in ({"PAYMENT-SIGNATURE": V2}, {"PAYMENT-SIGNATURE": V2, "X-PAYMENT": "x"},
                    {"X-PAYMENT": "x"}):
        r = _send(client, route, headers)
        assert r.status_code == 200 and r.json() == {"mode": None}
    fac.verify.assert_not_called()


# --------------------------------------------------------------------------
# The detector
# --------------------------------------------------------------------------

def test_detector_on_request_headers_and_raw_scope_headers():
    from starlette.datastructures import Headers
    from app.x402.payment_headers import offered_payment_versions
    cases = [
        ({}, set()),
        ({"X-PAYMENT": "a"}, {1}),
        ({"x-payment": "a"}, {1}),
        ({"PAYMENT-SIGNATURE": "b"}, {2}),
        ({"payment-signature": ""}, {2}),
        ({"X-Payment": "a", "Payment-Signature": "b"}, {1, 2}),
        ({"X-Payment-Mode": "free", "X-PAYMENT-RESPONSE": "c"}, set()),
    ]
    for given, expected in cases:
        raw = [(k.lower().encode(), v.encode()) for k, v in given.items()]
        assert offered_payment_versions(Headers(headers=given)) == expected, given
        assert offered_payment_versions(raw) == expected, given
        # Raw ASGI header names are lowercase by spec, but do not rely on it.
        assert offered_payment_versions([(k.encode(), v.encode()) for k, v in given.items()]) == expected

