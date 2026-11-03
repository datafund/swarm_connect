# tests/test_bee_unreachable_589.py
"""
/health and payments while the Bee node cannot be reached (#589).

Measured on staging: with every Bee call failing, /health said "ok" (with
bee_node.healthy false and every Bee field null), and the pre-flight check
logged can_accept=True with xbzz_ok, xdai_ok and chequebook_ok all false. So
the gateway kept offering paid purchases that could not complete.

What clients rely on, and these tests keep:
- / and /health answer 200 (Docker's `curl -f /`, the CLI and the MCP server);
- bee_node.healthy is present (#586's probe);
- status stays within "ok" | "degraded" | "critical", and an unreachable Bee is
  "degraded", not "critical": the SDK's health() is false only on "critical".
"""
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

from app.core.config import settings
from app.services import health_snapshot, swarm_api

BASE = {"ok": True, "is_critical": False, "balance_eth": 0.01, "threshold_eth": 0.005,
        "critical_eth": 0.001, "address": "0xPayTo", "warning": None}
GNOSIS_OK = {
    "can_accept": True, "wallet_address": "0xBee", "chequebook_address": "0xCheque",
    "xbzz_ok": True, "xdai_ok": True, "chequebook_ok": True,
    "balances": {"xbzz": {}, "xdai": {}, "chequebook": {}}, "warnings": [], "errors": [],
}
NODE_OK = {"healthy": True, "api_reachable": True, "network_availability": "Available",
           "connected_peers": 120, "warnings": []}


class RaisingBee:
    """A Bee client every call of which fails to connect."""

    def __init__(self):
        self.calls = 0

    async def _fail(self, *a, **k):
        self.calls += 1
        raise httpx.ConnectError("connection refused")

    get = post = patch_ = delete = _fail


@pytest.fixture
def bee_down(monkeypatch):
    bee = RaisingBee()
    monkeypatch.setattr(swarm_api, "get_client", lambda: bee)
    monkeypatch.setitem(swarm_api._node_status_cache, "data", None)
    return bee


@pytest.fixture
def x402_on(monkeypatch):
    monkeypatch.setattr(settings, "X402_ENABLED", True)
    monkeypatch.setattr(settings, "X402_NETWORK", "base-sepolia")
    monkeypatch.setattr(settings, "X402_PAY_TO_ADDRESS", "0x" + "1" * 40)
    monkeypatch.setattr(settings, "X402_FREE_TIER_ENABLED", True)
    monkeypatch.setattr(settings, "HEALTH_SNAPSHOT_TTL_SECONDS", 15.0)
    with patch("app.x402.base_balance.check_base_eth_balance", new=AsyncMock(return_value=dict(BASE))):
        yield


@pytest_asyncio.fixture
async def gw():
    from app.main import app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as c:
        yield c


def _install(node=None, gnosis=None, age=0.0, x402=True):
    """Put a snapshot in place directly, `age` seconds old."""
    snap = health_snapshot.HealthSnapshot(
        node=node, gnosis=gnosis, base_eth=dict(BASE) if x402 else None, x402=x402,
        taken_at=time.monotonic() - age)
    health_snapshot._current = snap
    return snap


