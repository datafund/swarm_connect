# tests/test_x402_middleware.py
"""
Unit tests for x402 middleware.
"""
import json
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from base64 import b64encode

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from app.x402.middleware import (
    X402Middleware,
    is_protected_endpoint,
    get_client_ip,
    create_payment_requirements,
    create_402_response,
    decode_payment_header,
    encode_payment_response,
    X402_VERSION,
    X_PAYMENT_HEADER,
    X_PAYMENT_RESPONSE_HEADER,
    USDC_ADDRESSES,
    PROTECTED_ENDPOINTS,
)


class TestIsProtectedEndpoint:
    """Test endpoint protection logic."""

    def test_post_stamps_protected(self):
        """POST /api/v1/stamps/ is protected."""
        assert is_protected_endpoint("POST", "/api/v1/stamps/") is True

    def test_post_stamps_with_id_protected(self):
        """POST /api/v1/stamps/abc123 is protected."""
        assert is_protected_endpoint("POST", "/api/v1/stamps/abc123") is True

    def test_post_data_protected(self):
        """POST /api/v1/data/ is protected."""
        assert is_protected_endpoint("POST", "/api/v1/data/") is True

    def test_post_data_manifest_protected(self):
        """POST /api/v1/data/manifest is protected."""
        assert is_protected_endpoint("POST", "/api/v1/data/manifest") is True

    def test_get_stamps_not_protected(self):
        """GET /api/v1/stamps/ is not protected."""
        assert is_protected_endpoint("GET", "/api/v1/stamps/") is False

    def test_get_data_not_protected(self):
        """GET /api/v1/data/abc123 is not protected."""
        assert is_protected_endpoint("GET", "/api/v1/data/abc123") is False

    def test_root_not_protected(self):
        """GET / is not protected."""
        assert is_protected_endpoint("GET", "/") is False

    def test_trailing_slash_handling(self):
        """Endpoints match with or without trailing slash."""
        assert is_protected_endpoint("POST", "/api/v1/stamps") is True
        assert is_protected_endpoint("POST", "/api/v1/data") is True


class TestGetClientIP:
    """Test client IP extraction."""

    def test_forwarded_for_header(self):
        """Extract IP from X-Forwarded-For header."""
        request = MagicMock(spec=Request)
        request.headers = {"X-Forwarded-For": "203.0.113.50, 70.41.3.18"}
        request.client = None

        assert get_client_ip(request) == "203.0.113.50"

    def test_real_ip_header(self):
        """Extract IP from X-Real-IP header."""
        request = MagicMock(spec=Request)
        request.headers = {"X-Real-IP": "203.0.113.50"}
        request.client = None

        assert get_client_ip(request) == "203.0.113.50"

    def test_direct_connection(self):
        """Extract IP from direct connection."""
        request = MagicMock(spec=Request)
        request.headers = {}
        request.client = MagicMock()
        request.client.host = "192.168.1.100"

        assert get_client_ip(request) == "192.168.1.100"

    def test_no_client_info(self):
        """Handle missing client info."""
        request = MagicMock(spec=Request)
        request.headers = {}
        request.client = None

        assert get_client_ip(request) == "unknown"

    def test_forwarded_for_takes_precedence(self):
        """X-Forwarded-For takes precedence over X-Real-IP."""
        request = MagicMock(spec=Request)
        request.headers = {
            "X-Forwarded-For": "203.0.113.50",
            "X-Real-IP": "10.0.0.1"
        }
        request.client = MagicMock()
        request.client.host = "192.168.1.100"

        assert get_client_ip(request) == "203.0.113.50"


