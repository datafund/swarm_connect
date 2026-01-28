# tests/test_cors.py
"""Tests for CORS middleware configuration."""

import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient


class TestCorsConfiguration:
    """Test CORS settings parsing in config."""

    def test_cors_origins_wildcard(self):
        """Test wildcard CORS origin."""
        with patch.dict("os.environ", {
            "SWARM_BEE_API_URL": "https://api.gateway.ethswarm.org",
            "CORS_ALLOWED_ORIGINS": "*",
        }, clear=False):
            from app.core.config import Settings
            settings = Settings()
            assert settings.get_cors_origins() == ["*"]

    def test_cors_origins_single(self):
        """Test single CORS origin."""
        with patch.dict("os.environ", {
            "SWARM_BEE_API_URL": "https://api.gateway.ethswarm.org",
            "CORS_ALLOWED_ORIGINS": "https://app.datafund.io",
        }, clear=False):
            from app.core.config import Settings
            settings = Settings()
            assert settings.get_cors_origins() == ["https://app.datafund.io"]

    def test_cors_origins_multiple(self):
        """Test multiple comma-separated CORS origins."""
        with patch.dict("os.environ", {
            "SWARM_BEE_API_URL": "https://api.gateway.ethswarm.org",
            "CORS_ALLOWED_ORIGINS": "https://app.datafund.io,https://fairdrop.datafund.io,https://verity.datafund.io",
        }, clear=False):
            from app.core.config import Settings
            settings = Settings()
            origins = settings.get_cors_origins()
            assert len(origins) == 3
            assert "https://app.datafund.io" in origins
            assert "https://fairdrop.datafund.io" in origins
            assert "https://verity.datafund.io" in origins

    def test_cors_origins_with_spaces(self):
        """Test CORS origins with spaces are trimmed."""
        with patch.dict("os.environ", {
            "SWARM_BEE_API_URL": "https://api.gateway.ethswarm.org",
            "CORS_ALLOWED_ORIGINS": "https://app.datafund.io , https://fairdrop.datafund.io",
        }, clear=False):
            from app.core.config import Settings
            settings = Settings()
            origins = settings.get_cors_origins()
            assert len(origins) == 2
            assert "https://app.datafund.io" in origins
            assert "https://fairdrop.datafund.io" in origins

    def test_cors_credentials_default_false(self):
        """Test CORS credentials defaults to false."""
        with patch.dict("os.environ", {
            "SWARM_BEE_API_URL": "https://api.gateway.ethswarm.org",
        }, clear=False):
            from app.core.config import Settings
            settings = Settings()
            assert settings.CORS_ALLOW_CREDENTIALS is False

    def test_cors_credentials_can_be_enabled(self):
        """Test CORS credentials can be enabled."""
        with patch.dict("os.environ", {
            "SWARM_BEE_API_URL": "https://api.gateway.ethswarm.org",
            "CORS_ALLOW_CREDENTIALS": "true",
        }, clear=False):
            from app.core.config import Settings
            settings = Settings()
            assert settings.CORS_ALLOW_CREDENTIALS is True


class TestCorsMiddleware:
    """Test CORS middleware behavior with actual HTTP requests.

    These tests use the default app which has CORS enabled with allow_origins=["*"].
    """

    @pytest.fixture
    def client(self):
        """Create test client using the default app."""
        from app.main import app
        return TestClient(app)

    def test_cors_headers_on_get(self, client):
        """Test CORS headers are present on GET request."""
        response = client.get(
            "/health",
            headers={"Origin": "http://localhost:5173"}
        )
        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == "*"

    def test_cors_preflight_options(self, client):
        """Test CORS preflight OPTIONS request."""
        response = client.options(
            "/health",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            }
        )
        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == "*"
        assert "GET" in response.headers.get("access-control-allow-methods", "")

    def test_cors_preflight_post(self, client):
        """Test CORS preflight for POST request."""
        response = client.options(
            "/api/v1/data/",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            }
        )
        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == "*"
        assert "POST" in response.headers.get("access-control-allow-methods", "")

    def test_cors_allows_all_headers(self, client):
        """Test CORS allows custom headers."""
        response = client.options(
            "/health",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "x-custom-header,authorization",
            }
        )
        assert response.status_code == 200
        # FastAPI CORS middleware reflects requested headers
        allow_headers = response.headers.get("access-control-allow-headers", "")
        assert "x-custom-header" in allow_headers.lower() or "*" in allow_headers

    def test_cors_on_api_endpoints(self, client):
        """Test CORS headers on API endpoints."""
        response = client.options(
            "/api/v1/stamps/",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
            }
        )
        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == "*"

    def test_cors_from_different_origins(self, client):
        """Test CORS allows requests from various origins when using wildcard."""
        origins = [
            "http://localhost:3000",
            "http://localhost:5173",
            "https://app.datafund.io",
            "https://example.com",
        ]
        for origin in origins:
            response = client.get(
                "/health",
                headers={"Origin": origin}
            )
            assert response.status_code == 200
            assert response.headers.get("access-control-allow-origin") == "*"