# --------------------------------------------------------------------------- #
# can_accept when a balance cannot be read
# --------------------------------------------------------------------------- #
class TestPreflight:

    @pytest.mark.asyncio
    async def test_unreadable_balances_close_payments_without_a_blocking_error(self, bee_down):
        from app.x402.preflight import check_preflight_balances
        r = await check_preflight_balances()
        assert bee_down.calls > 0
        assert r["can_accept"] is False
        assert r["xbzz_ok"] is False and r["xdai_ok"] is False
        # Not a blocking error: that would make /health critical.
        assert r["errors"] == []
        assert any("Paid requests are refused" in w for w in r["warnings"])

    @pytest.mark.asyncio
    async def test_only_the_chequebook_unreadable_keeps_payments_open(self, monkeypatch):
        from app.x402 import preflight
        monkeypatch.setattr(preflight, "get_wallet_info", AsyncMock(return_value={
            "walletAddress": "0xBee", "bzzBalance": str(100 * preflight.PLUR_PER_BZZ),
            "nativeTokenBalance": str(10 * preflight.WEI_PER_XDAI)}))
        monkeypatch.setattr(preflight, "get_chequebook_info", AsyncMock(side_effect=httpx.ConnectError("x")))
        monkeypatch.setattr(preflight, "get_chequebook_balance", AsyncMock(side_effect=httpx.ConnectError("x")))
        r = await preflight.check_preflight_balances()
        assert r["chequebook_ok"] is False
        assert r["can_accept"] is True

    @pytest.mark.asyncio
    async def test_low_but_readable_balances_keep_payments_open(self, monkeypatch):
        from app.x402 import preflight
        monkeypatch.setattr(preflight, "get_wallet_info", AsyncMock(return_value={
            "walletAddress": "0xBee", "bzzBalance": "0", "nativeTokenBalance": "0"}))
        monkeypatch.setattr(preflight, "get_chequebook_info", AsyncMock(return_value={
            "chequebookAddress": "0xC", "availableBalance": "0", "totalBalance": "0"}))
        r = await preflight.check_preflight_balances()
        assert r["xbzz_ok"] is False and r["xdai_ok"] is False
        assert r["can_accept"] is True  # unchanged: low is a warning


# --------------------------------------------------------------------------- #
# /health status
# --------------------------------------------------------------------------- #
class TestHealthStatus:

    @pytest.mark.asyncio
    async def test_unreachable_bee_is_degraded_with_200_and_healthy_false(self, bee_down, x402_on, gw):
        for path in ("/", "/health"):
            r = await gw.get(path)
            assert r.status_code == 200
            body = r.json()
            assert body["status"] == "degraded"
            assert body["status"] != "critical"  # the SDK's health() stays true
            assert body["bee_node"]["healthy"] is False
            assert body["bee_node"]["api_reachable"] is False
            assert body["x402"]["bee_gnosis_wallet"]["can_accept"] is False
            assert body["x402"]["errors"] == []

    @pytest.mark.asyncio
    async def test_unreachable_bee_is_degraded_without_x402(self, bee_down, gw, monkeypatch):
        monkeypatch.setattr(settings, "X402_ENABLED", False)
        r = await gw.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "degraded"
        assert r.json()["bee_node"]["healthy"] is False

    @pytest.mark.asyncio
    async def test_a_node_still_starting_up_is_reachable(self, monkeypatch):
        async def fetch(client, endpoint):
            return {"health": {"status": "ok", "version": "2.8.0"},
                    "addresses": {"overlay": "ab" * 32}}.get(endpoint, {})
        monkeypatch.setattr(swarm_api, "_fetch_bee_json", fetch)
        monkeypatch.setattr(swarm_api, "get_client", lambda: object())
        s = await swarm_api.get_node_status_summary(use_cache=False)
        assert s["healthy"] is False and s["api_reachable"] is True
        from app.main import build_health_response
        with patch.object(settings, "X402_ENABLED", False):
            body = build_health_response(_install(node=s, x402=False))
        assert body["status"] == "ok"

    def test_no_snapshot_yet_is_degraded(self, monkeypatch):
        monkeypatch.setattr(settings, "X402_ENABLED", False)
        from app.main import build_health_response
        body = build_health_response(None)
        assert body["status"] == "degraded"
        assert body["bee_node"]["healthy"] is False

    def test_failed_refresh_on_an_old_snapshot_is_degraded(self, monkeypatch, x402_on):
        from app.main import build_health_response
        snap = _install(node=dict(NODE_OK), gnosis=dict(GNOSIS_OK),
                        age=health_snapshot.max_trusted_age() + 1)
        monkeypatch.setattr(health_snapshot, "_last_failure", "refresh timed out")
        body = build_health_response(snap)
        assert body["status"] == "degraded"
        assert any("too old to trust" in w for w in body["snapshot"]["warnings"])
        assert body["bee_node"]["healthy"] is True  # still the last answer

    def test_failed_refresh_on_a_young_snapshot_stays_ok(self, monkeypatch, x402_on):
        from app.main import build_health_response
        snap = _install(node=dict(NODE_OK), gnosis=dict(GNOSIS_OK), age=20)
        monkeypatch.setattr(health_snapshot, "_last_failure", "refresh timed out")
        assert build_health_response(snap)["status"] == "ok"

    def test_old_snapshot_without_a_failed_refresh_stays_ok(self, x402_on):
        # An idle gateway: refreshes are started by requests.
        from app.main import build_health_response
        snap = _install(node=dict(NODE_OK), gnosis=dict(GNOSIS_OK), age=3600)
        assert build_health_response(snap)["status"] == "ok"

    def test_max_trusted_age_is_at_least_ttl_plus_refresh_timeout(self, monkeypatch):
        monkeypatch.setattr(settings, "HEALTH_SNAPSHOT_MAX_TRUSTED_AGE_SECONDS", 1.0)
        assert health_snapshot.max_trusted_age() == (
            settings.HEALTH_SNAPSHOT_TTL_SECONDS + settings.HEALTH_SNAPSHOT_REFRESH_TIMEOUT_SECONDS)


