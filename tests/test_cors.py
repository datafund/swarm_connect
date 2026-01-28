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