class TestCreatePaymentRequirements:
    """Test payment requirements generation."""

    @patch("app.x402.middleware.settings")
    def test_create_payment_requirements(self, mock_settings):
        """Create valid PaymentRequirements object."""
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_PAY_TO_ADDRESS = "0x1234567890abcdef1234567890abcdef12345678"

        request = MagicMock(spec=Request)
        request.url = MagicMock()
        request.url.__str__ = MagicMock(return_value="https://gateway.example.com/api/v1/data/")

        result = create_payment_requirements(
            request=request,
            price_usd=0.05,
            description="Test upload"
        )

        assert result.scheme == "exact"
        assert result.network == "base-sepolia"
        assert result.max_amount_required == "50000"  # $0.05 * 1,000,000
        assert result.resource == "https://gateway.example.com/api/v1/data/"
        assert result.description == "Test upload"
        assert result.pay_to == "0x1234567890abcdef1234567890abcdef12345678"
        assert result.asset == USDC_ADDRESSES["base-sepolia"]

    @patch("app.x402.middleware.settings")
    def test_create_payment_requirements_no_address(self, mock_settings):
        """Use placeholder address when PAY_TO not configured."""
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_PAY_TO_ADDRESS = None

        request = MagicMock(spec=Request)
        request.url = MagicMock()
        request.url.__str__ = MagicMock(return_value="https://gateway.example.com/api/v1/data/")

        result = create_payment_requirements(
            request=request,
            price_usd=0.01,
            description="Test"
        )

        assert result.pay_to == "0x0000000000000000000000000000000000000000"

    @patch("app.x402.middleware.settings")
    def test_usd_to_usdc_conversion(self, mock_settings):
        """Verify USD to USDC smallest units conversion."""
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_PAY_TO_ADDRESS = "0x1234"

        request = MagicMock(spec=Request)
        request.url = MagicMock()
        request.url.__str__ = MagicMock(return_value="https://example.com")

        # $1.00 should be 1,000,000 smallest units
        result = create_payment_requirements(request, price_usd=1.0, description="Test")
        assert result.max_amount_required == "1000000"

        # $0.01 should be 10,000 smallest units
        result = create_payment_requirements(request, price_usd=0.01, description="Test")
        assert result.max_amount_required == "10000"


class TestCreate402Response:
    """Test 402 response generation."""

    @patch("app.x402.middleware.settings")
    def test_create_402_response(self, mock_settings):
        """Create valid 402 response."""
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_PAY_TO_ADDRESS = "0x1234"

        request = MagicMock(spec=Request)
        request.url = MagicMock()
        request.url.__str__ = MagicMock(return_value="https://example.com")

        payment_req = create_payment_requirements(request, 0.05, "Test")
        response = create_402_response(payment_req, "Payment required")

        assert response.status_code == 402
        assert response.headers["content-type"] == "application/json"

        body = json.loads(response.body.decode())
        assert body["x402Version"] == X402_VERSION
        assert body["error"] == "Payment required"
        assert "accepts" in body
        assert len(body["accepts"]) == 1


class TestDecodePaymentHeader:
    """Test X-PAYMENT header decoding."""

    def test_decode_valid_header(self):
        """Decode valid base64-encoded payment payload."""
        payload = {
            "x402Version": 1,
            "scheme": "exact",
            "network": "base-sepolia",
            "payload": {
                "signature": "0xabc123",
                "authorization": {
                    "from": "0xpayer",
                    "to": "0xpayee",
                    "value": "50000",
                    "validAfter": "0",
                    "validBefore": "115792089237316195423570985008687907853269984665640564039457584007913129639935",
                    "nonce": "0x123"
                }
            }
        }
        encoded = b64encode(json.dumps(payload).encode()).decode()

        result = decode_payment_header(encoded)
        assert result is not None
        assert result.x402_version == 1
        assert result.scheme == "exact"
        assert result.network == "base-sepolia"

    def test_decode_invalid_base64(self):
        """Return None for invalid base64."""
        result = decode_payment_header("not-valid-base64!!!")
        assert result is None

    def test_decode_invalid_json(self):
        """Return None for invalid JSON."""
        encoded = b64encode(b"not json").decode()
        result = decode_payment_header(encoded)
        assert result is None

    def test_decode_empty_string(self):
        """Return None for empty string."""
        result = decode_payment_header("")
        assert result is None


class TestEncodePaymentResponse:
    """Test X-PAYMENT-RESPONSE header encoding."""

    def test_encode_settle_response(self):
        """Encode settlement response to base64."""
        from x402.types import SettleResponse

        # SettleResponse uses 'transaction' not 'transaction_hash'
        settle_response = SettleResponse(
            success=True,
            transaction="0xabc123def456",
            network="base-sepolia"
        )

        encoded = encode_payment_response(settle_response)

        # Should be valid base64
        from base64 import b64decode
        decoded = json.loads(b64decode(encoded).decode())

        assert decoded["success"] is True
        assert decoded["transaction"] == "0xabc123def456"
        assert decoded["network"] == "base-sepolia"


