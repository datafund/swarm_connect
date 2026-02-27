# tests/test_x402_integration.py
"""
Integration tests for x402 payment gateway.

These tests verify the full x402 flow including:
- Dependency + middleware integration with FastAPI
- Validation-before-payment ordering (issue #95)
- Access control (whitelist/blacklist)
- Rate limiting
- Audit logging
- 402 response generation
- Payment verification and settlement
"""
import json
import os
import pytest
import tempfile
from unittest.mock import patch, MagicMock, AsyncMock

from fastapi import FastAPI, Depends, APIRouter, Path
from fastapi.testclient import TestClient
from pydantic import BaseModel

from x402.types import PaymentRequirements, PaymentPayload, VerifyResponse, SettleResponse
from x402.encoding import safe_base64_encode

from app.x402.middleware import X402Middleware, PROTECTED_ENDPOINTS
from app.x402.dependency import require_x402_payment
from app.x402.access import check_access
from app.x402.ratelimit import reset_rate_limiter
from app.x402.audit import (
    get_audit_log_path,
    read_audit_log,
    AuditEventType,
)


# Default non-critical balance result for mocking
OK_BALANCE = {
    "ok": True,
    "is_critical": False,
    "balance_wei": int(0.01 * 10**18),
    "balance_eth": 0.01,
    "threshold_eth": 0.005,
    "critical_eth": 0.001,
    "address": "0x1234",
    "warning": None,
}


def _make_app(*routes, facilitator_client=None):
    """Create a test FastAPI app with x402 dependency and middleware."""
    app = FastAPI()
    router = APIRouter(dependencies=[Depends(require_x402_payment)])
    for method, path, handler in routes:
        router.add_api_route(path, handler, methods=[method])
    app.include_router(router)
    app.add_middleware(X402Middleware, facilitator_client=facilitator_client or MagicMock())
    return app


def _make_simple_app(*routes):
    """Create a test FastAPI app WITHOUT x402 (for disabled tests)."""
    app = FastAPI()
    for method, path, handler in routes:
        app.add_api_route(path, handler, methods=[method])
    app.add_middleware(X402Middleware)
    return app


def _configure(mock_dep, mock_mw, *, free_tier=False):
    """Set common mock values on both dependency and middleware settings mocks."""
    mock_dep.X402_ENABLED = True
    mock_dep.X402_NETWORK = "base-sepolia"
    mock_dep.X402_PAY_TO_ADDRESS = "0xpayee"
    mock_dep.X402_MIN_PRICE_USD = 0.01
    mock_dep.X402_FREE_TIER_ENABLED = free_tier
    mock_dep.X402_FREE_TIER_RATE_LIMIT = 3

    mock_mw.X402_ENABLED = True
    mock_mw.X402_NETWORK = "base-sepolia"
    mock_mw.X402_PAY_TO_ADDRESS = "0xpayee"


def create_valid_payment_header(
    payer: str = "0x1234567890abcdef1234567890abcdef12345678",
    amount: str = "100000",
    network: str = "base-sepolia"
) -> str:
    """Create a valid base64-encoded payment header."""
    payload = {
        "x402Version": 1,
        "scheme": "exact",
        "network": network,
        "payload": {
            "signature": "0x" + "ab" * 65,
            "authorization": {
                "from": payer,
                "to": "0xpayee",
                "value": amount,
                "validAfter": "0",
                "validBefore": "9999999999",
                "nonce": "0x" + "00" * 32,
            }
        }
    }
    return safe_base64_encode(json.dumps(payload).encode("utf-8"))


# --- Endpoint handlers ---
async def create_stamp():
    return {"stamp_id": "test-stamp-123", "status": "created"}

async def upload_data():
    return {"reference": "abc123def456", "status": "uploaded"}

async def upload_manifest():
    return {"reference": "manifest789", "status": "uploaded"}

async def health():
    return {"status": "healthy"}


