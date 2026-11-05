"""Advertising x402 v2: the PAYMENT-REQUIRED header on every 402 with an offer (#608).

The 402s are the ones the v1 goldens cover (tests/test_x402_v1_golden.py
`capture`, run unchanged): every dependency refusal with an offer, the
settlement refusal before delivery, the already-used authorization and the
Idempotency-Key original authorization, plus the v2 refusals
(tests/test_x402_v2_accept.py checks those through `_v1_body_402`) and the pool
allowance offer (built here through the middleware).

- "1,2": every 402 whose body has offers keeps the golden v1 body byte for
  byte and carries PAYMENT-REQUIRED. Decoded with codec_v2 and as plain
  base64 JSON (what @x402/core's decodePaymentRequiredHeader does), it is
  the v2 PaymentRequired of the same offers. A 402 without an offer gets none.
- "1": no PAYMENT-REQUIRED anywhere.
- Too large or not expressible in v2: the 402 goes out without the header,
  counted, never a 500.
"""
import asyncio
import json
import logging
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import settings
from app.services.metrics import x402_payment_required_header_failures_total as failures
from app.x402 import advertise_v2, codec_v2
from app.x402.middleware import X402Middleware, create_402_response, create_payment_requirements
from tests import test_x402_v1_golden as golden
from tests.test_x402_v1_golden import gateway  # noqa: F401  (fixture)
from tests.x402_v2_builders import advertised, expected_payment_required

# The golden capture's 402s, by name, and whether their body carries an offer.
WITH_OFFER = ("no_header_stamps", "no_header_extend", "no_header_data", "no_header_manifest",
              "no_header_credit", "free_tier_disabled", "invalid_header",
              "verify_invalid", "payer_not_address", "unsupported_payload", "payer_mismatch",
              "already_used", "settle_refused_before_delivery", "idempotency_original_auth",
              "no_header_free_tier_on")
# PAYMENT-SIGNATURE before v2 is enabled: with "1" a 402 X402_VERSION_UNSUPPORTED
# with the v1 offer; with v2 enabled the same header is decoded instead (an
# invalid v2 payment, 400), so these are checked with "1" only.
V1_ONLY = ("version_unsupported", "version_unsupported_free_tier_on")
# The middleware's own settlement refusal after the work ran: its body has no
# offer (PAYMENT_SETTLEMENT_FAILED with x402_status only), so nothing to advertise.
WITHOUT_OFFER = ("settle_refused_after_response",)


@pytest.fixture
def recorded(monkeypatch):
    """Run the golden capture unchanged and keep every full response, by name."""

    def run(gateway, network, versions):
        monkeypatch.setattr(settings, "X402_PROTOCOL_VERSIONS", versions)
        seen = []
        real = golden._resp

        def keep(r):
            seen.append(r)
            return real(r)

        monkeypatch.setattr(golden, "_resp", keep)
        got = golden.capture(gateway, network)
        monkeypatch.setattr(golden, "_resp", real)
        # capture() stores each _resp() result under its name right away, so the
        # response entries are in the order of the calls.
        names = [k for k, v in got.items() if isinstance(v, dict) and "status" in v and "body" in v]
        assert len(names) == len(seen)
        return {name: (got[name], r) for name, r in zip(names, seen)}

    return run


@pytest.mark.parametrize("network", golden.NETWORKS)
def test_dual_mode_every_402_with_an_offer_carries_the_header(gateway, recorded, network):
    responses = recorded(gateway, network, "1,2")
    assert set(WITH_OFFER) | set(WITHOUT_OFFER) <= set(responses)
    expected_golden = json.loads(golden._golden(network).read_text())
    for name in V1_ONLY:
        assert responses[name][1].status_code == 400, name
    for name in WITH_OFFER:
        summary, r = responses[name]
        assert r.status_code == 402, name
        # D2: the v1 body is the golden body, byte for byte.
        assert summary == expected_golden[name], f"{name}: the v1 body changed"
        body = r.json()
        assert advertised(r) == expected_payment_required(body), name
        assert advertised(r)["accepts"][0]["network"] == {"base": "eip155:8453",
                                                          "base-sepolia": "eip155:84532"}[network]
    for name in WITHOUT_OFFER:
        summary, r = responses[name]
        assert r.status_code == 402 and "accepts" not in r.json(), name
        assert "PAYMENT-REQUIRED" not in r.headers, name