class TestX402MiddlewareFlow:
    """Test middleware integration flow."""

    @patch("app.x402.middleware.settings")
    def test_disabled_middleware_passes_through(self, mock_settings):
        """When X402_ENABLED=false, all requests pass through."""
        mock_settings.X402_ENABLED = False

        app = FastAPI()
        app.add_middleware(X402Middleware)

        @app.post("/api/v1/data/")
        async def upload_data():
            return {"status": "uploaded"}

        client = TestClient(app)
        response = client.post("/api/v1/data/")

        assert response.status_code == 200
        assert response.json() == {"status": "uploaded"}

    @patch("app.x402.middleware.settings")
    def test_unprotected_endpoint_passes_through(self, mock_settings):
        """Unprotected endpoints pass through even when x402 enabled."""
        mock_settings.X402_ENABLED = True

        app = FastAPI()
        app.add_middleware(X402Middleware)

        @app.get("/api/v1/data/{reference}")
        async def download_data(reference: str):
            return {"reference": reference}

        client = TestClient(app)
        response = client.get("/api/v1/data/abc123")

        assert response.status_code == 200
        assert response.json() == {"reference": "abc123"}

    @patch("app.x402.middleware.settings")
    @patch("app.x402.middleware.get_price_quote")
    def test_protected_endpoint_returns_402_without_payment(self, mock_price_quote, mock_settings):
        """Protected endpoint returns 402 without X-PAYMENT header when free tier disabled."""
        mock_settings.X402_ENABLED = True
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_PAY_TO_ADDRESS = "0x1234"
        mock_settings.X402_MIN_PRICE_USD = 0.01
        mock_settings.X402_FREE_TIER_ENABLED = False  # Disable free tier to test 402

        mock_price_quote.return_value = {
            "price_usd": 0.05,
            "description": "Data upload"
        }

        app = FastAPI()
        app.add_middleware(X402Middleware)

        @app.post("/api/v1/data/")
        async def upload_data():
            return {"status": "uploaded"}

        client = TestClient(app)
        response = client.post("/api/v1/data/")

        assert response.status_code == 402
        body = response.json()
        assert body["x402Version"] == 1
        assert "X-PAYMENT header is required" in body["error"]
        assert "accepts" in body

    @patch("app.x402.middleware.settings")
    @patch("app.x402.middleware.get_price_quote")
    def test_invalid_payment_header_returns_402(self, mock_price_quote, mock_settings):
        """Invalid X-PAYMENT header returns 402."""
        mock_settings.X402_ENABLED = True
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_PAY_TO_ADDRESS = "0x1234"
        mock_settings.X402_MIN_PRICE_USD = 0.01

        mock_price_quote.return_value = {
            "price_usd": 0.05,
            "description": "Data upload"
        }

        app = FastAPI()
        app.add_middleware(X402Middleware)

        @app.post("/api/v1/data/")
        async def upload_data():
            return {"status": "uploaded"}

        client = TestClient(app)
        response = client.post(
            "/api/v1/data/",
            headers={X_PAYMENT_HEADER: "invalid-base64!!!"}
        )

        assert response.status_code == 402
        body = response.json()
        assert "Invalid X-PAYMENT header format" in body["error"]

    @patch("app.x402.middleware.settings")
    @patch("app.x402.middleware.get_price_quote")
    @patch("app.x402.middleware.FacilitatorClient")
    def test_verification_failure_returns_402(self, mock_facilitator_class, mock_price_quote, mock_settings):
        """Payment verification failure returns 402."""
        mock_settings.X402_ENABLED = True
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_PAY_TO_ADDRESS = "0x1234"
        mock_settings.X402_FACILITATOR_URL = "https://x402.org/facilitator"
        mock_settings.X402_MIN_PRICE_USD = 0.01

        mock_price_quote.return_value = {
            "price_usd": 0.05,
            "description": "Data upload"
        }

        # Mock facilitator client to return invalid verification
        from x402.types import VerifyResponse
        mock_facilitator = MagicMock()
        # VerifyResponse requires 'payer' field
        mock_facilitator.verify.return_value = VerifyResponse(
            is_valid=False,
            invalid_reason="Insufficient balance",
            payer=None  # Invalid payments may not have payer identified
        )
        mock_facilitator_class.return_value = mock_facilitator

        app = FastAPI()
        app.add_middleware(X402Middleware)

        @app.post("/api/v1/data/")
        async def upload_data():
            return {"status": "uploaded"}

        # Create valid payment payload
        payload = {
            "x402Version": 1,
            "scheme": "exact",
            "network": "base-sepolia",
            "payload": {
                "signature": "0xabc123",
                "authorization": {
                    "from": "0xpayer",
                    "to": "0xpayee",
                    "value": "50000",
                    "validAfter": "0",
                    "validBefore": "115792089237316195423570985008687907853269984665640564039457584007913129639935",
                    "nonce": "0x123"
                }
            }
        }
        encoded_payment = b64encode(json.dumps(payload).encode()).decode()

        client = TestClient(app)
        response = client.post(
            "/api/v1/data/",
            headers={X_PAYMENT_HEADER: encoded_payment}
        )

        assert response.status_code == 402
        body = response.json()
        assert "Insufficient balance" in body["error"]


