# tests/test_cors.py
"""Tests for CORS middleware configuration."""

import pytest
from unittest.mock import patch, MagicMock


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
    """Test CORS middleware behavior with actual HTTP requests."""

    @pytest.fixture
    def client_with_cors(self):
        """Create test client with CORS enabled."""
        with patch.dict("os.environ", {
            "SWARM_BEE_API_URL": "https://api.gateway.ethswarm.org",
            "CORS_ALLOWED_ORIGINS": "*",
            "CORS_ALLOW_CREDENTIALS": "false",
            "STAMP_POOL_ENABLED": "false",
            "X402_ENABLED": "false",
        }, clear=False):
            # Need to reload modules to pick up new settings
            import importlib
            import app.core.config
            importlib.reload(app.core.config)

            # Now import main which will create app with new settings
            import app.main
            importlib.reload(app.main)

            from fastapi.testclient import TestClient
            return TestClient(app.main.app)

    def test_cors_headers_on_get(self, client_with_cors):
        """Test CORS headers are present on GET request."""
        response = client_with_cors.get(
            "/health",
            headers={"Origin": "http://localhost:5173"}
        )
        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == "*"

    def test_cors_preflight_options(self, client_with_cors):
        """Test CORS preflight OPTIONS request."""
        response = client_with_cors.options(
            "/health",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            }
        )
        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == "*"
        assert "GET" in response.headers.get("access-control-allow-methods", "")

    def test_cors_preflight_post(self, client_with_cors):
        """Test CORS preflight for POST request."""
        response = client_with_cors.options(
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

    def test_cors_allows_all_headers(self, client_with_cors):
        """Test CORS allows custom headers."""
        response = client_with_cors.options(
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


class TestCorsWithSpecificOrigins:
    """Test CORS with specific allowed origins."""

    @pytest.fixture
    def client_with_specific_origins(self):
        """Create test client with specific CORS origins."""
        with patch.dict("os.environ", {
            "SWARM_BEE_API_URL": "https://api.gateway.ethswarm.org",
            "CORS_ALLOWED_ORIGINS": "https://app.datafund.io,https://fairdrop.datafund.io",
            "CORS_ALLOW_CREDENTIALS": "true",
            "STAMP_POOL_ENABLED": "false",
            "X402_ENABLED": "false",
        }, clear=False):
            import importlib
            import app.core.config
            importlib.reload(app.core.config)

            import app.main
            importlib.reload(app.main)

            from fastapi.testclient import TestClient
            return TestClient(app.main.app)

    def test_allowed_origin_gets_cors_header(self, client_with_specific_origins):
        """Test that allowed origin receives CORS header."""
        response = client_with_specific_origins.get(
            "/health",
            headers={"Origin": "https://app.datafund.io"}
        )
        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == "https://app.datafund.io"

    def test_disallowed_origin_no_cors_header(self, client_with_specific_origins):
        """Test that disallowed origin doesn't receive CORS header."""
        response = client_with_specific_origins.get(
            "/health",
            headers={"Origin": "https://malicious-site.com"}
        )
        assert response.status_code == 200
        # No CORS header for disallowed origin
        assert response.headers.get("access-control-allow-origin") is None

    def test_credentials_header_with_specific_origins(self, client_with_specific_origins):
        """Test credentials header is present with specific origins."""
        response = client_with_specific_origins.get(
            "/health",
            headers={"Origin": "https://app.datafund.io"}
        )
        assert response.status_code == 200
        assert response.headers.get("access-control-allow-credentials") == "true"
