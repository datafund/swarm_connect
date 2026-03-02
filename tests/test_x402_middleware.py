# tests/test_x402_middleware.py
"""
Unit tests for x402 middleware and dependency.

Tests cover:
- Helper functions in middleware.py (is_protected_endpoint, get_client_ip, etc.)
- The x402 dependency (pre-request payment verification)
- The simplified middleware (post-response headers and settlement)
- Free tier access and rate limiting
"""
import json
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from base64 import b64encode

from fastapi import FastAPI, Request, Depends, APIRouter
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
    USDC_TOKEN_METADATA,
    PROTECTED_ENDPOINTS,
)
from app.x402.dependency import require_x402_payment


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


def _make_app(*routes):
    """Create a FastAPI app with x402 dependency and middleware.

    Each route is a tuple of (method, path, handler).
    """
    app = FastAPI()
    router = APIRouter(dependencies=[Depends(require_x402_payment)])

    for method, path, handler in routes:
        router.add_api_route(path, handler, methods=[method])

    app.include_router(router)
    app.add_middleware(X402Middleware)
    return app


def _configure_dep(mock_dep, mock_mw, *, free_tier=False, balance=None):
    """Set common mock values on both dependency and middleware settings mocks."""
    mock_dep.X402_ENABLED = True
    mock_dep.X402_NETWORK = "base-sepolia"
    mock_dep.X402_PAY_TO_ADDRESS = "0x1234"
    mock_dep.X402_MIN_PRICE_USD = 0.01
    mock_dep.X402_FREE_TIER_ENABLED = free_tier
    mock_dep.X402_FREE_TIER_RATE_LIMIT = 3

    mock_mw.X402_ENABLED = True
    mock_mw.X402_NETWORK = "base-sepolia"
    mock_mw.X402_PAY_TO_ADDRESS = "0x1234"


class TestIsProtectedEndpoint:
    """Test endpoint protection logic."""

    def test_post_stamps_protected(self):
        assert is_protected_endpoint("POST", "/api/v1/stamps/") is True

    def test_post_stamps_with_id_protected(self):
        assert is_protected_endpoint("POST", "/api/v1/stamps/abc123") is True

    def test_post_data_protected(self):
        assert is_protected_endpoint("POST", "/api/v1/data/") is True

    def test_post_data_manifest_protected(self):
        assert is_protected_endpoint("POST", "/api/v1/data/manifest") is True

    def test_get_stamps_not_protected(self):
        assert is_protected_endpoint("GET", "/api/v1/stamps/") is False

    def test_get_data_not_protected(self):
        assert is_protected_endpoint("GET", "/api/v1/data/abc123") is False

    def test_root_not_protected(self):
        assert is_protected_endpoint("GET", "/") is False

    def test_trailing_slash_handling(self):
        assert is_protected_endpoint("POST", "/api/v1/stamps") is True
        assert is_protected_endpoint("POST", "/api/v1/data") is True


class TestGetClientIP:
    """Test client IP extraction."""

    def test_forwarded_for_header(self):
        request = MagicMock(spec=Request)
        request.headers = {"X-Forwarded-For": "203.0.113.50, 70.41.3.18"}
        request.client = None
        assert get_client_ip(request) == "203.0.113.50"

    def test_real_ip_header(self):
        request = MagicMock(spec=Request)
        request.headers = {"X-Real-IP": "203.0.113.50"}
        request.client = None
        assert get_client_ip(request) == "203.0.113.50"

    def test_direct_connection(self):
        request = MagicMock(spec=Request)
        request.headers = {}
        request.client = MagicMock()
        request.client.host = "192.168.1.100"
        assert get_client_ip(request) == "192.168.1.100"

    def test_no_client_info(self):
        request = MagicMock(spec=Request)
        request.headers = {}
        request.client = None
        assert get_client_ip(request) == "unknown"

    def test_forwarded_for_takes_precedence(self):
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
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_PAY_TO_ADDRESS = "0x1234567890abcdef1234567890abcdef12345678"

        request = MagicMock(spec=Request)
        request.url = MagicMock()
        request.url.__str__ = MagicMock(return_value="https://gateway.example.com/api/v1/data/")

        result = create_payment_requirements(request=request, price_usd=0.05, description="Test upload")

        assert result.scheme == "exact"
        assert result.network == "base-sepolia"
        assert result.max_amount_required == "50000"
        assert result.resource == "https://gateway.example.com/api/v1/data/"
        assert result.description == "Test upload"
        assert result.pay_to == "0x1234567890abcdef1234567890abcdef12345678"
        assert result.asset == USDC_ADDRESSES["base-sepolia"]

    @patch("app.x402.middleware.settings")
    def test_create_payment_requirements_no_address(self, mock_settings):
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_PAY_TO_ADDRESS = None

        request = MagicMock(spec=Request)
        request.url = MagicMock()
        request.url.__str__ = MagicMock(return_value="https://gateway.example.com/api/v1/data/")

        result = create_payment_requirements(request=request, price_usd=0.01, description="Test")
        assert result.pay_to == "0x0000000000000000000000000000000000000000"

    @patch("app.x402.middleware.settings")
    def test_usd_to_usdc_conversion(self, mock_settings):
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_PAY_TO_ADDRESS = "0x1234"

        request = MagicMock(spec=Request)
        request.url = MagicMock()
        request.url.__str__ = MagicMock(return_value="https://example.com")

        result = create_payment_requirements(request, price_usd=1.0, description="Test")
        assert result.max_amount_required == "1000000"

        result = create_payment_requirements(request, price_usd=0.01, description="Test")
        assert result.max_amount_required == "10000"


