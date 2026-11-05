"""x402 v1 wire output is byte-identical across the v2 groundwork (#599, #596).

The golden files in tests/golden/x402_v1/ were generated from origin/dev before
the v1 <-> CAIP-2 network map (#599) existed. They hold, for `base` and
`base-sepolia`:

- every x402 v1 402 body the paid routes send, as the exact response bytes
  (key order included), and the X-PAYMENT-RESPONSE header of a paid request;
- the exact JSON the gateway sends to the facilitator's /verify and /settle;
- the pool allowance 402 offer.

Never regenerate them to make a change pass: a difference is a change to what
v1 clients and facilitators receive. They are written only when
X402_GOLDEN_REGENERATE=1 is set, which was done once, on unchanged code.
"""
import json
import os
import types
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException
from x402.encoding import safe_base64_encode
from x402.facilitator import FacilitatorClient

import x402.facilitator as sdk_facilitator
from app.core.config import settings
from app.x402.dependency import require_x402_payment
from app.x402.middleware import X402Middleware
from app.x402.ratelimit import reset_rate_limiter
from app.x402.settlement import replay_guard, settle_payment

GOLDEN_DIR = Path(__file__).parent / "golden" / "x402_v1"
REGENERATE = os.environ.get("X402_GOLDEN_REGENERATE") == "1"

# The v1 network names the goldens were captured for.
NETWORKS = ("base", "base-sepolia")

PAY_TO = "0x" + "22" * 20
PAYER = "0x" + "a1" * 20
OTHER_PAYER = "0x" + "b2" * 20
FACILITATOR_URL = "https://x402.org/facilitator"
TX = "0x" + "ab" * 32
EXTEND = "/api/v1/stamps/" + "c" * 64 + "/extend"


def payment_header(network, payer=PAYER, nonce_byte="01", value="20000"):
    payload = {
        "x402Version": 1,
        "scheme": "exact",
        "network": network,
        "payload": {
            "signature": "0x" + "ab" * 65,
            "authorization": {
                "from": payer,
                "to": PAY_TO,
                "value": value,
                "validAfter": "0",
                "validBefore": "9999999999",
                "nonce": "0x" + nonce_byte * 32,
            },
        },
    }
    return safe_base64_encode(json.dumps(payload).encode("utf-8"))


class Facilitator:
    """The facilitator's HTTP side: records each request body as sent."""

    def __init__(self):
        self.calls = []
        self.verify = {"isValid": True, "payer": PAYER}
        self.settle = {"success": True, "transaction": TX, "network": "base-sepolia", "payer": PAYER}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.rsplit("/", 1)[-1]
        self.calls.append({"path": path, "body": request.content.decode()})
        return httpx.Response(200, json=self.verify if path == "verify" else self.settle)


async def _settle_then_answer(request: Request):
    await settle_payment(request)
    return {"batchID": "d" * 64}


async def _answer(request: Request):
    # No settle_payment: the middleware settles after the response.
    return {"reference": "e" * 64}


async def _settle_then_fail(request: Request):
    await settle_payment(request)
    raise HTTPException(status_code=500, detail="boom")


def _app(fac_client):
    app = FastAPI()
    paid = APIRouter(dependencies=[Depends(require_x402_payment)])
    paid.add_api_route("/api/v1/stamps/", _settle_then_answer, methods=["POST"])
    paid.add_api_route("/api/v1/stamps/{stamp_id}/extend", _settle_then_answer, methods=["PATCH"])
    paid.add_api_route("/api/v1/data/", _answer, methods=["POST"])
    paid.add_api_route("/api/v1/data/manifest", _answer, methods=["POST"])
    paid.add_api_route("/api/v1/chunks/credit", _answer, methods=["POST"])
    app.include_router(paid)
    app.add_middleware(X402Middleware, facilitator_client=fac_client)
    from app.main import http_exception_handler
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    return app


def configure(monkeypatch, network, *, free_tier=False):
    monkeypatch.setattr(settings, "X402_ENABLED", True)
    monkeypatch.setattr(settings, "X402_NETWORK", network)
    monkeypatch.setattr(settings, "X402_PAY_TO_ADDRESS", PAY_TO)
    monkeypatch.setattr(settings, "X402_FACILITATOR_URL", FACILITATOR_URL)
    monkeypatch.setattr(settings, "X402_MIN_PRICE_USD", 0.01)
    monkeypatch.setattr(settings, "X402_FREE_TIER_ENABLED", free_tier)
    monkeypatch.setattr(settings, "X402_FREE_TIER_RATE_LIMIT", 3)
    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 0)