class TestCorsOn402Responses:
    """Test that CORS headers are present on 402 Payment Required responses.

    This is critical for browser-based SDK usage - without CORS headers on 402s,
    browsers block the response and clients can't see payment requirements.

    Issue: https://github.com/datafund/swarm_connect/issues/90
    """

    def test_cors_on_402_response_middleware_order(self):
        """Test that CORS middleware wraps x402 middleware correctly.

        The middleware order in app.add_middleware() is LIFO for responses:
        - Last added middleware is the outer wrapper
        - CORS must be added LAST to wrap all responses including 402s

        This test creates a minimal app to verify the middleware ordering pattern.
        """
        from fastapi import FastAPI
        from fastapi.middleware.cors import CORSMiddleware
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.responses import JSONResponse

        # Create a middleware that returns 402 (simulating x402)
        class Mock402Middleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                if request.url.path == "/protected":
                    return JSONResponse(
                        status_code=402,
                        content={"error": "Payment Required"}
                    )
                return await call_next(request)

        # Create test app with correct middleware order
        test_app = FastAPI()

        @test_app.get("/")
        def root():
            return {"status": "ok"}

        @test_app.post("/protected")
        def protected():
            return {"status": "ok"}

        # Add middlewares in correct order: 402 middleware first, then CORS
        # (CORS added last = outer wrapper = processes all responses)
        test_app.add_middleware(Mock402Middleware)
        test_app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )

        client = TestClient(test_app)

        # Test 402 response has CORS headers
        response = client.post(
            "/protected",
            headers={"Origin": "http://localhost:5173"}
        )
        assert response.status_code == 402
        assert response.headers.get("access-control-allow-origin") == "*", \
            "402 responses must have CORS headers for browser SDK compatibility"

    def test_cors_on_402_preflight_still_works(self):
        """Test that preflight OPTIONS requests work for protected endpoints."""
        from fastapi import FastAPI
        from fastapi.middleware.cors import CORSMiddleware
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.responses import JSONResponse

        class Mock402Middleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                # Don't block OPTIONS requests
                if request.method == "OPTIONS":
                    return await call_next(request)
                if request.url.path == "/protected":
                    return JSONResponse(
                        status_code=402,
                        content={"error": "Payment Required"}
                    )
                return await call_next(request)

        test_app = FastAPI()

        @test_app.post("/protected")
        def protected():
            return {"status": "ok"}

        test_app.add_middleware(Mock402Middleware)
        test_app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )

        client = TestClient(test_app)

        # Preflight should succeed
        response = client.options(
            "/protected",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
            }
        )
        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == "*"

    def test_wrong_middleware_order_breaks_cors_on_402(self):
        """Demonstrate that wrong middleware order breaks CORS on 402 responses.

        This test proves the bug described in issue #90:
        If CORS is added BEFORE the 402 middleware, 402 responses won't have CORS headers.
        """
        from fastapi import FastAPI
        from fastapi.middleware.cors import CORSMiddleware
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.responses import JSONResponse

        class Mock402Middleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                if request.url.path == "/protected":
                    return JSONResponse(
                        status_code=402,
                        content={"error": "Payment Required"}
                    )
                return await call_next(request)

        # Create app with WRONG order (CORS first, then 402 middleware)
        test_app = FastAPI()

        @test_app.post("/protected")
        def protected():
            return {"status": "ok"}

        # WRONG ORDER: CORS added first, 402 middleware added second (outer)
        test_app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )
        test_app.add_middleware(Mock402Middleware)

        client = TestClient(test_app)

        # 402 response will NOT have CORS headers with wrong order
        response = client.post(
            "/protected",
            headers={"Origin": "http://localhost:5173"}
        )
        assert response.status_code == 402
        # This demonstrates the bug - no CORS header with wrong order
        assert response.headers.get("access-control-allow-origin") is None, \
            "Wrong middleware order: 402 response missing CORS headers"
