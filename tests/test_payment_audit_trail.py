"""Every paid delivery is linked to its settlement in the audit log (#375)."""
import json
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.testclient import TestClient
from x402.types import SettleResponse, VerifyResponse

from app.core.config import settings
from app.services.metrics import x402_settlements_total
from app.x402.audit import read_audit_log
from app.x402.dependency import require_x402_payment
from app.x402.middleware import X402Middleware
from app.x402.ratelimit import reset_rate_limiter
from app.x402.settlement import settle_payment
from tests.test_x402_integration import _configure, create_valid_payment_header

TX = "0x" + "ef" * 32


async def buy(request: Request):
    await settle_payment(request)
    return {"batchID": "b" * 64, "token": "secret-bearer"}


async def settle_then_fail(request: Request):
    from fastapi import HTTPException
    await settle_payment(request)
    raise HTTPException(status_code=502, detail="Bee unavailable")


async def no_settle_point():
    return {"ok": True}


def _run(settle, path="/api/v1/stamps/"):
    reset_rate_limiter()
    fac = MagicMock()
    fac.verify = AsyncMock(return_value=VerifyResponse(isValid=True, payer="0xpayer"))
    fac.settle = AsyncMock(side_effect=settle) if isinstance(settle, Exception) else AsyncMock(return_value=settle)
    app = FastAPI()
    router = APIRouter(dependencies=[Depends(require_x402_payment)])
    router.add_api_route("/api/v1/stamps/", buy, methods=["POST"])
    router.add_api_route("/api/v1/stamps/fail", settle_then_fail, methods=["POST"])
    router.add_api_route("/api/v1/data/manifest", no_settle_point, methods=["POST"])
    app.include_router(router)
    app.add_middleware(X402Middleware, facilitator_client=fac)
    with patch("app.x402.dependency._calculate_price_for_request",
               new=AsyncMock(return_value={"price_usd": 0.02, "description": "t"})), \
         patch("app.x402.dependency._get_facilitator_client", return_value=fac), \
         patch("app.x402.middleware.settings") as mw, patch("app.x402.dependency.settings") as dep:
        _configure(dep, mw)
        return TestClient(app).post(path, headers={"X-PAYMENT": create_valid_payment_header()})


def _count(result):
    return x402_settlements_total.labels(result=result)._value.get()


def test_delivery_is_linked_to_the_settlement_transaction():
    before = _count("settled")
    assert _run(SettleResponse(success=True, transaction=TX)).status_code == 200
    events = read_audit_log()
    settled = [e for e in events if e["event_type"] == "payment_settled" and e["data"]["transaction_hash"] == TX]
    delivered = [e for e in events if e["event_type"] == "payment_delivered" and e["data"]["transaction_hash"] == TX]
    assert settled and delivered
    assert delivered[-1]["data"]["resource"] == {"batchID": "b" * 64}
    assert delivered[-1]["data"]["amount"] == "20000"
    assert "secret-bearer" not in json.dumps(delivered[-1])  # never log a bearer token
    assert _count("settled") == before + 1


def test_a_refused_settlement_is_counted():
    before = _count("refused")
    assert _run(SettleResponse(success=False, errorReason="insufficient_funds")).status_code == 402
    assert _count("refused") == before + 1


def test_audit_log_defaults_to_the_persistent_volume():
    from app.core.config import Settings
    assert Settings.model_fields["X402_AUDIT_LOG_PATH"].default.startswith("data/")


def test_settlement_alert_rule_is_defined():
    import os
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    rules = json.load(open(os.path.join(repo, "monitoring/alerting/alert-rules.json")))
    rule = next(r for r in rules if r["title"] == "x402 settlement problems")
    assert "gateway_x402_settlements_total" in rule["data"][0]["model"]["expr"]



def test_all_results_exist_from_startup():
    from prometheus_client import REGISTRY
    names = {s.labels["result"] for m in REGISTRY.collect() if m.name == "gateway_x402_settlements"
             for s in m.samples if s.name == "gateway_x402_settlements_total"}
    assert {"settled", "refused", "error", "settled_not_delivered"} <= names


def test_settled_but_not_delivered_is_counted_and_not_recorded_as_delivered():
    before = _count("settled_not_delivered")
    r = _run(SettleResponse(success=True, transaction=TX), path="/api/v1/stamps/fail")
    assert r.status_code == 502
    assert _count("settled_not_delivered") == before + 1
    delivered = [e for e in read_audit_log() if e["event_type"] == "payment_delivered"
                 and e["data"]["path"] == "/api/v1/stamps/fail"]
    assert delivered == []


def test_an_unknown_settlement_outcome_is_an_error_not_a_refusal():
    """Middleware fallback path (route without a settle point)."""
    before_error, before_refused = _count("error"), _count("refused")
    r = _run(RuntimeError("facilitator timeout"), path="/api/v1/data/manifest")
    assert r.status_code == 402
    assert _count("error") == before_error + 1
    assert _count("refused") == before_refused