class TestCreate402Response:
    """Test 402 response generation."""

    @patch("app.x402.middleware.settings")
    def test_create_402_response(self, mock_settings):
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
        assert decode_payment_header("not-valid-base64!!!") is None

    def test_decode_invalid_json(self):
        encoded = b64encode(b"not json").decode()
        assert decode_payment_header(encoded) is None

    def test_decode_empty_string(self):
        assert decode_payment_header("") is None


class TestEncodePaymentResponse:
    """Test X-PAYMENT-RESPONSE header encoding."""

    def test_encode_settle_response(self):
        from x402.types import SettleResponse
        settle_response = SettleResponse(
            success=True,
            transaction="0xabc123def456",
            network="base-sepolia"
        )
        encoded = encode_payment_response(settle_response)
        from base64 import b64decode
        decoded = json.loads(b64decode(encoded).decode())
        assert decoded["success"] is True
        assert decoded["transaction"] == "0xabc123def456"
        assert decoded["network"] == "base-sepolia"


class TestX402MiddlewareFlow:
    """Test middleware and dependency integration flow."""

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

    @patch("app.x402.dependency.check_base_eth_balance", return_value=OK_BALANCE)
    @patch("app.x402.middleware.settings")
    @patch("app.x402.dependency.settings")
    def test_unprotected_endpoint_passes_through(self, mock_dep, mock_mw, mock_balance):
        """Unprotected endpoints pass through even when x402 enabled."""
        _configure_dep(mock_dep, mock_mw)

        async def download_data(reference: str):
            return {"reference": reference}

        app = _make_app(("GET", "/api/v1/data/{reference}", download_data))
        client = TestClient(app)
        response = client.get("/api/v1/data/abc123")
        assert response.status_code == 200
        assert response.json() == {"reference": "abc123"}

    @patch("app.x402.dependency.check_base_eth_balance", return_value=OK_BALANCE)
    @patch("app.x402.dependency.get_price_quote")
    @patch("app.x402.middleware.settings")
    @patch("app.x402.dependency.settings")
    def test_protected_endpoint_returns_402_without_payment(self, mock_dep, mock_mw, mock_price, mock_balance):
        """Protected endpoint returns 402 without X-PAYMENT header when free tier disabled."""
        _configure_dep(mock_dep, mock_mw, free_tier=False)
        mock_price.return_value = {"price_usd": 0.05, "description": "Data upload"}

        async def upload_data():
            return {"status": "uploaded"}

        app = _make_app(("POST", "/api/v1/data/", upload_data))
        client = TestClient(app)
        response = client.post("/api/v1/data/")

        assert response.status_code == 402
        body = response.json()["detail"]
        assert body["x402Version"] == 1
        assert "Payment required" in body["error"]
        assert "accepts" in body

    @patch("app.x402.dependency.check_base_eth_balance", return_value=OK_BALANCE)
    @patch("app.x402.dependency.get_price_quote")
    @patch("app.x402.middleware.settings")
    @patch("app.x402.dependency.settings")
    def test_invalid_payment_header_returns_402(self, mock_dep, mock_mw, mock_price, mock_balance):
        """Invalid X-PAYMENT header returns 402."""
        _configure_dep(mock_dep, mock_mw)
        mock_price.return_value = {"price_usd": 0.05, "description": "Data upload"}

        async def upload_data():
            return {"status": "uploaded"}

        app = _make_app(("POST", "/api/v1/data/", upload_data))
        client = TestClient(app)
        response = client.post("/api/v1/data/", headers={X_PAYMENT_HEADER: "invalid-base64!!!"})

        assert response.status_code == 402
        body = response.json()["detail"]
        assert "Invalid X-PAYMENT header format" in body["error"]

    @patch("app.x402.dependency.check_base_eth_balance", return_value=OK_BALANCE)
    @patch("app.x402.dependency._get_facilitator_client")
    @patch("app.x402.dependency.get_price_quote")
    @patch("app.x402.middleware.settings")
    @patch("app.x402.dependency.settings")
    def test_verification_failure_returns_402(self, mock_dep, mock_mw, mock_price, mock_get_fac, mock_balance):
        """Payment verification failure returns 402."""
        _configure_dep(mock_dep, mock_mw)
        mock_dep.X402_FACILITATOR_URL = "https://x402.org/facilitator"
        mock_price.return_value = {"price_usd": 0.05, "description": "Data upload"}

        from x402.types import VerifyResponse
        mock_fac = MagicMock()
        mock_fac.verify = AsyncMock(return_value=VerifyResponse(
            is_valid=False, invalid_reason="Insufficient balance", payer=None
        ))
        mock_get_fac.return_value = mock_fac

        async def upload_data():
            return {"status": "uploaded"}

        app = _make_app(("POST", "/api/v1/data/", upload_data))
        client = TestClient(app)

        payload = {
            "x402Version": 1, "scheme": "exact", "network": "base-sepolia",
            "payload": {
                "signature": "0xabc123",
                "authorization": {
                    "from": "0xpayer", "to": "0xpayee", "value": "50000",
                    "validAfter": "0",
                    "validBefore": "115792089237316195423570985008687907853269984665640564039457584007913129639935",
                    "nonce": "0x123"
                }
            }
        }
        encoded_payment = b64encode(json.dumps(payload).encode()).decode()
        response = client.post("/api/v1/data/", headers={X_PAYMENT_HEADER: encoded_payment})

        assert response.status_code == 402
        body = response.json()["detail"]
        assert "Insufficient balance" in body["error"]