# --------------------------------------------------------------------------- #
# Paid requests refused before payment
# --------------------------------------------------------------------------- #
async def _bought():
    return {"ok": True}


@pytest.fixture
def facilitator(monkeypatch):
    from app.x402 import dependency as dep
    f = MagicMock()
    f.verify = AsyncMock(side_effect=AssertionError("verify must not be reached"))
    f.settle = AsyncMock(side_effect=AssertionError("settle must not be reached"))
    monkeypatch.setattr(dep, "_get_facilitator_client", lambda: f)
    monkeypatch.setattr(dep, "_calculate_price_for_request",
                        AsyncMock(return_value={"price_usd": 0.05, "description": "Stamp"}))
    return f


@pytest.fixture
def paid_client(x402_on, facilitator):
    from app.x402.dependency import require_x402_payment
    app = FastAPI()
    from app.main import http_exception_handler
    from starlette.exceptions import HTTPException as StarletteHTTPException
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    router = APIRouter(dependencies=[Depends(require_x402_payment)])
    router.add_api_route("/api/v1/stamps/", _bought, methods=["POST"])
    app.include_router(router)
    return TestClient(app)


def _closed():
    _install(node={"healthy": False, "api_reachable": False, "warnings": []},
             gnosis=dict(GNOSIS_OK, can_accept=False, xbzz_ok=False, xdai_ok=False))


PAYMENT = {"X-PAYMENT": "eyJub3QiOiAicmVhbCJ9"}