class TestMiddlewarePriceCalculation:
    """Test middleware price calculation for different endpoints."""

    @patch("app.x402.middleware.settings")
    @patch("app.x402.middleware.get_price_quote")
    def test_stamps_endpoint_pricing(self, mock_price_quote, mock_settings):
        """Stamps endpoint uses stamp_purchase pricing."""
        mock_settings.X402_ENABLED = True
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_PAY_TO_ADDRESS = "0x1234"
        mock_settings.X402_MIN_PRICE_USD = 0.01
        mock_settings.X402_FREE_TIER_ENABLED = False  # Disable free tier to test 402

        mock_price_quote.return_value = {
            "price_usd": 1.50,
            "description": "Stamp purchase"
        }

        app = FastAPI()
        app.add_middleware(X402Middleware)

        @app.post("/api/v1/stamps/")
        async def purchase_stamp():
            return {"status": "purchased"}

        client = TestClient(app)
        response = client.post("/api/v1/stamps/")

        assert response.status_code == 402
        body = response.json()
        # Should have called get_price_quote with stamp_purchase operation
        mock_price_quote.assert_called()
        assert body["accepts"][0]["maxAmountRequired"] == "1500000"  # $1.50

    @patch("app.x402.middleware.settings")
    @patch("app.x402.middleware.get_price_quote")
    def test_data_endpoint_uses_content_length(self, mock_price_quote, mock_settings):
        """Data upload endpoint considers Content-Length header."""
        mock_settings.X402_ENABLED = True
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_PAY_TO_ADDRESS = "0x1234"
        mock_settings.X402_MIN_PRICE_USD = 0.01
        mock_settings.X402_FREE_TIER_ENABLED = False  # Disable free tier to test 402

        mock_price_quote.return_value = {
            "price_usd": 0.10,
            "description": "Data upload"
        }

        app = FastAPI()
        app.add_middleware(X402Middleware)

        @app.post("/api/v1/data/")
        async def upload_data():
            return {"status": "uploaded"}

        client = TestClient(app)
        response = client.post(
            "/api/v1/data/",
            headers={"Content-Length": "10240"}  # 10 KB
        )

        assert response.status_code == 402
        # Price quote should have been called
        mock_price_quote.assert_called()


class TestUSDCAddresses:
    """Test USDC contract addresses."""

    def test_base_mainnet_address(self):
        """Base mainnet USDC address is correct."""
        assert USDC_ADDRESSES["base"] == "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

    def test_base_sepolia_address(self):
        """Base Sepolia USDC address is correct."""
        assert USDC_ADDRESSES["base-sepolia"] == "0x036CbD53842c5426634e7929541eC2318f3dCF7e"


class TestProtectedEndpoints:
    """Test protected endpoints configuration."""

    def test_protected_endpoints_list(self):
        """Verify protected endpoints are configured correctly."""
        expected = [
            ("POST", "/api/v1/stamps/"),
            ("POST", "/api/v1/data/"),
            ("POST", "/api/v1/data/manifest"),
        ]
        assert PROTECTED_ENDPOINTS == expected

    def test_get_endpoints_not_protected(self):
        """GET methods should not be in protected list."""
        for method, path in PROTECTED_ENDPOINTS:
            assert method != "GET"