class TestMiddlewarePriceCalculation:
    """Test dependency price calculation for different endpoints."""

    @patch("app.x402.dependency.check_base_eth_balance", return_value=OK_BALANCE)
    @patch("app.x402.dependency.get_price_quote")
    @patch("app.x402.middleware.settings")
    @patch("app.x402.dependency.settings")
    def test_stamps_endpoint_pricing(self, mock_dep, mock_mw, mock_price, mock_balance):
        _configure_dep(mock_dep, mock_mw, free_tier=False)
        mock_price.return_value = {"price_usd": 1.50, "description": "Stamp purchase"}

        async def purchase_stamp():
            return {"status": "purchased"}

        app = _make_app(("POST", "/api/v1/stamps/", purchase_stamp))
        client = TestClient(app)
        response = client.post("/api/v1/stamps/")

        assert response.status_code == 402
        body = response.json()["detail"]
        mock_price.assert_called()
        assert body["accepts"][0]["maxAmountRequired"] == "1500000"

    @patch("app.x402.dependency.check_base_eth_balance", return_value=OK_BALANCE)
    @patch("app.x402.dependency.get_price_quote")
    @patch("app.x402.middleware.settings")
    @patch("app.x402.dependency.settings")
    def test_data_endpoint_uses_content_length(self, mock_dep, mock_mw, mock_price, mock_balance):
        _configure_dep(mock_dep, mock_mw, free_tier=False)
        mock_price.return_value = {"price_usd": 0.10, "description": "Data upload"}

        async def upload_data():
            return {"status": "uploaded"}

        app = _make_app(("POST", "/api/v1/data/", upload_data))
        client = TestClient(app)
        response = client.post("/api/v1/data/", headers={"Content-Length": "10240"})

        assert response.status_code == 402
        mock_price.assert_called()


class TestUSDCAddresses:
    def test_base_mainnet_address(self):
        assert USDC_ADDRESSES["base"] == "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

    def test_base_sepolia_address(self):
        assert USDC_ADDRESSES["base-sepolia"] == "0x036CbD53842c5426634e7929541eC2318f3dCF7e"