class TestPaymentGate:

    def test_paid_request_gets_503_before_verify(self, paid_client, facilitator):
        _closed()
        r = paid_client.post("/api/v1/stamps/", headers=PAYMENT)
        assert r.status_code == 503
        assert r.json()["code"] == "PAYMENTS_UNAVAILABLE"
        assert r.json()["detail"]["reason"] == "balances_unreadable"
        assert r.headers["Retry-After"] == "15"
        facilitator.verify.assert_not_called()
        facilitator.settle.assert_not_called()

    def test_unpaid_request_gets_503_not_a_402_offer(self, paid_client):
        _closed()
        r = paid_client.post("/api/v1/stamps/")
        assert r.status_code == 503
        assert r.json()["code"] == "PAYMENTS_UNAVAILABLE"
        assert "accepts" not in r.json() and "accepts" not in r.json()["detail"]

    def test_free_tier_is_not_gated(self, paid_client):
        _closed()
        r = paid_client.post("/api/v1/stamps/", headers={"X-Payment-Mode": "free"})
        assert r.status_code == 200

    def test_free_opt_in_with_free_tier_off_is_gated_like_an_offer(self, paid_client, monkeypatch):
        monkeypatch.setattr(settings, "X402_FREE_TIER_ENABLED", False)
        _closed()
        r = paid_client.post("/api/v1/stamps/", headers={"X-Payment-Mode": "free"})
        assert r.status_code == 503

    def test_open_gate_still_offers_402(self, paid_client):
        _install(node=dict(NODE_OK), gnosis=dict(GNOSIS_OK))
        r = paid_client.post("/api/v1/stamps/")
        assert r.status_code == 402
        assert r.json()["detail"]["accepts"]

    def test_no_snapshot_yet_keeps_the_402(self, paid_client):
        r = paid_client.post("/api/v1/stamps/")
        assert r.status_code == 402

    def test_snapshot_too_old_to_trust_closes_payments(self, paid_client, monkeypatch):
        _install(node=dict(NODE_OK), gnosis=dict(GNOSIS_OK), age=health_snapshot.max_trusted_age() + 1)
        monkeypatch.setattr(health_snapshot, "_last_failure", "refresh timed out")
        monkeypatch.setattr(health_snapshot, "_failed_at", time.monotonic())  # no retry now
        r = paid_client.post("/api/v1/stamps/", headers=PAYMENT)
        assert r.status_code == 503
        assert r.json()["detail"]["reason"] == "snapshot_too_old"

    def test_refusals_are_counted(self, paid_client):
        from app.services.metrics import x402_payments_total
        before = x402_payments_total.labels(mode="unavailable")._value.get()
        _closed()
        paid_client.post("/api/v1/stamps/", headers=PAYMENT)
        assert x402_payments_total.labels(mode="unavailable")._value.get() == before + 1

    @pytest.mark.asyncio
    async def test_snapshot_from_a_raising_bee_closes_payments_without_more_bee_calls(
            self, bee_down, x402_on, facilitator):
        # The real refresh, against a Bee whose every call fails.
        snap = await health_snapshot.get_snapshot(True)
        assert snap.gnosis["can_accept"] is False
        calls = bee_down.calls
        from app.x402.dependency import require_x402_payment
        app = FastAPI()
        router = APIRouter(dependencies=[Depends(require_x402_payment)])
        router.add_api_route("/api/v1/stamps/", _bought, methods=["POST"])
        app.include_router(router)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gw") as c:
            for _ in range(20):
                r = await c.post("/api/v1/stamps/", headers=PAYMENT)
                assert r.status_code == 503
        assert bee_down.calls == calls  # the gate read the snapshot only
        facilitator.verify.assert_not_called()


# --------------------------------------------------------------------------- #
# Pool acquire: paid acquire gated, free allowance untouched
# --------------------------------------------------------------------------- #
class TestPool:

    def test_paid_pool_acquire_is_gated(self, x402_on, facilitator):
        from app.x402.dependency import settle_payment_if_offered
        app = FastAPI()
        router = APIRouter(dependencies=[Depends(settle_payment_if_offered)])
        router.add_api_route("/api/v1/pool/acquire", _bought, methods=["POST"])
        app.include_router(router)
        _closed()
        client = TestClient(app)
        assert client.post("/api/v1/pool/acquire", headers=PAYMENT).status_code == 503
        # No payment offered: the free allowance path is not touched.
        assert client.post("/api/v1/pool/acquire").status_code == 200

    def test_exhausted_allowance_answers_429_not_a_402_offer(self, monkeypatch, tmp_path, x402_on):
        from app.main import app
        from app.services import pool_allowance
        from app.services.pool_allowance import PoolAllowanceTracker
        import app.api.endpoints.pool as pool_ep
        monkeypatch.setattr(settings, "X402_NETWORK", "base")  # paying would bypass
        monkeypatch.setattr(settings, "STAMP_POOL_ENABLED", True)
        monkeypatch.setattr(settings, "POOL_DAILY_ALLOWANCES", "")
        monkeypatch.setattr(settings, "POOL_DEFAULT_DAILY_ALLOWANCE", 0)
        tracker = PoolAllowanceTracker(state_file=str(tmp_path / "a.json"))
        monkeypatch.setattr(pool_allowance, "pool_allowance_tracker", tracker)
        monkeypatch.setattr(pool_ep, "pool_allowance_tracker", tracker)
        monkeypatch.setattr(pool_ep.stamp_pool_manager, "get_available_stamp",
                            lambda d: type("S", (), {"depth": 17})())
        from app.x402 import dependency as dep
        monkeypatch.setattr(dep, "_calculate_price_for_request",
                            AsyncMock(return_value={"price_usd": 0.05, "description": "Pooled stamp"}))
        _closed()
        r = TestClient(app).post("/api/v1/pool/acquire", json={"size": "small"})
        assert r.status_code == 429, r.text
        assert "accepts" not in r.json() and "accepts" not in r.json()["detail"]
        assert "X-PAYMENT" not in r.json()["detail"]["message"]


