# tests/test_x402_integration.py
"""
Integration tests for x402 payment gateway.

These tests verify the full x402 flow including:
- Middleware integration with FastAPI
- Access control (whitelist/blacklist)
- Rate limiting
- Audit logging
- 402 response generation
- Payment verification and settlement

All tests use mocked facilitator responses to avoid requiring
real blockchain transactions or testnet setup.
"""
import json
import os
import pytest
import tempfile
from unittest.mock import patch, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from x402.types import PaymentRequirements, PaymentPayload, VerifyResponse, SettleResponse
from x402.encoding import safe_base64_encode

from app.x402.middleware import X402Middleware, PROTECTED_ENDPOINTS
from app.x402.access import check_access
from app.x402.ratelimit import reset_rate_limiter
from app.x402.audit import (
    get_audit_log_path,
    read_audit_log,
    AuditEventType,
)


# Create a test FastAPI app with the x402 middleware
def create_test_app(x402_enabled: bool = True) -> FastAPI:
    """Create a test FastAPI app with x402 middleware."""
    app = FastAPI()

    # Add test endpoints matching protected patterns
    @app.post("/api/v1/stamps/")
    async def create_stamp():
        return {"stamp_id": "test-stamp-123", "status": "created"}

    @app.post("/api/v1/data/")
    async def upload_data():
        return {"reference": "abc123def456", "status": "uploaded"}

    @app.post("/api/v1/data/manifest")
    async def upload_manifest():
        return {"reference": "manifest789", "status": "uploaded"}

    @app.get("/api/v1/health")
    async def health():
        return {"status": "healthy"}

    # Add middleware
    mock_client = MagicMock()
    app.add_middleware(X402Middleware, facilitator_client=mock_client)

    return app


def create_valid_payment_header(
    payer: str = "0x1234567890abcdef1234567890abcdef12345678",
    amount: str = "100000",  # 0.1 USDC
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
                "validAfter": "0",  # SDK expects strings
                "validBefore": "9999999999",  # SDK expects strings
                "nonce": "0x" + "00" * 32,
            }
        }
    }
    return safe_base64_encode(json.dumps(payload).encode("utf-8"))