class TestMiddlewareIntegration:
    """Test x402 middleware with full FastAPI integration."""

    def setup_method(self):
        reset_rate_limiter()

    def teardown_method(self):
        reset_rate_limiter()

    @patch("app.x402.middleware.settings")
    def test_x402_disabled_passes_through(self, mock_mw):
        mock_mw.X402_ENABLED = False
        app = _make_simple_app(("POST", "/api/v1/stamps/", create_stamp))
        client = TestClient(app)
        response = client.post("/api/v1/stamps/")
        assert response.status_code == 200
        assert response.json()["status"] == "created"

    @patch("app.x402.dependency.check_base_eth_balance", return_value=OK_BALANCE)
    @patch("app.x402.middleware.settings")
    @patch("app.x402.dependency.settings")
    def test_unprotected_endpoint_passes_through(self, mock_dep, mock_mw, mock_balance):
        _configure(mock_dep, mock_mw)
        app = _make_app(("GET", "/api/v1/health", health))
        client = TestClient(app)
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        assert response.json()["status"] == "healthy"

    @patch("app.x402.dependency.check_base_eth_balance", return_value=OK_BALANCE)
    @patch("app.x402.dependency.get_price_quote")
    @patch("app.x402.middleware.settings")
    @patch("app.x402.dependency.settings")
    def test_protected_endpoint_returns_402_without_payment(self, mock_dep, mock_mw, mock_price, mock_balance):
        _configure(mock_dep, mock_mw, free_tier=False)
        mock_price.return_value = {"price_usd": 0.05, "description": "Test operation"}

        app = _make_app(("POST", "/api/v1/stamps/", create_stamp))
        client = TestClient(app)
        response = client.post("/api/v1/stamps/")

        assert response.status_code == 402
        data = response.json()["detail"]
        assert data["x402Version"] == 1
        assert "accepts" in data
        assert len(data["accepts"]) > 0

    @patch("app.x402.dependency.check_base_eth_balance", return_value=OK_BALANCE)
    @patch("app.x402.dependency.get_price_quote")
    @patch("app.x402.middleware.settings")
    @patch("app.x402.dependency.settings")
    def test_402_response_contains_payment_requirements(self, mock_dep, mock_mw, mock_price, mock_balance):
        _configure(mock_dep, mock_mw, free_tier=False)
        mock_dep.X402_PAY_TO_ADDRESS = "0xTestPayee"
        mock_mw.X402_PAY_TO_ADDRESS = "0xTestPayee"
        mock_price.return_value = {"price_usd": 0.10, "description": "Stamp purchase"}

        app = _make_app(("POST", "/api/v1/stamps/", create_stamp))
        client = TestClient(app)
        response = client.post("/api/v1/stamps/")

        assert response.status_code == 402
        data = response.json()["detail"]
        requirements = data["accepts"][0]
        assert requirements["scheme"] == "exact"
        assert requirements["network"] == "base-sepolia"
        assert requirements["payTo"] == "0xTestPayee"
        assert int(requirements["maxAmountRequired"]) == 100000

    @patch("app.x402.dependency.check_base_eth_balance", return_value=OK_BALANCE)
    @patch("app.x402.dependency.get_price_quote")
    @patch("app.x402.middleware.settings")
    @patch("app.x402.dependency.settings")
    def test_invalid_payment_header_returns_402(self, mock_dep, mock_mw, mock_price, mock_balance):
        _configure(mock_dep, mock_mw)
        mock_price.return_value = {"price_usd": 0.05, "description": "Test operation"}

        app = _make_app(("POST", "/api/v1/stamps/", create_stamp))
        client = TestClient(app)
        response = client.post("/api/v1/stamps/", headers={"X-PAYMENT": "invalid-base64-data!!!"})

        assert response.status_code == 402
        assert "Invalid X-PAYMENT header" in response.json()["detail"]["error"]