@pytest.mark.parametrize("network", golden.NETWORKS)
def test_default_setting_sends_no_header_anywhere(gateway, recorded, network):
    responses = recorded(gateway, network, "1")
    expected_golden = json.loads(golden._golden(network).read_text())
    assert set(WITH_OFFER) | set(V1_ONLY) <= set(responses)
    for name, (summary, r) in responses.items():
        assert "PAYMENT-REQUIRED" not in r.headers, name
        assert summary == expected_golden[name], name


def test_the_header_carries_the_resource_and_error(gateway, recorded):
    responses = recorded(gateway, "base-sepolia", "1,2")
    _, r = responses["verify_invalid"]
    got = advertised(r)
    assert got["error"] == r.json()["error"]
    assert got["resource"] == {"url": "http://testserver/api/v1/stamps/",
                               "description": "Golden operation", "mimeType": "application/json"}
    assert got["accepts"] == [{
        "scheme": "exact", "network": "eip155:84532",
        "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e", "amount": "20000",
        "payTo": golden.PAY_TO, "maxTimeoutSeconds": 300,
        "extra": {"name": "USDC", "version": "2"},
    }]
    # Key order as @x402/core writes it: x402Version, error, resource, accepts.
    assert list(got) == ["x402Version", "error", "resource", "accepts"]


# --- the pool allowance offer, through the middleware -----------------------

def _pool_app():
    from app.api.endpoints.pool import _refuse_allowance

    async def acquire(request: Request):
        await _refuse_allowance(request, "Allowance spent. ", {"code": "POOL_ALLOWANCE_EXHAUSTED"},
                                size="small", scope="origin")

    app = FastAPI()
    app.add_api_route("/api/v1/pool/acquire", acquire, methods=["POST"])
    app.add_middleware(X402Middleware)
    from app.main import http_exception_handler
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("versions", ["1", "1,2"])
def test_pool_allowance_offer(monkeypatch, versions):
    golden.configure(monkeypatch, "base-sepolia")
    monkeypatch.setattr(settings, "X402_ALLOW_TESTNET_PAID_BYPASS", True)
    monkeypatch.setattr(settings, "X402_PROTOCOL_VERSIONS", versions)
    with patch("app.x402.dependency._calculate_price_for_request",
               new=AsyncMock(return_value={"price_usd": 0.02, "description": "Pooled stamp"})), \
         patch("app.x402.accept_gate.payments_refusal", return_value=None):
        r = _pool_app().post("/api/v1/pool/acquire")
    assert r.status_code == 402, r.text
    body = r.json()
    assert body["code"] == "POOL_ALLOWANCE_EXHAUSTED" and body["x402Version"] == 1
    if versions == "1":
        assert "PAYMENT-REQUIRED" not in r.headers
    else:
        got = advertised(r)
        assert got == expected_payment_required(body)
        assert got["resource"]["description"] == "Pooled stamp"


# --- 402s that offer no x402 payment ----------------------------------------

@pytest.mark.parametrize("detail", [
    {"code": "CREDIT_REQUIRED", "message": "top up", "payment_info": {"price_usd": 0.01}},
    {"code": "FREE_TIER_DISABLED", "message": "pay"},
    {"x402Version": 1, "error": "already used", "accepts": []},     # idempotency, no requirements
    "Payment Required",
])
def test_a_402_without_an_offer_gets_no_header(monkeypatch, detail):
    golden.configure(monkeypatch, "base-sepolia")
    monkeypatch.setattr(settings, "X402_PROTOCOL_VERSIONS", "1,2")

    async def refuse(request: Request):
        raise HTTPException(status_code=402, detail=detail)

    app = FastAPI()
    app.add_api_route("/api/v1/chunks/", refuse, methods=["POST"])
    app.add_middleware(X402Middleware)
    from app.main import http_exception_handler
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    before = sum(failures.labels(reason=x)._value.get() for x in ("too_large", "invalid"))
    r = TestClient(app).post("/api/v1/chunks/")
    assert r.status_code == 402
    assert "PAYMENT-REQUIRED" not in r.headers
    assert sum(failures.labels(reason=x)._value.get() for x in ("too_large", "invalid")) == before


# --- create_402_response ----------------------------------------------------

def _requirements(monkeypatch, description="Gateway operation"):
    golden.configure(monkeypatch, "base-sepolia")
    from starlette.requests import Request as StarletteRequest
    scope = {"type": "http", "method": "POST", "path": "/api/v1/stamps/", "raw_path": b"/api/v1/stamps/",
             "root_path": "", "scheme": "https", "query_string": b"",
             "headers": [(b"host", b"gateway.example")], "server": ("gateway.example", 443)}
    return create_payment_requirements(StarletteRequest(scope), 0.02, description)