class TestMiddlewareIntegration:
    """Test x402 middleware with full FastAPI integration."""

    def setup_method(self):
        """Reset rate limiter before each test."""
        reset_rate_limiter()

    def teardown_method(self):
        """Reset rate limiter after each test."""
        reset_rate_limiter()

    @patch("app.x402.middleware.settings")
    def test_x402_disabled_passes_through(self, mock_settings):
        """When x402 is disabled, requests pass through without payment."""
        mock_settings.X402_ENABLED = False
        mock_settings.X402_FACILITATOR_URL = "https://x402.org/facilitator"
        mock_settings.X402_PAY_TO_ADDRESS = "0xpayee"
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_BZZ_USD_RATE = 0.50
        mock_settings.X402_MARKUP_PERCENT = 50.0
        mock_settings.X402_MIN_PRICE_USD = 0.01

        app = create_test_app(x402_enabled=False)
        client = TestClient(app)

        response = client.post("/api/v1/stamps/")
        assert response.status_code == 200
        assert response.json()["status"] == "created"

    @patch("app.x402.middleware.settings")
    def test_unprotected_endpoint_passes_through(self, mock_settings):
        """Unprotected endpoints pass through even when x402 is enabled."""
        mock_settings.X402_ENABLED = True
        mock_settings.X402_FACILITATOR_URL = "https://x402.org/facilitator"
        mock_settings.X402_PAY_TO_ADDRESS = "0xpayee"
        mock_settings.X402_NETWORK = "base-sepolia"

        app = create_test_app(x402_enabled=True)
        client = TestClient(app)

        response = client.get("/api/v1/health")
        assert response.status_code == 200
        assert response.json()["status"] == "healthy"

    @patch("app.x402.middleware.settings")
    @patch("app.x402.middleware.get_price_quote")
    def test_protected_endpoint_returns_402_without_payment(
        self, mock_price_quote, mock_settings
    ):
        """Protected endpoint returns 402 without X-PAYMENT header."""
        mock_settings.X402_ENABLED = True
        mock_settings.X402_FACILITATOR_URL = "https://x402.org/facilitator"
        mock_settings.X402_PAY_TO_ADDRESS = "0xpayee"
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_MIN_PRICE_USD = 0.01

        mock_price_quote.return_value = {
            "price_usd": 0.05,
            "description": "Test operation"
        }

        app = create_test_app(x402_enabled=True)
        client = TestClient(app)

        response = client.post("/api/v1/stamps/")

        assert response.status_code == 402
        data = response.json()
        assert "x402Version" in data
        assert data["x402Version"] == 1
        assert "accepts" in data
        assert len(data["accepts"]) > 0

    @patch("app.x402.middleware.settings")
    @patch("app.x402.middleware.get_price_quote")
    def test_402_response_contains_payment_requirements(
        self, mock_price_quote, mock_settings
    ):
        """402 response contains proper payment requirements."""
        mock_settings.X402_ENABLED = True
        mock_settings.X402_FACILITATOR_URL = "https://x402.org/facilitator"
        mock_settings.X402_PAY_TO_ADDRESS = "0xTestPayee"
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_MIN_PRICE_USD = 0.01

        mock_price_quote.return_value = {
            "price_usd": 0.10,
            "description": "Stamp purchase"
        }

        app = create_test_app(x402_enabled=True)
        client = TestClient(app)

        response = client.post("/api/v1/stamps/")

        assert response.status_code == 402
        data = response.json()

        requirements = data["accepts"][0]
        assert requirements["scheme"] == "exact"
        assert requirements["network"] == "base-sepolia"
        assert requirements["payTo"] == "0xTestPayee"
        assert int(requirements["maxAmountRequired"]) == 100000  # 0.10 USD = 100000 USDC units

    @patch("app.x402.middleware.settings")
    @patch("app.x402.middleware.get_price_quote")
    def test_invalid_payment_header_returns_402(
        self, mock_price_quote, mock_settings
    ):
        """Invalid X-PAYMENT header returns 402."""
        mock_settings.X402_ENABLED = True
        mock_settings.X402_FACILITATOR_URL = "https://x402.org/facilitator"
        mock_settings.X402_PAY_TO_ADDRESS = "0xpayee"
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_MIN_PRICE_USD = 0.01

        mock_price_quote.return_value = {
            "price_usd": 0.05,
            "description": "Test operation"
        }

        app = create_test_app(x402_enabled=True)
        client = TestClient(app)

        response = client.post(
            "/api/v1/stamps/",
            headers={"X-PAYMENT": "invalid-base64-data!!!"}
        )

        assert response.status_code == 402
        assert "Invalid X-PAYMENT header" in response.json()["error"]


class TestAccessControlIntegration:
    """Test access control integration with the gateway."""

    @patch("app.x402.access.settings")
    def test_blacklisted_ip_blocked(self, mock_settings):
        """Blacklisted IP is blocked."""
        mock_settings.X402_BLACKLIST_IPS = "192.168.1.100"
        mock_settings.X402_WHITELIST_IPS = ""

        status, reason = check_access("192.168.1.100")

        assert status == "blocked"
        assert reason == "IP address is blocked"

    @patch("app.x402.access.settings")
    def test_whitelisted_ip_free_access(self, mock_settings):
        """Whitelisted IP gets free access."""
        mock_settings.X402_BLACKLIST_IPS = ""
        mock_settings.X402_WHITELIST_IPS = "192.168.1.50"

        status, reason = check_access("192.168.1.50")

        assert status == "free"
        assert reason is None

    @patch("app.x402.access.settings")
    def test_normal_ip_requires_payment(self, mock_settings):
        """Normal IP requires payment."""
        mock_settings.X402_BLACKLIST_IPS = ""
        mock_settings.X402_WHITELIST_IPS = ""

        status, reason = check_access("192.168.1.1")

        assert status == "pay"
        assert reason is None

    @patch("app.x402.access.settings")
    def test_cidr_range_blocking(self, mock_settings):
        """CIDR range blocking works."""
        mock_settings.X402_BLACKLIST_IPS = "10.0.0.0/8"
        mock_settings.X402_WHITELIST_IPS = ""

        # Should be blocked
        status, _ = check_access("10.50.100.200")
        assert status == "blocked"

        # Should not be blocked
        status, _ = check_access("192.168.1.1")
        assert status == "pay"