class TestValidationBeforePayment:
    """Test that x402 no longer blocks at the ASGI level (issue #95).

    With the dependency approach, x402 checks run after route matching,
    so non-existent routes get 404 and wrong HTTP methods get 405.
    Router-level dependencies still run before endpoint parameter
    validation (body, typed path params), so those still get 402.
    """

    def setup_method(self):
        reset_rate_limiter()

    def teardown_method(self):
        reset_rate_limiter()

    @patch("app.x402.dependency.check_base_eth_balance", return_value=OK_BALANCE)
    @patch("app.x402.middleware.settings")
    @patch("app.x402.dependency.settings")
    def test_nonexistent_route_returns_404_not_402(self, mock_dep, mock_mw, mock_balance):
        """Requests to non-existent routes should return 404, not 402."""
        _configure(mock_dep, mock_mw, free_tier=False)

        app = _make_app(("POST", "/api/v1/stamps/", create_stamp))
        client = TestClient(app)
        response = client.post("/api/v1/nonexistent/")
        assert response.status_code == 404

    @patch("app.x402.dependency.check_base_eth_balance", return_value=OK_BALANCE)
    @patch("app.x402.middleware.settings")
    @patch("app.x402.dependency.settings")
    def test_get_on_protected_path_passes_through(self, mock_dep, mock_mw, mock_balance):
        """GET requests on protected endpoint paths should pass through."""
        _configure(mock_dep, mock_mw, free_tier=False)

        async def list_stamps():
            return {"stamps": []}

        app = _make_app(
            ("POST", "/api/v1/stamps/", create_stamp),
            ("GET", "/api/v1/stamps/", list_stamps),
        )
        client = TestClient(app)
        response = client.get("/api/v1/stamps/")
        assert response.status_code == 200
        assert response.json() == {"stamps": []}

    @patch("app.x402.dependency.check_base_eth_balance", return_value=OK_BALANCE)
    @patch("app.x402.middleware.settings")
    @patch("app.x402.dependency.settings")
    def test_unprotected_get_with_x402_enabled(self, mock_dep, mock_mw, mock_balance):
        """GET endpoints on the same router should not be payment-gated."""
        _configure(mock_dep, mock_mw, free_tier=False)

        async def download_data(reference: str):
            return {"reference": reference, "data": "hello"}

        app = _make_app(
            ("POST", "/api/v1/data/", upload_data),
            ("GET", "/api/v1/data/{reference}", download_data),
        )
        client = TestClient(app)
        response = client.get("/api/v1/data/abc123")
        assert response.status_code == 200
        assert response.json()["reference"] == "abc123"


class TestAccessControlIntegration:
    """Test access control integration with the gateway."""

    @patch("app.x402.access.settings")
    def test_blacklisted_ip_blocked(self, mock_settings):
        mock_settings.X402_BLACKLIST_IPS = "192.168.1.100"
        mock_settings.X402_WHITELIST_IPS = ""
        status, reason = check_access("192.168.1.100")
        assert status == "blocked"
        assert reason == "IP address is blocked"

    @patch("app.x402.access.settings")
    def test_whitelisted_ip_free_access(self, mock_settings):
        mock_settings.X402_BLACKLIST_IPS = ""
        mock_settings.X402_WHITELIST_IPS = "192.168.1.50"
        status, reason = check_access("192.168.1.50")
        assert status == "free"
        assert reason is None

    @patch("app.x402.access.settings")
    def test_normal_ip_requires_payment(self, mock_settings):
        mock_settings.X402_BLACKLIST_IPS = ""
        mock_settings.X402_WHITELIST_IPS = ""
        status, reason = check_access("192.168.1.1")
        assert status == "pay"
        assert reason is None

    @patch("app.x402.access.settings")
    def test_cidr_range_blocking(self, mock_settings):
        mock_settings.X402_BLACKLIST_IPS = "10.0.0.0/8"
        mock_settings.X402_WHITELIST_IPS = ""
        status, _ = check_access("10.50.100.200")
        assert status == "blocked"
        status, _ = check_access("192.168.1.1")
        assert status == "pay"