# --------------------------------------------------------------------------- #
# Retries of attempts that may have been charged pass the gate (review round 1)
# --------------------------------------------------------------------------- #
class TestRetriesOfPossiblyChargedRequests:
    """While payments are refused, a retry still gets its stored outcome.

    Uses the real dependency, settlement and middleware from the idempotency
    suite. The gate is closed only after the first attempt.
    """

    @pytest.fixture
    def fac(self, monkeypatch):
        import secrets as _secrets
        from x402.types import SettleResponse, VerifyResponse
        from app.x402.ratelimit import reset_rate_limiter
        monkeypatch.setattr(settings, "X402_ENABLED", True)
        monkeypatch.setattr(settings, "X402_PAY_TO_ADDRESS", "0xpayee")
        monkeypatch.setattr(settings, "X402_NETWORK", "base-sepolia")
        monkeypatch.setattr(settings, "HEALTH_SNAPSHOT_TTL_SECONDS", 15.0)
        reset_rate_limiter()
        f = MagicMock()
        f.verify = AsyncMock(side_effect=lambda payment, **kw: VerifyResponse(
            isValid=True, payer=payment.payload.authorization.from_))
        f.settle = AsyncMock(side_effect=lambda **kw: SettleResponse(
            success=True, transaction="0x" + _secrets.token_hex(32)))
        with patch("app.x402.dependency._get_facilitator_client", return_value=f), \
             patch("app.x402.dependency._calculate_price_for_request",
                   new=AsyncMock(return_value={"price_usd": 0.02, "description": "t"})):
            yield f
        reset_rate_limiter()

    @staticmethod
    def _client(fac):
        import secrets as _secrets
        from fastapi import Request
        from app.x402.middleware import X402Middleware
        from app.x402.settlement import settle_payment
        router = APIRouter()
        bought = []

        @router.post("/", status_code=201)
        async def buy(request: Request, body: dict):
            await settle_payment(request)
            bought.append(_secrets.token_hex(32))
            return {"batchID": bought[-1]}

        app = FastAPI()
        app.include_router(router, prefix="/api/v1/stamps", dependencies=[Depends(require_payment())])
        app.add_middleware(X402Middleware, facilitator_client=fac)
        return TestClient(app), bought

    def test_stored_result_is_replayed_while_payments_are_refused(self, fac):
        c, bought = self._client(fac)
        first = c.post("/api/v1/stamps/", json={"depth": 17},
                       headers={"X-PAYMENT": pay(), "Idempotency-Key": "k1"})
        assert first.status_code == 201
        _closed()
        retry = c.post("/api/v1/stamps/", json={"depth": 17},
                       headers={"X-PAYMENT": pay(), "Idempotency-Key": "k1"})
        assert retry.status_code == 201
        assert retry.json() == first.json()
        assert retry.headers["Idempotent-Replayed"] == "true"
        assert fac.settle.await_count == 1 and len(bought) == 1

    def test_settling_key_gets_its_409_while_payments_are_refused(self, fac):
        from x402.types import SettleResponse
        c, bought = self._client(fac)
        fac.settle.side_effect = httpx.ReadTimeout("facilitator slow")
        r = c.post("/api/v1/stamps/", json={"depth": 17},
                   headers={"X-PAYMENT": pay(), "Idempotency-Key": "k1"})
        assert r.status_code == 502
        fac.settle.side_effect = lambda **kw: SettleResponse(success=True, transaction="0x" + "cd" * 32)
        _closed()
        retry = c.post("/api/v1/stamps/", json={"depth": 17},
                       headers={"X-PAYMENT": pay(), "Idempotency-Key": "k1"})
        assert retry.status_code == 409
        assert retry.json()["detail"]["code"] == "IDEMPOTENCY_KEY_SETTLEMENT_UNKNOWN"
        assert fac.settle.await_count == 1 and not bought

    def test_unknown_outcome_authorization_gets_its_409_while_payments_are_refused(self, fac):
        from app.x402.settlement import AFTER_WORK, outcome_unknown
        c, bought = self._client(fac)
        nonce = "0x" + "5e" * 32
        outcome_unknown.add((ALICE.lower(), nonce), AFTER_WORK)
        _closed()
        r = c.post("/api/v1/stamps/", json={"depth": 17}, headers={"X-PAYMENT": pay(nonce=nonce)})
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "PAYMENT_AUTHORIZATION_OUTCOME_UNKNOWN"
        fac.settle.assert_not_called()
        assert not bought

    def test_a_fresh_payment_is_still_refused(self, fac):
        c, bought = self._client(fac)
        _closed()
        for headers in ({"X-PAYMENT": pay()}, {"X-PAYMENT": pay(), "Idempotency-Key": "new-key"}):
            r = c.post("/api/v1/stamps/", json={"depth": 17}, headers=headers)
            assert r.status_code == 503
            assert r.json()["detail"]["code"] == "PAYMENTS_UNAVAILABLE"
            assert "This request was not charged" in r.json()["detail"]["message"]
        fac.verify.assert_not_called()
        fac.settle.assert_not_called()

    def test_another_payers_key_does_not_open_the_gate(self, fac):
        c, _ = self._client(fac)
        assert c.post("/api/v1/stamps/", json={"depth": 17},
                      headers={"X-PAYMENT": pay(), "Idempotency-Key": "k1"}).status_code == 201
        _closed()
        r = c.post("/api/v1/stamps/", json={"depth": 17},
                   headers={"X-PAYMENT": pay(MALLORY), "Idempotency-Key": "k1"})
        assert r.status_code == 503