class TestRateLimitingIntegration:
    """Test rate limiting integration."""

    def setup_method(self):
        """Reset rate limiter before each test."""
        reset_rate_limiter()

    def teardown_method(self):
        """Reset rate limiter after each test."""
        reset_rate_limiter()

    @patch("app.x402.ratelimit.settings")
    def test_rate_limit_blocks_after_threshold(self, mock_settings):
        """Rate limit blocks requests after threshold."""
        from app.x402.ratelimit import check_rate_limit

        mock_settings.X402_RATE_LIMIT_PER_IP = 3

        # First 3 requests should pass
        for i in range(3):
            is_allowed, reason, stats = check_rate_limit("192.168.1.1")
            assert is_allowed is True

        # 4th request should be blocked
        is_allowed, reason, stats = check_rate_limit("192.168.1.1")
        assert is_allowed is False
        assert "Rate limit exceeded" in reason

    @patch("app.x402.ratelimit.settings")
    def test_different_ips_have_separate_limits(self, mock_settings):
        """Different IPs have separate rate limits."""
        from app.x402.ratelimit import check_rate_limit

        mock_settings.X402_RATE_LIMIT_PER_IP = 2

        # Max out first IP
        check_rate_limit("192.168.1.1")
        check_rate_limit("192.168.1.1")
        is_allowed, _, _ = check_rate_limit("192.168.1.1")
        assert is_allowed is False

        # Second IP should still be allowed
        is_allowed, _, _ = check_rate_limit("192.168.1.2")
        assert is_allowed is True