class TestRateLimitingIntegration:
    """Test rate limiting integration."""

    def setup_method(self):
        reset_rate_limiter()

    def teardown_method(self):
        reset_rate_limiter()

    @patch("app.x402.ratelimit.settings")
    def test_rate_limit_blocks_after_threshold(self, mock_settings):
        from app.x402.ratelimit import check_rate_limit
        mock_settings.X402_RATE_LIMIT_PER_IP = 3

        for i in range(3):
            is_allowed, reason, stats = check_rate_limit("192.168.1.1")
            assert is_allowed is True

        is_allowed, reason, stats = check_rate_limit("192.168.1.1")
        assert is_allowed is False
        assert "Rate limit exceeded" in reason

    @patch("app.x402.ratelimit.settings")
    def test_different_ips_have_separate_limits(self, mock_settings):
        from app.x402.ratelimit import check_rate_limit
        mock_settings.X402_RATE_LIMIT_PER_IP = 2

        check_rate_limit("192.168.1.1")
        check_rate_limit("192.168.1.1")
        is_allowed, _, _ = check_rate_limit("192.168.1.1")
        assert is_allowed is False

        is_allowed, _, _ = check_rate_limit("192.168.1.2")
        assert is_allowed is True


class TestAuditLoggingIntegration:
    """Test audit logging integration."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.audit_log_path = os.path.join(self.temp_dir, "x402_audit.jsonl")

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch("app.x402.audit.settings")
    def test_audit_event_logged(self, mock_settings):
        from app.x402.audit import log_audit_event
        mock_settings.X402_AUDIT_LOG_PATH = self.audit_log_path

        request_id = log_audit_event(
            event_type=AuditEventType.REQUEST_RECEIVED,
            data={"method": "POST", "path": "/api/v1/stamps/"},
            client_ip="192.168.1.1"
        )
        assert request_id is not None

        with open(self.audit_log_path, "r") as f:
            event = json.loads(f.readline())
        assert event["event_type"] == "request_received"
        assert event["client_ip"] == "192.168.1.1"

    @patch("app.x402.audit.settings")
    def test_audit_log_reading(self, mock_settings):
        from app.x402.audit import log_audit_event
        mock_settings.X402_AUDIT_LOG_PATH = self.audit_log_path

        log_audit_event(event_type=AuditEventType.REQUEST_RECEIVED, data={"method": "POST"}, client_ip="192.168.1.1")
        log_audit_event(event_type=AuditEventType.PAYMENT_RECEIVED, data={"amount": "100000"}, client_ip="192.168.1.1")

        events = read_audit_log(max_entries=10)
        assert len(events) == 2
        assert events[0]["event_type"] == "payment_received"
        assert events[1]["event_type"] == "request_received"

    @patch("app.x402.audit.settings")
    def test_audit_log_filtering(self, mock_settings):
        from app.x402.audit import log_audit_event
        mock_settings.X402_AUDIT_LOG_PATH = self.audit_log_path

        log_audit_event(event_type=AuditEventType.REQUEST_RECEIVED, data={}, client_ip="192.168.1.1")
        log_audit_event(event_type=AuditEventType.REQUEST_RECEIVED, data={}, client_ip="192.168.1.2")

        events = read_audit_log(client_ip="192.168.1.1")
        assert len(events) == 1
        assert events[0]["client_ip"] == "192.168.1.1"


class TestFullPaymentFlow:
    """Test full payment flow with mocked facilitator."""

    def setup_method(self):
        reset_rate_limiter()

    def teardown_method(self):
        reset_rate_limiter()

    @patch("app.x402.dependency.check_base_eth_balance", return_value=OK_BALANCE)
    @patch("app.x402.dependency._get_facilitator_client")
    @patch("app.x402.dependency.get_price_quote")
    @patch("app.x402.middleware.settings")
    @patch("app.x402.dependency.settings")
    def test_successful_payment_flow(self, mock_dep, mock_mw, mock_price, mock_get_fac, mock_balance):
        _configure(mock_dep, mock_mw)
        mock_dep.X402_FACILITATOR_URL = "https://x402.org/facilitator"
        mock_price.return_value = {"price_usd": 0.05, "description": "Test stamp"}

        # Dependency uses this to verify
        mock_fac = MagicMock()
        mock_fac.verify = AsyncMock(return_value=VerifyResponse(
            is_valid=True, invalid_reason=None,
            payer="0x1234567890abcdef1234567890abcdef12345678"
        ))
        mock_get_fac.return_value = mock_fac

        # Middleware uses this to settle
        mock_mw_fac = MagicMock()
        mock_mw_fac.settle = AsyncMock(return_value=SettleResponse(
            success=True, transaction="0x" + "ab" * 32, network="base-sepolia"
        ))

        app = _make_app(
            ("POST", "/api/v1/stamps/", create_stamp),
            facilitator_client=mock_mw_fac,
        )
        client = TestClient(app)

        payment_header = create_valid_payment_header()
        response = client.post("/api/v1/stamps/", headers={"X-PAYMENT": payment_header})

        assert response.status_code == 200
        assert response.json()["status"] == "created"
        assert "X-PAYMENT-RESPONSE" in response.headers
        mock_fac.verify.assert_called_once()
        mock_mw_fac.settle.assert_called_once()

    @patch("app.x402.dependency.check_base_eth_balance", return_value=OK_BALANCE)
    @patch("app.x402.dependency._get_facilitator_client")
    @patch("app.x402.dependency.get_price_quote")
    @patch("app.x402.middleware.settings")
    @patch("app.x402.dependency.settings")
    def test_payment_verification_failure(self, mock_dep, mock_mw, mock_price, mock_get_fac, mock_balance):
        _configure(mock_dep, mock_mw)
        mock_dep.X402_FACILITATOR_URL = "https://x402.org/facilitator"
        mock_price.return_value = {"price_usd": 0.05, "description": "Test stamp"}

        mock_fac = MagicMock()
        mock_fac.verify = AsyncMock(return_value=VerifyResponse(
            is_valid=False, invalid_reason="Insufficient balance", payer=None
        ))
        mock_get_fac.return_value = mock_fac

        app = _make_app(("POST", "/api/v1/stamps/", create_stamp))
        client = TestClient(app)

        payment_header = create_valid_payment_header()
        response = client.post("/api/v1/stamps/", headers={"X-PAYMENT": payment_header})

        assert response.status_code == 402
        assert "Insufficient balance" in response.json()["detail"]["error"]


class TestProtectedEndpoints:
    """Test all protected endpoints are properly gated."""

    def setup_method(self):
        reset_rate_limiter()

    def teardown_method(self):
        reset_rate_limiter()

    @patch("app.x402.dependency.check_base_eth_balance", return_value=OK_BALANCE)
    @patch("app.x402.dependency.get_price_quote")
    @patch("app.x402.middleware.settings")
    @patch("app.x402.dependency.settings")
    def test_stamps_endpoint_protected(self, mock_dep, mock_mw, mock_price, mock_balance):
        _configure(mock_dep, mock_mw, free_tier=False)
        mock_price.return_value = {"price_usd": 0.05, "description": "Test"}
        app = _make_app(("POST", "/api/v1/stamps/", create_stamp))
        client = TestClient(app)
        response = client.post("/api/v1/stamps/")
        assert response.status_code == 402

    @patch("app.x402.dependency.check_base_eth_balance", return_value=OK_BALANCE)
    @patch("app.x402.dependency.get_price_quote")
    @patch("app.x402.middleware.settings")
    @patch("app.x402.dependency.settings")
    def test_data_endpoint_protected(self, mock_dep, mock_mw, mock_price, mock_balance):
        _configure(mock_dep, mock_mw, free_tier=False)
        mock_price.return_value = {"price_usd": 0.05, "description": "Test"}
        app = _make_app(("POST", "/api/v1/data/", upload_data))
        client = TestClient(app)
        response = client.post("/api/v1/data/")
        assert response.status_code == 402

    @patch("app.x402.dependency.check_base_eth_balance", return_value=OK_BALANCE)
    @patch("app.x402.dependency.get_price_quote")
    @patch("app.x402.middleware.settings")
    @patch("app.x402.dependency.settings")
    def test_manifest_endpoint_protected(self, mock_dep, mock_mw, mock_price, mock_balance):
        _configure(mock_dep, mock_mw, free_tier=False)
        mock_price.return_value = {"price_usd": 0.05, "description": "Test"}
        app = _make_app(("POST", "/api/v1/data/manifest", upload_manifest))
        client = TestClient(app)
        response = client.post("/api/v1/data/manifest")
        assert response.status_code == 402


class TestEndToEndScenarios:
    """Test complete end-to-end scenarios."""

    def setup_method(self):
        reset_rate_limiter()

    def teardown_method(self):
        reset_rate_limiter()

    @patch("app.x402.dependency.check_base_eth_balance", return_value=OK_BALANCE)
    @patch("app.x402.dependency._get_facilitator_client")
    @patch("app.x402.dependency.get_price_quote")
    @patch("app.x402.middleware.settings")
    @patch("app.x402.dependency.settings")
    def test_gateway_operator_workflow(self, mock_dep, mock_mw, mock_price, mock_get_fac, mock_balance):
        # Step 1: x402 disabled
        mock_mw.X402_ENABLED = False
        app = _make_simple_app(("POST", "/api/v1/stamps/", create_stamp))
        client = TestClient(app)
        response = client.post("/api/v1/stamps/")
        assert response.status_code == 200

        # Step 2: Enable x402
        _configure(mock_dep, mock_mw, free_tier=False)
        mock_dep.X402_FACILITATOR_URL = "https://x402.org/facilitator"
        mock_price.return_value = {"price_usd": 0.05, "description": "Test"}

        mock_fac = MagicMock()
        mock_fac.verify = AsyncMock(return_value=VerifyResponse(
            is_valid=True, invalid_reason=None,
            payer="0x1234567890abcdef1234567890abcdef12345678"
        ))
        mock_get_fac.return_value = mock_fac

        mock_mw_fac = MagicMock()
        mock_mw_fac.settle = AsyncMock(return_value=SettleResponse(
            success=True, transaction="0x" + "ab" * 32, network="base-sepolia"
        ))

        app2 = _make_app(
            ("POST", "/api/v1/stamps/", create_stamp),
            facilitator_client=mock_mw_fac,
        )
        client2 = TestClient(app2)

        # Step 3: Without payment, get 402
        response = client2.post("/api/v1/stamps/")
        assert response.status_code == 402

        # Step 4: With payment, succeed
        payment_header = create_valid_payment_header()
        response = client2.post("/api/v1/stamps/", headers={"X-PAYMENT": payment_header})
        assert response.status_code == 200

    @patch("app.x402.dependency.check_base_eth_balance", return_value=OK_BALANCE)
    @patch("app.x402.dependency.get_price_quote")
    @patch("app.x402.middleware.settings")
    @patch("app.x402.dependency.settings")
    def test_price_varies_by_endpoint(self, mock_dep, mock_mw, mock_price, mock_balance):
        _configure(mock_dep, mock_mw, free_tier=False)

        app = _make_app(
            ("POST", "/api/v1/stamps/", create_stamp),
            ("POST", "/api/v1/data/", upload_data),
        )
        client = TestClient(app)

        mock_price.return_value = {"price_usd": 0.10, "description": "Stamp"}
        response = client.post("/api/v1/stamps/")
        stamp_price = int(response.json()["detail"]["accepts"][0]["maxAmountRequired"])

        mock_price.return_value = {"price_usd": 0.05, "description": "Upload"}
        response = client.post("/api/v1/data/")
        data_price = int(response.json()["detail"]["accepts"][0]["maxAmountRequired"])

        assert stamp_price == 100000
        assert data_price == 50000


class TestHealthEndpointWithX402:
    """Test health endpoint x402 status integration."""

    def setup_method(self):
        from app.x402.base_balance import clear_balance_cache
        clear_balance_cache()

    def teardown_method(self):
        from app.x402.base_balance import clear_balance_cache
        clear_balance_cache()

    @patch("app.main.settings")
    def test_health_without_x402(self, mock_settings):
        mock_settings.X402_ENABLED = False
        mock_settings.PROJECT_NAME = "Test Gateway"
        from app.main import read_root
        response = read_root()
        assert response["status"] == "ok"
        assert "x402" not in response

    @patch("app.x402.preflight.check_preflight_balances")
    @patch("app.x402.base_balance.check_base_eth_balance")
    @patch("app.main.settings")
    def test_health_with_x402_healthy(self, mock_settings, mock_base_balance, mock_preflight):
        mock_settings.X402_ENABLED = True
        mock_settings.PROJECT_NAME = "Test Gateway"
        mock_base_balance.return_value = {
            "ok": True, "is_critical": False,
            "balance_wei": int(0.01 * 10**18), "balance_eth": 0.01,
            "threshold_eth": 0.005, "critical_eth": 0.001,
            "address": "0xTestAddress", "warning": None
        }
        mock_preflight.return_value = {
            "can_accept": True, "wallet_address": "0xBeeWallet123",
            "chequebook_address": "0xCheque123",
            "xbzz_ok": True, "xdai_ok": True, "chequebook_ok": True,
            "balances": {"xbzz": {}, "xdai": {}, "chequebook": {}},
            "warnings": [], "errors": []
        }
        from app.main import read_root
        response = read_root()
        assert response["status"] == "ok"
        assert response["x402"]["enabled"] is True

    @patch("app.x402.preflight.check_preflight_balances")
    @patch("app.x402.base_balance.check_base_eth_balance")
    @patch("app.main.settings")
    def test_health_with_x402_degraded(self, mock_settings, mock_base_balance, mock_preflight):
        mock_settings.X402_ENABLED = True
        mock_settings.PROJECT_NAME = "Test Gateway"
        mock_base_balance.return_value = {
            "ok": False, "is_critical": False,
            "balance_wei": int(0.003 * 10**18), "balance_eth": 0.003,
            "threshold_eth": 0.005, "critical_eth": 0.001,
            "address": "0xTestAddress",
            "warning": "Base wallet ETH (0.003) below threshold (0.005)"
        }
        mock_preflight.return_value = {
            "can_accept": True, "wallet_address": "0xBeeWallet123",
            "chequebook_address": "0xCheque123",
            "xbzz_ok": True, "xdai_ok": True, "chequebook_ok": True,
            "balances": {"xbzz": {}, "xdai": {}, "chequebook": {}},
            "warnings": [], "errors": []
        }
        from app.main import read_root
        response = read_root()
        assert response["status"] == "degraded"
        assert len(response["x402"]["warnings"]) == 1

    @patch("app.x402.preflight.check_preflight_balances")
    @patch("app.x402.base_balance.check_base_eth_balance")
    @patch("app.main.settings")
    def test_health_with_x402_critical_still_returns_200(self, mock_settings, mock_base_balance, mock_preflight):
        mock_settings.X402_ENABLED = True
        mock_settings.PROJECT_NAME = "Test Gateway"
        mock_base_balance.return_value = {
            "ok": False, "is_critical": True,
            "balance_wei": int(0.0005 * 10**18), "balance_eth": 0.0005,
            "threshold_eth": 0.005, "critical_eth": 0.001,
            "address": "0xTestAddress",
            "warning": "Base wallet ETH critically low (0.0005)"
        }
        mock_preflight.return_value = {
            "can_accept": True, "wallet_address": "0xBeeWallet123",
            "chequebook_address": "0xCheque123",
            "xbzz_ok": True, "xdai_ok": True, "chequebook_ok": True,
            "balances": {"xbzz": {}, "xdai": {}, "chequebook": {}},
            "warnings": [], "errors": []
        }
        from app.main import read_root
        response = read_root()
        assert response["status"] == "critical"
        assert len(response["x402"]["errors"]) == 1

    @patch("app.x402.preflight.check_preflight_balances")
    @patch("app.x402.base_balance.check_base_eth_balance")
    @patch("app.main.settings")
    def test_health_includes_wallet_addresses(self, mock_settings, mock_base_balance, mock_preflight):
        mock_settings.X402_ENABLED = True
        mock_settings.PROJECT_NAME = "Test Gateway"
        test_address = "0x1234567890abcdef1234567890abcdef12345678"
        mock_base_balance.return_value = {
            "ok": True, "is_critical": False,
            "balance_wei": int(0.01 * 10**18), "balance_eth": 0.01,
            "threshold_eth": 0.005, "critical_eth": 0.001,
            "address": test_address, "warning": None
        }
        mock_preflight.return_value = {
            "can_accept": True, "wallet_address": "0xBeeWallet123",
            "chequebook_address": "0xCheque123",
            "xbzz_ok": True, "xdai_ok": True, "chequebook_ok": True,
            "balances": {"xbzz": {}, "xdai": {}, "chequebook": {}},
            "warnings": [], "errors": []
        }
        from app.main import read_root
        response = read_root()
        assert response["x402"]["base_wallet"]["address"] == test_address


class TestMiddlewareWithCriticalBalance:
    """Test dependency blocks requests when balance is critical."""

    def setup_method(self):
        reset_rate_limiter()
        from app.x402.base_balance import clear_balance_cache
        clear_balance_cache()

    def teardown_method(self):
        reset_rate_limiter()
        from app.x402.base_balance import clear_balance_cache
        clear_balance_cache()

    @patch("app.x402.dependency.check_base_eth_balance")
    @patch("app.x402.dependency.get_price_quote")
    @patch("app.x402.middleware.settings")
    @patch("app.x402.dependency.settings")
    def test_critical_balance_returns_503(self, mock_dep, mock_mw, mock_price, mock_base_balance):
        _configure(mock_dep, mock_mw, free_tier=False)
        mock_base_balance.return_value = {
            "ok": False, "is_critical": True,
            "balance_wei": int(0.0005 * 10**18), "balance_eth": 0.0005,
            "threshold_eth": 0.005, "critical_eth": 0.001,
            "address": "0xpayee", "warning": "Base wallet ETH critically low"
        }
        mock_price.return_value = {"price_usd": 0.05, "description": "Test"}

        app = _make_app(("POST", "/api/v1/stamps/", create_stamp))
        client = TestClient(app)
        response = client.post("/api/v1/stamps/")

        assert response.status_code == 503
        assert "temporarily unavailable" in response.json()["detail"]["error"]
        assert response.json()["detail"]["x402_status"] == "critical"

    @patch("app.x402.dependency.check_base_eth_balance")
    @patch("app.x402.dependency.get_price_quote")
    @patch("app.x402.middleware.settings")
    @patch("app.x402.dependency.settings")
    def test_warning_balance_allows_requests(self, mock_dep, mock_mw, mock_price, mock_base_balance):
        _configure(mock_dep, mock_mw, free_tier=False)
        mock_base_balance.return_value = {
            "ok": False, "is_critical": False,
            "balance_wei": int(0.003 * 10**18), "balance_eth": 0.003,
            "threshold_eth": 0.005, "critical_eth": 0.001,
            "address": "0xpayee", "warning": "Base wallet ETH below warning threshold"
        }
        mock_price.return_value = {"price_usd": 0.05, "description": "Test"}

        app = _make_app(("POST", "/api/v1/stamps/", create_stamp))
        client = TestClient(app)
        response = client.post("/api/v1/stamps/")
        assert response.status_code == 402