class TestFreeTierAccess:
    """Test free tier access for users without x402 payment capability."""

    def setup_method(self):
        """Reset rate limiter before each test."""
        from app.x402.ratelimit import reset_rate_limiter
        reset_rate_limiter()

    def teardown_method(self):
        """Reset rate limiter after each test."""
        from app.x402.ratelimit import reset_rate_limiter
        reset_rate_limiter()

    @patch("app.x402.middleware.settings")
    @patch("app.x402.middleware.get_price_quote")
    def test_free_tier_allows_access_without_payment(self, mock_price_quote, mock_settings):
        """Free tier enabled allows access without X-PAYMENT header."""
        mock_settings.X402_ENABLED = True
        mock_settings.X402_FREE_TIER_ENABLED = True
        mock_settings.X402_FREE_TIER_RATE_LIMIT = 3
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_PAY_TO_ADDRESS = "0x1234"
        mock_settings.X402_MIN_PRICE_USD = 0.01

        mock_price_quote.return_value = {
            "price_usd": 0.05,
            "description": "Data upload"
        }

        app = FastAPI()
        app.add_middleware(X402Middleware)

        @app.post("/api/v1/data/")
        async def upload_data():
            return {"status": "uploaded"}

        client = TestClient(app)
        response = client.post("/api/v1/data/")

        # Should succeed with 200, not 402
        assert response.status_code == 200
        assert response.json() == {"status": "uploaded"}
        # Should have free tier header
        assert response.headers.get("X-Payment-Mode") == "free-tier"
        # Should have rate limit headers
        assert "X-RateLimit-Limit" in response.headers
        assert "X-RateLimit-Remaining" in response.headers

    @patch("app.x402.ratelimit.settings")
    @patch("app.x402.middleware.settings")
    @patch("app.x402.middleware.get_price_quote")
    def test_free_tier_rate_limit_enforced(self, mock_price_quote, mock_middleware_settings, mock_ratelimit_settings):
        """Free tier enforces stricter rate limit."""
        # Configure middleware settings
        mock_middleware_settings.X402_ENABLED = True
        mock_middleware_settings.X402_FREE_TIER_ENABLED = True
        mock_middleware_settings.X402_FREE_TIER_RATE_LIMIT = 2
        mock_middleware_settings.X402_NETWORK = "base-sepolia"
        mock_middleware_settings.X402_PAY_TO_ADDRESS = "0x1234"
        mock_middleware_settings.X402_MIN_PRICE_USD = 0.01

        # Configure ratelimit settings
        mock_ratelimit_settings.X402_FREE_TIER_RATE_LIMIT = 2  # Only 2 requests per minute
        mock_ratelimit_settings.X402_RATE_LIMIT_PER_IP = 10  # Paid tier would allow 10

        mock_price_quote.return_value = {
            "price_usd": 0.05,
            "description": "Data upload"
        }

        app = FastAPI()
        app.add_middleware(X402Middleware)

        @app.post("/api/v1/data/")
        async def upload_data():
            return {"status": "uploaded"}

        client = TestClient(app)

        # First 2 requests should succeed
        for i in range(2):
            response = client.post("/api/v1/data/")
            assert response.status_code == 200, f"Request {i+1} should succeed"

        # 3rd request should be rate limited
        response = client.post("/api/v1/data/")
        assert response.status_code == 429
        body = response.json()
        assert body["error"] == "Rate limit exceeded"
        assert "free tier" in body["detail"].lower()
        # Should include payment info for upgrade
        assert "payment_info" in body
        assert body["payment_info"]["price_usd"] == 0.05

    @patch("app.x402.middleware.settings")
    @patch("app.x402.middleware.get_price_quote")
    def test_free_tier_disabled_returns_402(self, mock_price_quote, mock_settings):
        """When free tier is disabled, requests without payment get 402."""
        mock_settings.X402_ENABLED = True
        mock_settings.X402_FREE_TIER_ENABLED = False  # Disable free tier
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_PAY_TO_ADDRESS = "0x1234"
        mock_settings.X402_MIN_PRICE_USD = 0.01

        mock_price_quote.return_value = {
            "price_usd": 0.05,
            "description": "Data upload"
        }

        app = FastAPI()
        app.add_middleware(X402Middleware)

        @app.post("/api/v1/data/")
        async def upload_data():
            return {"status": "uploaded"}

        client = TestClient(app)
        response = client.post("/api/v1/data/")

        # Should return 402 when free tier disabled
        assert response.status_code == 402

    @patch("app.x402.ratelimit.settings")
    @patch("app.x402.middleware.settings")
    @patch("app.x402.middleware.get_price_quote")
    def test_free_tier_rate_limit_headers_present(self, mock_price_quote, mock_middleware_settings, mock_ratelimit_settings):
        """Free tier responses include rate limit headers."""
        mock_middleware_settings.X402_ENABLED = True
        mock_middleware_settings.X402_FREE_TIER_ENABLED = True
        mock_middleware_settings.X402_FREE_TIER_RATE_LIMIT = 5
        mock_middleware_settings.X402_NETWORK = "base-sepolia"
        mock_middleware_settings.X402_PAY_TO_ADDRESS = "0x1234"
        mock_middleware_settings.X402_MIN_PRICE_USD = 0.01

        mock_ratelimit_settings.X402_FREE_TIER_RATE_LIMIT = 5
        mock_ratelimit_settings.X402_RATE_LIMIT_PER_IP = 10

        mock_price_quote.return_value = {
            "price_usd": 0.05,
            "description": "Data upload"
        }

        app = FastAPI()
        app.add_middleware(X402Middleware)

        @app.post("/api/v1/data/")
        async def upload_data():
            return {"status": "uploaded"}

        client = TestClient(app)
        response = client.post("/api/v1/data/")

        assert response.status_code == 200
        assert response.headers["X-RateLimit-Limit"] == "5"
        assert response.headers["X-RateLimit-Remaining"] == "4"  # 5 - 1 = 4
        assert response.headers["X-Payment-Mode"] == "free-tier"

    @patch("app.x402.ratelimit.settings")
    @patch("app.x402.middleware.settings")
    @patch("app.x402.middleware.get_price_quote")
    def test_free_tier_429_includes_upgrade_info(self, mock_price_quote, mock_middleware_settings, mock_ratelimit_settings):
        """Rate limit exceeded response includes info on how to upgrade."""
        mock_middleware_settings.X402_ENABLED = True
        mock_middleware_settings.X402_FREE_TIER_ENABLED = True
        mock_middleware_settings.X402_FREE_TIER_RATE_LIMIT = 1
        mock_middleware_settings.X402_NETWORK = "base-sepolia"
        mock_middleware_settings.X402_PAY_TO_ADDRESS = "0xPaymentWallet"
        mock_middleware_settings.X402_MIN_PRICE_USD = 0.01

        mock_ratelimit_settings.X402_FREE_TIER_RATE_LIMIT = 1
        mock_ratelimit_settings.X402_RATE_LIMIT_PER_IP = 10

        mock_price_quote.return_value = {
            "price_usd": 0.10,
            "description": "Stamp purchase"
        }

        app = FastAPI()
        app.add_middleware(X402Middleware)

        @app.post("/api/v1/stamps/")
        async def purchase_stamp():
            return {"status": "purchased"}

        client = TestClient(app)

        # First request succeeds
        response = client.post("/api/v1/stamps/")
        assert response.status_code == 200

        # Second request hits rate limit
        response = client.post("/api/v1/stamps/")
        assert response.status_code == 429

        body = response.json()
        assert "payment_info" in body
        assert body["payment_info"]["network"] == "base-sepolia"
        assert body["payment_info"]["pay_to"] == "0xPaymentWallet"
        assert body["payment_info"]["price_usd"] == 0.10
        assert "Use x402 payment for higher limits" in body["message"]

    @patch("app.x402.middleware.settings")
    @patch("app.x402.middleware.get_price_quote")
    def test_different_endpoints_share_free_tier_limit(self, mock_price_quote, mock_settings):
        """Free tier rate limit is shared across all protected endpoints."""
        mock_settings.X402_ENABLED = True
        mock_settings.X402_FREE_TIER_ENABLED = True
        mock_settings.X402_FREE_TIER_RATE_LIMIT = 3
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_PAY_TO_ADDRESS = "0x1234"
        mock_settings.X402_MIN_PRICE_USD = 0.01

        mock_price_quote.return_value = {
            "price_usd": 0.05,
            "description": "Operation"
        }

        app = FastAPI()
        app.add_middleware(X402Middleware)

        @app.post("/api/v1/stamps/")
        async def purchase_stamp():
            return {"status": "purchased"}

        @app.post("/api/v1/data/")
        async def upload_data():
            return {"status": "uploaded"}

        client = TestClient(app)

        # Mix of endpoints should share the rate limit
        assert client.post("/api/v1/stamps/").status_code == 200
        assert client.post("/api/v1/data/").status_code == 200
        assert client.post("/api/v1/stamps/").status_code == 200

        # 4th request should be rate limited
        response = client.post("/api/v1/data/")
        assert response.status_code == 429