ALICE = "0x" + "a1" * 20
MALLORY = "0x" + "b2" * 20


def pay(payer=ALICE, nonce=None):
    """A payment header with a fresh nonce, as a retrying client would send."""
    import base64
    import json
    import secrets as _secrets
    payload = {"x402Version": 1, "scheme": "exact", "network": "base-sepolia", "payload": {
        "signature": "0x" + "ab" * 65,
        "authorization": {"from": payer, "to": "0xpayee", "value": "100000", "validAfter": "0",
                          "validBefore": "9999999999", "nonce": nonce or "0x" + _secrets.token_hex(32)}}}
    return base64.b64encode(json.dumps(payload).encode()).decode()


def require_payment():
    from app.x402.dependency import require_x402_payment
    return require_x402_payment


class TestPoolRefusalWhilePaymentsAreRefused:

    def test_429_says_retry_later_and_does_not_send_callers_to_paid_routes(
            self, monkeypatch, tmp_path, x402_on):
        from app.main import app
        from app.services import pool_allowance
        from app.services.pool_allowance import PoolAllowanceTracker
        import app.api.endpoints.pool as pool_ep
        # A testnet, where the refusal would otherwise send callers to POST /stamps/.
        monkeypatch.setattr(settings, "STAMP_POOL_ENABLED", True)
        monkeypatch.setattr(settings, "POOL_DAILY_ALLOWANCES", "")
        monkeypatch.setattr(settings, "POOL_DEFAULT_DAILY_ALLOWANCE", 0)
        tracker = PoolAllowanceTracker(state_file=str(tmp_path / "a.json"))
        monkeypatch.setattr(pool_allowance, "pool_allowance_tracker", tracker)
        monkeypatch.setattr(pool_ep, "pool_allowance_tracker", tracker)
        monkeypatch.setattr(pool_ep.stamp_pool_manager, "get_available_stamp",
                            lambda d: type("S", (), {"depth": 17})())
        _closed()
        r = TestClient(app).post("/api/v1/pool/acquire", json={"size": "small"})
        assert r.status_code == 429, r.text
        assert r.headers["Retry-After"] == "15"
        d = r.json()["detail"]
        assert "/api/v1/stamps/" not in d["message"]
        assert "temporarily refused" in d["message"]
        assert d["alternative"]["retry_after_seconds"] == 15
        assert "endpoint" not in d["alternative"]