@pytest.mark.parametrize("versions", ["1", "1,2", "2"])
def test_create_402_response(monkeypatch, versions):
    req = _requirements(monkeypatch)
    monkeypatch.setattr(settings, "X402_PROTOCOL_VERSIONS", versions)
    r = create_402_response(req, "Payment required")
    body = json.loads(r.body)
    assert body == {"x402Version": 1, "error": "Payment required", "accepts": [req.model_dump(by_alias=True)]}
    value = r.headers.get("PAYMENT-REQUIRED")
    if versions == "1":
        assert value is None
    else:
        got = codec_v2.decode_payment_required(value).model_dump(mode="json", by_alias=True, exclude_none=True)
        assert got == expected_payment_required(body)


# --- the fallback: too large, or not expressible in v2 ----------------------

def test_header_too_large_falls_back_without_it(gateway, monkeypatch, caplog):
    client, _fac = gateway("base-sepolia")
    monkeypatch.setattr(settings, "X402_PROTOCOL_VERSIONS", "1,2")
    huge = "Postage stamp " + "x" * 9000
    before = failures.labels(reason="too_large")._value.get()
    with patch("app.x402.dependency._calculate_price_for_request",
               new=AsyncMock(return_value={"price_usd": 0.02, "description": huge})), \
         caplog.at_level(logging.WARNING, logger="app.x402.advertise_v2"):
        r = client.post("/api/v1/stamps/")
    assert r.status_code == 402, r.text
    body = r.json()
    assert body["x402Version"] == 1 and body["accepts"][0]["description"] == huge
    assert "PAYMENT-REQUIRED" not in r.headers
    assert failures.labels(reason="too_large")._value.get() == before + 1
    assert any("PAYMENT-REQUIRED not sent" in m and "8192" in m for m in caplog.messages)
    assert all(huge not in m for m in caplog.messages)


def test_an_offer_v2_cannot_express_falls_back_without_it(gateway, monkeypatch, caplog):
    client, _fac = gateway("base-sepolia")
    monkeypatch.setattr(settings, "X402_PROTOCOL_VERSIONS", "1,2")
    before = failures.labels(reason="invalid")._value.get()

    def broken(_requirements):
        raise ValueError("no CAIP-2 id")

    monkeypatch.setattr("app.x402.v2_payment.v2_offer_from_v1", broken)
    with caplog.at_level(logging.WARNING, logger="app.x402.advertise_v2"):
        r = client.post("/api/v1/stamps/")
    assert r.status_code == 402 and r.json()["x402Version"] == 1
    assert "PAYMENT-REQUIRED" not in r.headers
    assert failures.labels(reason="invalid")._value.get() == before + 1
    assert any("cannot express" in m for m in caplog.messages)


def test_an_encoder_error_is_never_a_500(gateway, monkeypatch):
    client, _fac = gateway("base-sepolia")
    monkeypatch.setattr(settings, "X402_PROTOCOL_VERSIONS", "1,2")

    def boom(_message):
        raise codec_v2.PaymentHeaderInvalid(codec_v2.PAYMENT_REQUIRED_HEADER, "test")

    monkeypatch.setattr(codec_v2, "encode_payment_required", boom)
    r = client.post("/api/v1/stamps/")
    assert r.status_code == 402 and "PAYMENT-REQUIRED" not in r.headers


def test_the_builder_does_nothing_with_v2_off(monkeypatch):
    monkeypatch.setattr(settings, "X402_PROTOCOL_VERSIONS", "1")
    called = []
    monkeypatch.setattr("app.x402.v2_payment.v2_offer_from_v1", lambda r: called.append(r))
    assert advertise_v2.payment_required_header({"x402Version": 1, "error": "x", "accepts": [{}]}) is None
    assert called == []


# --- metrics ----------------------------------------------------------------

def _fresh_exposition(versions):
    import os
    import subprocess
    import sys
    code = ("import app.services.metrics\n"
            "from prometheus_client import REGISTRY, generate_latest\n"
            "print(generate_latest(REGISTRY).decode())\n")
    env = {**os.environ, "X402_PROTOCOL_VERSIONS": versions}
    out = subprocess.run([sys.executable, "-c", code], cwd=golden.Path(__file__).parent.parent,
                         env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_failure_series_exist_from_startup_with_v2_only():
    text = _fresh_exposition("1,2")
    for reason in ("too_large", "invalid"):
        assert f'gateway_x402_payment_required_header_failures_total{{reason="{reason}"}} 0.0' in text
    assert 'gateway_x402_payment_required_header_failures_total{' not in _fresh_exposition("1")