class TestTokenMetadata:
    """Test EIP-712 token metadata for payment signatures."""

    def test_all_networks_have_token_metadata(self):
        for network in USDC_ADDRESSES.keys():
            assert network in USDC_TOKEN_METADATA

    def test_token_metadata_has_required_fields(self):
        for network, metadata in USDC_TOKEN_METADATA.items():
            assert "name" in metadata
            assert "version" in metadata

    def test_token_name_is_not_empty(self):
        for network, metadata in USDC_TOKEN_METADATA.items():
            assert metadata["name"]
            assert len(metadata["name"]) > 0

    def test_token_version_is_valid(self):
        for network, metadata in USDC_TOKEN_METADATA.items():
            assert metadata["version"]
            assert isinstance(metadata["version"], str)

    def test_usdc_domain_name_matches_onchain(self):
        assert USDC_TOKEN_METADATA["base"]["name"] == "USDC"
        assert USDC_TOKEN_METADATA["base-sepolia"]["name"] == "USDC"

    def test_token_name_not_human_readable_variation(self):
        forbidden_names = ["USD Coin", "Usd Coin", "usd coin"]
        for network, metadata in USDC_TOKEN_METADATA.items():
            assert metadata["name"] not in forbidden_names

    def test_mainnet_and_testnet_metadata_consistent(self):
        if "base" in USDC_TOKEN_METADATA and "base-sepolia" in USDC_TOKEN_METADATA:
            assert USDC_TOKEN_METADATA["base"]["name"] == USDC_TOKEN_METADATA["base-sepolia"]["name"]
            assert USDC_TOKEN_METADATA["base"]["version"] == USDC_TOKEN_METADATA["base-sepolia"]["version"]


class TestProtectedEndpoints:
    def test_protected_endpoints_list(self):
        expected = [
            ("POST", "/api/v1/stamps/"),
            ("POST", "/api/v1/data/"),
            ("POST", "/api/v1/data/manifest"),
        ]
        assert PROTECTED_ENDPOINTS == expected

    def test_get_endpoints_not_protected(self):
        for method, path in PROTECTED_ENDPOINTS:
            assert method != "GET"