@pytest.fixture
def gateway(monkeypatch):
    """Build a gateway for one network: (client, facilitator, set_free_tier)."""
    stack = []

    def build(network, free_tier=False):
        configure(monkeypatch, network, free_tier=free_tier)
        reset_rate_limiter()
        fac = Facilitator()
        real_async_client = httpx.AsyncClient
        monkeypatch.setattr(sdk_facilitator, "httpx", types.SimpleNamespace(
            AsyncClient=lambda: real_async_client(transport=httpx.MockTransport(fac.handler))))
        client = FacilitatorClient(config={"url": FACILITATOR_URL})
        p1 = patch("app.x402.dependency._calculate_price_for_request",
                   new=AsyncMock(return_value={"price_usd": 0.02, "description": "Golden operation"}))
        p2 = patch("app.x402.dependency._get_facilitator_client", return_value=client)
        p1.start()
        p2.start()
        stack.extend([p1, p2])
        return TestClient(_app(client), raise_server_exceptions=False), fac

    yield build
    for p in reversed(stack):
        p.stop()
    reset_rate_limiter()


def _resp(r):
    return {"status": r.status_code, "body": r.text}


def capture(build, network):
    """Every v1 output this module covers, for one configured network.

    The network written into payment headers is always the v1 name the 402
    advertises, read from the first 402: that is what a v1 client signs.
    """
    out = {}

    # --- 402 without a payment, on every paid route -------------------------
    client, fac = build(network)
    for name, method, path in (
        ("no_header_stamps", "POST", "/api/v1/stamps/"),
        ("no_header_extend", "PATCH", EXTEND),
        ("no_header_data", "POST", "/api/v1/data/"),
        ("no_header_manifest", "POST", "/api/v1/data/manifest"),
        ("no_header_credit", "POST", "/api/v1/chunks/credit"),
    ):
        out[name] = _resp(client.request(method, path))
    v1_name = json.loads(out["no_header_stamps"]["body"])["accepts"][0]["network"]

    out["free_tier_disabled"] = _resp(client.post("/api/v1/stamps/", headers={"X-Payment-Mode": "free"}))
    out["invalid_header"] = _resp(client.post("/api/v1/stamps/", headers={"X-PAYMENT": "not base64 !!"}))
    out["version_unsupported"] = _resp(client.post("/api/v1/stamps/",
                                                   headers={"PAYMENT-SIGNATURE": "eyJ4NDAyVmVyc2lvbiI6Mn0="}))

    fac.verify = {"isValid": False, "invalidReason": "insufficient_funds", "payer": PAYER}
    out["verify_invalid"] = _resp(client.post("/api/v1/stamps/",
                                              headers={"X-PAYMENT": payment_header(v1_name, nonce_byte="02")}))
    out["verify_invalid_request"] = fac.calls[-1]

    fac.verify = {"isValid": True, "payer": "not-an-address"}
    out["payer_not_address"] = _resp(client.post("/api/v1/stamps/",
                                                 headers={"X-PAYMENT": payment_header(v1_name, nonce_byte="03")}))

    fac.verify = {"isValid": True, "payer": PAYER}
    out["unsupported_payload"] = _resp(client.post("/api/v1/stamps/", headers={
        "X-PAYMENT": payment_header(v1_name, payer="not-an-address", nonce_byte="04")}))

    fac.verify = {"isValid": True, "payer": OTHER_PAYER}
    out["payer_mismatch"] = _resp(client.post("/api/v1/stamps/", headers={
        "X-PAYMENT": payment_header(v1_name, nonce_byte="05"), "Idempotency-Key": "golden-mismatch"}))

    # --- a paid request settled before delivery: verify + settle JSON --------
    fac.verify = {"isValid": True, "payer": PAYER}
    fac.calls.clear()
    r = client.post("/api/v1/stamps/", headers={"X-PAYMENT": payment_header(v1_name, nonce_byte="06")})
    out["paid_settle_before_delivery"] = {**_resp(r), "x_payment_response": r.headers.get("X-PAYMENT-RESPONSE")}
    out["paid_settle_before_delivery_requests"] = list(fac.calls)

    # The same authorization again: already used.
    out["already_used"] = _resp(client.post("/api/v1/stamps/",
                                            headers={"X-PAYMENT": payment_header(v1_name, nonce_byte="06")}))

    # --- a paid request settled by the middleware after the response --------
    fac.calls.clear()
    r = client.post("/api/v1/data/", headers={"X-PAYMENT": payment_header(v1_name, nonce_byte="07")})
    out["paid_settle_after_response"] = {**_resp(r), "x_payment_response": r.headers.get("X-PAYMENT-RESPONSE")}
    out["paid_settle_after_response_requests"] = list(fac.calls)

    # --- settlement refused --------------------------------------------------
    fac.settle = {"success": False, "errorReason": "invalid_transaction_state",
                  "transaction": "", "network": "base-sepolia", "payer": PAYER}
    out["settle_refused_before_delivery"] = _resp(client.post(
        "/api/v1/stamps/", headers={"X-PAYMENT": payment_header(v1_name, nonce_byte="08")}))
    out["settle_refused_after_response"] = _resp(client.post(
        "/api/v1/data/", headers={"X-PAYMENT": payment_header(v1_name, nonce_byte="09")}))

    # --- the original authorization retried under its own Idempotency-Key ----
    fac.settle = {"success": True, "transaction": TX, "network": "base-sepolia", "payer": PAYER}
    headers = {"X-PAYMENT": payment_header(v1_name, nonce_byte="0a"), "Idempotency-Key": "golden-orig"}
    client.post("/api/v1/data/", headers=headers)
    replay_guard.reset()            # the reservation lapsed; the stored entry stays
    out["idempotency_original_auth"] = _resp(client.post("/api/v1/data/", headers=headers))

    # --- 402 with the free tier on (freeTier block) ---------------------------
    client, fac = build(network, free_tier=True)
    out["no_header_free_tier_on"] = _resp(client.post("/api/v1/stamps/"))
    out["version_unsupported_free_tier_on"] = _resp(client.post(
        "/api/v1/stamps/", headers={"PAYMENT-SIGNATURE": "eyJ4NDAyVmVyc2lvbiI6Mn0="}))
    return out