class TestAuditLoggingIntegration:
    """Test audit logging integration."""

    def setup_method(self):
        """Create temporary audit log directory."""
        self.temp_dir = tempfile.mkdtemp()
        self.audit_log_path = os.path.join(self.temp_dir, "x402_audit.jsonl")

    def teardown_method(self):
        """Clean up temporary files."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch("app.x402.audit.settings")
    def test_audit_event_logged(self, mock_settings):
        """Audit events are logged correctly."""
        from app.x402.audit import log_audit_event

        mock_settings.X402_AUDIT_LOG_PATH = self.audit_log_path

        request_id = log_audit_event(
            event_type=AuditEventType.REQUEST_RECEIVED,
            data={"method": "POST", "path": "/api/v1/stamps/"},
            client_ip="192.168.1.1"
        )

        assert request_id is not None

        # Verify log file contents
        with open(self.audit_log_path, "r") as f:
            line = f.readline()
            event = json.loads(line)

        assert event["event_type"] == "request_received"
        assert event["client_ip"] == "192.168.1.1"
        assert event["data"]["method"] == "POST"

    @patch("app.x402.audit.settings")
    def test_audit_log_reading(self, mock_settings):
        """Audit log reading works correctly."""
        from app.x402.audit import log_audit_event

        mock_settings.X402_AUDIT_LOG_PATH = self.audit_log_path

        # Log multiple events
        log_audit_event(
            event_type=AuditEventType.REQUEST_RECEIVED,
            data={"method": "POST"},
            client_ip="192.168.1.1"
        )
        log_audit_event(
            event_type=AuditEventType.PAYMENT_RECEIVED,
            data={"amount": "100000"},
            client_ip="192.168.1.1"
        )

        # Read back
        events = read_audit_log(max_entries=10)

        assert len(events) == 2
        # Most recent first
        assert events[0]["event_type"] == "payment_received"
        assert events[1]["event_type"] == "request_received"

    @patch("app.x402.audit.settings")
    def test_audit_log_filtering(self, mock_settings):
        """Audit log filtering works correctly."""
        from app.x402.audit import log_audit_event

        mock_settings.X402_AUDIT_LOG_PATH = self.audit_log_path

        # Log events from different IPs
        log_audit_event(
            event_type=AuditEventType.REQUEST_RECEIVED,
            data={},
            client_ip="192.168.1.1"
        )
        log_audit_event(
            event_type=AuditEventType.REQUEST_RECEIVED,
            data={},
            client_ip="192.168.1.2"
        )

        # Filter by IP
        events = read_audit_log(client_ip="192.168.1.1")

        assert len(events) == 1
        assert events[0]["client_ip"] == "192.168.1.1"


class TestFullPaymentFlow:
    """Test full payment flow with mocked facilitator."""

    def setup_method(self):
        """Reset rate limiter before each test."""
        reset_rate_limiter()

    def teardown_method(self):
        """Reset rate limiter after each test."""
        reset_rate_limiter()

    @patch("app.x402.middleware.settings")
    @patch("app.x402.middleware.get_price_quote")
    def test_successful_payment_flow(
        self, mock_price_quote, mock_settings
    ):
        """Test successful payment flow from start to finish."""
        mock_settings.X402_ENABLED = True
        mock_settings.X402_FACILITATOR_URL = "https://x402.org/facilitator"
        mock_settings.X402_PAY_TO_ADDRESS = "0xpayee"
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_MIN_PRICE_USD = 0.01

        mock_price_quote.return_value = {
            "price_usd": 0.05,
            "description": "Test stamp"
        }

        # Create app with mocked facilitator
        mock_facilitator = MagicMock()
        mock_facilitator.verify.return_value = VerifyResponse(
            is_valid=True,
            invalid_reason=None,
            payer="0x1234567890abcdef1234567890abcdef12345678"
        )
        mock_facilitator.settle.return_value = SettleResponse(
            success=True,
            transaction="0x" + "ab" * 32,
            network="base-sepolia"
        )

        app = FastAPI()

        @app.post("/api/v1/stamps/")
        async def create_stamp():
            return {"stamp_id": "test-stamp-123", "status": "created"}

        app.add_middleware(X402Middleware, facilitator_client=mock_facilitator)

        client = TestClient(app)

        # Make request with valid payment
        payment_header = create_valid_payment_header()
        response = client.post(
            "/api/v1/stamps/",
            headers={"X-PAYMENT": payment_header}
        )

        # Should succeed
        assert response.status_code == 200
        assert response.json()["status"] == "created"

        # Should have X-PAYMENT-RESPONSE header
        assert "X-PAYMENT-RESPONSE" in response.headers

        # Verify facilitator was called
        mock_facilitator.verify.assert_called_once()
        mock_facilitator.settle.assert_called_once()

    @patch("app.x402.middleware.settings")
    @patch("app.x402.middleware.get_price_quote")
    def test_payment_verification_failure(
        self, mock_price_quote, mock_settings
    ):
        """Test payment verification failure returns 402."""
        mock_settings.X402_ENABLED = True
        mock_settings.X402_FACILITATOR_URL = "https://x402.org/facilitator"
        mock_settings.X402_PAY_TO_ADDRESS = "0xpayee"
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_MIN_PRICE_USD = 0.01

        mock_price_quote.return_value = {
            "price_usd": 0.05,
            "description": "Test stamp"
        }

        # Create app with mocked facilitator that rejects payment
        mock_facilitator = MagicMock()
        mock_facilitator.verify.return_value = VerifyResponse(
            is_valid=False,
            invalid_reason="Insufficient balance",
            payer=None
        )

        app = FastAPI()

        @app.post("/api/v1/stamps/")
        async def create_stamp():
            return {"stamp_id": "test-stamp-123", "status": "created"}

        app.add_middleware(X402Middleware, facilitator_client=mock_facilitator)

        client = TestClient(app)

        # Make request with payment that will fail verification
        payment_header = create_valid_payment_header()
        response = client.post(
            "/api/v1/stamps/",
            headers={"X-PAYMENT": payment_header}
        )

        # Should return 402 with error
        assert response.status_code == 402
        assert "Insufficient balance" in response.json()["error"]


class TestProtectedEndpoints:
    """Test all protected endpoints are properly gated."""

    def setup_method(self):
        """Reset rate limiter before each test."""
        reset_rate_limiter()

    def teardown_method(self):
        """Reset rate limiter after each test."""
        reset_rate_limiter()

    @patch("app.x402.middleware.settings")
    @patch("app.x402.middleware.get_price_quote")
    def test_stamps_endpoint_protected(self, mock_price_quote, mock_settings):
        """POST /api/v1/stamps/ is protected."""
        mock_settings.X402_ENABLED = True
        mock_settings.X402_FACILITATOR_URL = "https://x402.org/facilitator"
        mock_settings.X402_PAY_TO_ADDRESS = "0xpayee"
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_MIN_PRICE_USD = 0.01

        mock_price_quote.return_value = {"price_usd": 0.05, "description": "Test"}

        app = create_test_app(x402_enabled=True)
        client = TestClient(app)

        response = client.post("/api/v1/stamps/")
        assert response.status_code == 402

    @patch("app.x402.middleware.settings")
    @patch("app.x402.middleware.get_price_quote")
    def test_data_endpoint_protected(self, mock_price_quote, mock_settings):
        """POST /api/v1/data/ is protected."""
        mock_settings.X402_ENABLED = True
        mock_settings.X402_FACILITATOR_URL = "https://x402.org/facilitator"
        mock_settings.X402_PAY_TO_ADDRESS = "0xpayee"
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_MIN_PRICE_USD = 0.01

        mock_price_quote.return_value = {"price_usd": 0.05, "description": "Test"}

        app = create_test_app(x402_enabled=True)
        client = TestClient(app)

        response = client.post("/api/v1/data/")
        assert response.status_code == 402

    @patch("app.x402.middleware.settings")
    @patch("app.x402.middleware.get_price_quote")
    def test_manifest_endpoint_protected(self, mock_price_quote, mock_settings):
        """POST /api/v1/data/manifest is protected."""
        mock_settings.X402_ENABLED = True
        mock_settings.X402_FACILITATOR_URL = "https://x402.org/facilitator"
        mock_settings.X402_PAY_TO_ADDRESS = "0xpayee"
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_MIN_PRICE_USD = 0.01

        mock_price_quote.return_value = {"price_usd": 0.05, "description": "Test"}

        app = create_test_app(x402_enabled=True)
        client = TestClient(app)

        response = client.post("/api/v1/data/manifest")
        assert response.status_code == 402


class TestEndToEndScenarios:
    """Test complete end-to-end scenarios."""

    def setup_method(self):
        """Reset rate limiter before each test."""
        reset_rate_limiter()

    def teardown_method(self):
        """Reset rate limiter after each test."""
        reset_rate_limiter()

    @patch("app.x402.middleware.settings")
    @patch("app.x402.middleware.get_price_quote")
    def test_gateway_operator_workflow(self, mock_price_quote, mock_settings):
        """
        Simulate a gateway operator workflow:
        1. Start with x402 disabled
        2. Enable x402
        3. Requests without payment get 402
        4. Requests with valid payment succeed
        """
        # Step 1: x402 disabled
        mock_settings.X402_ENABLED = False
        mock_settings.X402_FACILITATOR_URL = "https://x402.org/facilitator"
        mock_settings.X402_PAY_TO_ADDRESS = "0xpayee"
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_MIN_PRICE_USD = 0.01
        mock_settings.X402_BZZ_USD_RATE = 0.50
        mock_settings.X402_MARKUP_PERCENT = 50.0

        app = create_test_app(x402_enabled=False)
        client = TestClient(app)

        # Requests pass through
        response = client.post("/api/v1/stamps/")
        assert response.status_code == 200

        # Step 2: Enable x402
        mock_settings.X402_ENABLED = True
        mock_price_quote.return_value = {"price_usd": 0.05, "description": "Test"}

        # Need to recreate app to pick up new setting
        mock_facilitator = MagicMock()
        mock_facilitator.verify.return_value = VerifyResponse(
            is_valid=True,
            invalid_reason=None,
            payer="0x1234567890abcdef1234567890abcdef12345678"
        )
        mock_facilitator.settle.return_value = SettleResponse(
            success=True,
            transaction="0x" + "ab" * 32,
            network="base-sepolia"
        )

        app2 = FastAPI()

        @app2.post("/api/v1/stamps/")
        async def create_stamp():
            return {"stamp_id": "test-stamp-123", "status": "created"}

        app2.add_middleware(X402Middleware, facilitator_client=mock_facilitator)
        client2 = TestClient(app2)

        # Step 3: Without payment, get 402
        response = client2.post("/api/v1/stamps/")
        assert response.status_code == 402

        # Step 4: With payment, succeed
        payment_header = create_valid_payment_header()
        response = client2.post(
            "/api/v1/stamps/",
            headers={"X-PAYMENT": payment_header}
        )
        assert response.status_code == 200

    @patch("app.x402.middleware.settings")
    @patch("app.x402.middleware.get_price_quote")
    def test_price_varies_by_endpoint(self, mock_price_quote, mock_settings):
        """Different endpoints can have different prices."""
        mock_settings.X402_ENABLED = True
        mock_settings.X402_FACILITATOR_URL = "https://x402.org/facilitator"
        mock_settings.X402_PAY_TO_ADDRESS = "0xpayee"
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_MIN_PRICE_USD = 0.01

        app = create_test_app(x402_enabled=True)
        client = TestClient(app)

        # Stamps endpoint price
        mock_price_quote.return_value = {"price_usd": 0.10, "description": "Stamp"}
        response = client.post("/api/v1/stamps/")
        stamp_price = int(response.json()["accepts"][0]["maxAmountRequired"])

        # Data endpoint price
        mock_price_quote.return_value = {"price_usd": 0.05, "description": "Upload"}
        response = client.post("/api/v1/data/")
        data_price = int(response.json()["accepts"][0]["maxAmountRequired"])

        # Prices should be different
        assert stamp_price == 100000  # 0.10 USD
        assert data_price == 50000    # 0.05 USD