class TestFreeTierAccess:
    """Test free tier access for users without x402 payment capability."""

    def setup_method(self):
        from app.x402.ratelimit import reset_rate_limiter
        reset_rate_limiter()

    def teardown_method(self):
        from app.x402.ratelimit import reset_rate_limiter
        reset_rate_limiter()

    @patch("app.x402.dependency.check_base_eth_balance", return_value=OK_BALANCE)
    @patch("app.x402.dependency.get_price_quote")
    @patch("app.x402.middleware.settings")
    @patch("app.x402.dependency.settings")
    def test_free_tier_allows_access_without_payment(self, mock_dep, mock_mw, mock_price, mock_balance):
        _configure_dep(mock_dep, mock_mw, free_tier=True)
        mock_price.return_value = {"price_usd": 0.05, "description": "Data upload"}

        async def upload_data():
            return {"status": "uploaded"}

        app = _make_app(("POST", "/api/v1/data/", upload_data))
        client = TestClient(app)
        response = client.post("/api/v1/data/", headers={"X-Payment-Mode": "free"})

        assert response.status_code == 200
        assert response.json() == {"status": "uploaded"}
        assert response.headers.get("X-Payment-Mode") == "free-tier"
        assert "X-RateLimit-Limit" in response.headers
        assert "X-RateLimit-Remaining" in response.headers

    @patch("app.x402.dependency.check_base_eth_balance", return_value=OK_BALANCE)
    @patch("app.x402.ratelimit.settings")
    @patch("app.x402.dependency.get_price_quote")
    @patch("app.x402.middleware.settings")
    @patch("app.x402.dependency.settings")
    def test_free_tier_rate_limit_enforced(self, mock_dep, mock_mw, mock_price, mock_rl, mock_balance):
        _configure_dep(mock_dep, mock_mw, free_tier=True)
        mock_dep.X402_FREE_TIER_RATE_LIMIT = 2
        mock_rl.X402_FREE_TIER_RATE_LIMIT = 2
        mock_rl.X402_RATE_LIMIT_PER_IP = 10
        mock_price.return_value = {"price_usd": 0.05, "description": "Data upload"}

        async def upload_data():
            return {"status": "uploaded"}

        app = _make_app(("POST", "/api/v1/data/", upload_data))
        client = TestClient(app)

        for i in range(2):
            response = client.post("/api/v1/data/", headers={"X-Payment-Mode": "free"})
            assert response.status_code == 200, f"Request {i+1} should succeed"

        response = client.post("/api/v1/data/", headers={"X-Payment-Mode": "free"})
        assert response.status_code == 429
        body = response.json()["detail"]
        assert body["error"] == "Rate limit exceeded"
        assert "free tier" in body["detail"].lower()
        assert "payment_info" in body
        assert body["payment_info"]["price_usd"] == 0.05

    @patch("app.x402.dependency.check_base_eth_balance", return_value=OK_BALANCE)
    @patch("app.x402.dependency.get_price_quote")
    @patch("app.x402.middleware.settings")
    @patch("app.x402.dependency.settings")
    def test_free_tier_disabled_returns_402(self, mock_dep, mock_mw, mock_price, mock_balance):
        _configure_dep(mock_dep, mock_mw, free_tier=False)
        mock_price.return_value = {"price_usd": 0.05, "description": "Data upload"}

        async def upload_data():
            return {"status": "uploaded"}

        app = _make_app(("POST", "/api/v1/data/", upload_data))
        client = TestClient(app)
        response = client.post("/api/v1/data/")
        assert response.status_code == 402

    @patch("app.x402.dependency.check_base_eth_balance", return_value=OK_BALANCE)
    @patch("app.x402.ratelimit.settings")
    @patch("app.x402.dependency.get_price_quote")
    @patch("app.x402.middleware.settings")
    @patch("app.x402.dependency.settings")
    def test_free_tier_rate_limit_headers_present(self, mock_dep, mock_mw, mock_price, mock_rl, mock_balance):
        _configure_dep(mock_dep, mock_mw, free_tier=True)
        mock_dep.X402_FREE_TIER_RATE_LIMIT = 5
        mock_rl.X402_FREE_TIER_RATE_LIMIT = 5
        mock_rl.X402_RATE_LIMIT_PER_IP = 10
        mock_price.return_value = {"price_usd": 0.05, "description": "Data upload"}

        async def upload_data():
            return {"status": "uploaded"}

        app = _make_app(("POST", "/api/v1/data/", upload_data))
        client = TestClient(app)
        response = client.post("/api/v1/data/", headers={"X-Payment-Mode": "free"})

        assert response.status_code == 200
        assert response.headers["X-RateLimit-Limit"] == "5"
        assert response.headers["X-RateLimit-Remaining"] == "4"
        assert response.headers["X-Payment-Mode"] == "free-tier"

    @patch("app.x402.dependency.check_base_eth_balance", return_value=OK_BALANCE)
    @patch("app.x402.ratelimit.settings")
    @patch("app.x402.dependency.get_price_quote")
    @patch("app.x402.middleware.settings")
    @patch("app.x402.dependency.settings")
    def test_free_tier_429_includes_upgrade_info(self, mock_dep, mock_mw, mock_price, mock_rl, mock_balance):
        _configure_dep(mock_dep, mock_mw, free_tier=True)
        mock_dep.X402_FREE_TIER_RATE_LIMIT = 1
        mock_dep.X402_PAY_TO_ADDRESS = "0xPaymentWallet"
        mock_mw.X402_PAY_TO_ADDRESS = "0xPaymentWallet"
        mock_rl.X402_FREE_TIER_RATE_LIMIT = 1
        mock_rl.X402_RATE_LIMIT_PER_IP = 10
        mock_price.return_value = {"price_usd": 0.10, "description": "Stamp purchase"}

        async def purchase_stamp():
            return {"status": "purchased"}

        app = _make_app(("POST", "/api/v1/stamps/", purchase_stamp))
        client = TestClient(app)

        response = client.post("/api/v1/stamps/", headers={"X-Payment-Mode": "free"})
        assert response.status_code == 200

        response = client.post("/api/v1/stamps/", headers={"X-Payment-Mode": "free"})
        assert response.status_code == 429

        body = response.json()["detail"]
        assert "payment_info" in body
        assert body["payment_info"]["network"] == "base-sepolia"
        assert body["payment_info"]["pay_to"] == "0xPaymentWallet"
        assert body["payment_info"]["price_usd"] == 0.10
        assert "Use x402 payment for higher limits" in body["message"]

    @patch("app.x402.dependency.check_base_eth_balance", return_value=OK_BALANCE)
    @patch("app.x402.dependency.get_price_quote")
    @patch("app.x402.middleware.settings")
    @patch("app.x402.dependency.settings")
    def test_different_endpoints_share_free_tier_limit(self, mock_dep, mock_mw, mock_price, mock_balance):
        _configure_dep(mock_dep, mock_mw, free_tier=True)
        mock_price.return_value = {"price_usd": 0.05, "description": "Operation"}

        async def purchase_stamp():
            return {"status": "purchased"}

        async def upload_data():
            return {"status": "uploaded"}

        app = _make_app(
            ("POST", "/api/v1/stamps/", purchase_stamp),
            ("POST", "/api/v1/data/", upload_data),
        )
        client = TestClient(app)

        assert client.post("/api/v1/stamps/", headers={"X-Payment-Mode": "free"}).status_code == 200
        assert client.post("/api/v1/data/", headers={"X-Payment-Mode": "free"}).status_code == 200
        assert client.post("/api/v1/stamps/", headers={"X-Payment-Mode": "free"}).status_code == 200

        response = client.post("/api/v1/data/", headers={"X-Payment-Mode": "free"})
        assert response.status_code == 429

    @patch("app.x402.dependency.check_base_eth_balance", return_value=OK_BALANCE)
    @patch("app.x402.ratelimit.settings")
    @patch("app.x402.dependency.get_price_quote")
    @patch("app.x402.middleware.settings")
    @patch("app.x402.dependency.settings")
    def test_402_response_includes_free_tier_info(self, mock_dep, mock_mw, mock_price, mock_rl, mock_balance):
        _configure_dep(mock_dep, mock_mw, free_tier=True)
        mock_dep.X402_FREE_TIER_RATE_LIMIT = 5
        mock_rl.X402_FREE_TIER_ENABLED = True
        mock_rl.X402_FREE_TIER_RATE_LIMIT = 5
        mock_rl.X402_RATE_LIMIT_PER_IP = 10
        mock_price.return_value = {"price_usd": 0.05, "description": "Data upload"}

        async def upload_data():
            return {"status": "uploaded"}

        app = _make_app(("POST", "/api/v1/data/", upload_data))
        client = TestClient(app)
        response = client.post("/api/v1/data/")

        assert response.status_code == 402
        body = response.json()["detail"]
        assert body["x402Version"] == 1
        assert "accepts" in body
        assert "freeTier" in body
        assert body["freeTier"]["available"] is True
        assert body["freeTier"]["requestsLimit"] == 5
        assert "X-Payment-Mode: free" in body["freeTier"]["instruction"]

    @patch("app.x402.dependency.check_base_eth_balance", return_value=OK_BALANCE)
    @patch("app.x402.dependency.get_price_quote")
    @patch("app.x402.middleware.settings")
    @patch("app.x402.dependency.settings")
    def test_402_response_no_free_tier_when_disabled(self, mock_dep, mock_mw, mock_price, mock_balance):
        _configure_dep(mock_dep, mock_mw, free_tier=False)
        mock_price.return_value = {"price_usd": 0.05, "description": "Data upload"}

        async def upload_data():
            return {"status": "uploaded"}

        app = _make_app(("POST", "/api/v1/data/", upload_data))
        client = TestClient(app)
        response = client.post("/api/v1/data/")

        assert response.status_code == 402
        body = response.json()["detail"]
        assert body["x402Version"] == 1
        assert "accepts" in body
        assert "freeTier" not in body

    @patch("app.x402.dependency.check_base_eth_balance", return_value=OK_BALANCE)
    @patch("app.x402.dependency.get_price_quote")
    @patch("app.x402.middleware.settings")
    @patch("app.x402.dependency.settings")
    def test_free_tier_request_disabled_returns_402(self, mock_dep, mock_mw, mock_price, mock_balance):
        _configure_dep(mock_dep, mock_mw, free_tier=False)
        mock_price.return_value = {"price_usd": 0.05, "description": "Data upload"}

        async def upload_data():
            return {"status": "uploaded"}

        app = _make_app(("POST", "/api/v1/data/", upload_data))
        client = TestClient(app)
        response = client.post("/api/v1/data/", headers={"X-Payment-Mode": "free"})

        assert response.status_code == 402
        body = response.json()["detail"]
        assert "Free tier is not available" in body["error"]