def capture_pool_offer(monkeypatch, network):
    """The pool allowance refusal's 402 offer (app/api/endpoints/pool.py)."""
    import asyncio
    from starlette.requests import Request as StarletteRequest
    from app.api.endpoints.pool import _refuse_allowance
    configure(monkeypatch, network)
    monkeypatch.setattr(settings, "X402_ALLOW_TESTNET_PAID_BYPASS", True)
    scope = {"type": "http", "method": "POST", "path": "/api/v1/pool/acquire",
             "raw_path": b"/api/v1/pool/acquire", "root_path": "", "scheme": "http",
             "query_string": b"", "headers": [(b"host", b"testserver")], "server": ("testserver", 80)}
    request = StarletteRequest(scope)
    with patch("app.x402.dependency._calculate_price_for_request",
               new=AsyncMock(return_value={"price_usd": 0.02, "description": "Pooled stamp"})), \
         patch("app.x402.accept_gate.payments_refusal", return_value=None):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(_refuse_allowance(request, "Allowance spent. ", {"code": "POOL_ALLOWANCE_EXHAUSTED"},
                                          size="small", scope="origin"))
    return {"status": exc.value.status_code, "body": json.dumps(exc.value.detail)}


def _golden(name):
    return GOLDEN_DIR / f"{name}.json"


def _check(name, got):
    path = _golden(name)
    if REGENERATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
    expected = json.loads(path.read_text())
    assert sorted(got) == sorted(expected)
    for key in expected:
        assert got[key] == expected[key], f"{name}: {key} differs from the v1 golden"


def run_capture(gateway, monkeypatch, configured, golden_name):
    got = capture(gateway, configured)
    got["pool_allowance_offer"] = capture_pool_offer(monkeypatch, configured)
    _check(golden_name, got)
    return got


@pytest.mark.parametrize("network", NETWORKS)
def test_v1_output_matches_golden(gateway, monkeypatch, network):
    got = run_capture(gateway, monkeypatch, network, network)
    # Sanity: the goldens really cover the facilitator calls and the 402s.
    assert [c["path"] for c in got["paid_settle_before_delivery_requests"]] == ["verify", "settle"]
    assert [c["path"] for c in got["paid_settle_after_response_requests"]] == ["verify", "settle"]
    assert json.loads(got["no_header_stamps"]["body"])["accepts"][0]["network"] == network
