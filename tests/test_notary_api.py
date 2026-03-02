# tests/test_notary_api.py
"""
API integration tests for notary/provenance endpoints.
"""
import json
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock

from app.main import app
from app.services.provenance import ProvenanceService, SignedDocument

VALID_STAMP_ID = "a" * 64


class TestNotaryInfoEndpoint:
    """Tests for GET /api/v1/notary/info endpoint."""

    @pytest.fixture
    def client(self):
        """Create a test client."""
        return TestClient(app)

    def test_info_when_disabled(self, client):
        """Test notary info when disabled."""
        with patch('app.api.endpoints.notary.settings') as mock_settings:
            mock_settings.NOTARY_ENABLED = False
            response = client.get("/api/v1/notary/info")

        assert response.status_code == 200
        data = response.json()
        assert data["enabled"] is False
        assert data["available"] is False
        assert data["address"] is None
        assert "not enabled" in data["message"].lower()

    def test_info_when_enabled_but_not_configured(self, client):
        """Test notary info when enabled but no key configured."""
        mock_service = MagicMock(spec=ProvenanceService)
        mock_service.is_available = False
        mock_service.notary_address = None

        with patch('app.api.endpoints.notary.settings') as mock_settings:
            with patch('app.api.endpoints.notary.get_provenance_service', return_value=mock_service):
                mock_settings.NOTARY_ENABLED = True
                response = client.get("/api/v1/notary/info")

        assert response.status_code == 200
        data = response.json()
        assert data["enabled"] is True
        assert data["available"] is False
        assert data["address"] is None
        assert "not configured" in data["message"].lower()

    def test_info_when_fully_configured(self, client):
        """Test notary info when fully configured."""
        mock_service = MagicMock(spec=ProvenanceService)
        mock_service.is_available = True
        mock_service.notary_address = "0x1234567890abcdef"

        with patch('app.api.endpoints.notary.settings') as mock_settings:
            with patch('app.api.endpoints.notary.get_provenance_service', return_value=mock_service):
                mock_settings.NOTARY_ENABLED = True
                response = client.get("/api/v1/notary/info")

        assert response.status_code == 200
        data = response.json()
        assert data["enabled"] is True
        assert data["available"] is True
        assert data["address"] == "0x1234567890abcdef"
        assert "available" in data["message"].lower()


class TestNotaryStatusEndpoint:
    """Tests for GET /api/v1/notary/status endpoint."""

    @pytest.fixture
    def client(self):
        """Create a test client."""
        return TestClient(app)

    def test_status_returns_simplified_response(self, client):
        """Test that status returns minimal response."""
        mock_service = MagicMock(spec=ProvenanceService)
        mock_service.is_available = True
        mock_service.notary_address = "0xabc"

        with patch('app.api.endpoints.notary.settings') as mock_settings:
            with patch('app.api.endpoints.notary.get_provenance_service', return_value=mock_service):
                mock_settings.NOTARY_ENABLED = True
                response = client.get("/api/v1/notary/status")

        assert response.status_code == 200
        data = response.json()
        assert "enabled" in data
        assert "available" in data
        assert "address" in data
        # Status should NOT have message field
        assert "message" not in data


class TestDataUploadWithSigning:
    """Tests for POST /api/v1/data/?sign=notary endpoint."""

    @pytest.fixture
    def client(self):
        """Create a test client."""
        return TestClient(app)

    def test_sign_notary_invalid_sign_option(self, client):
        """Test that invalid sign option returns error."""
        with patch('app.api.endpoints.data.get_provenance_service') as mock:
            mock.return_value = MagicMock(is_available=True)
            response = client.post(
                f"/api/v1/data/?stamp_id={VALID_STAMP_ID}&sign=invalid",
                files={"file": ("test.json", json.dumps({"data": "test"}).encode('utf-8'), "application/json")}
            )

        assert response.status_code == 400
        data = response.json()
        assert data["detail"]["code"] == "INVALID_SIGN_OPTION"

    def test_sign_notary_when_not_enabled(self, client):
        """Test signing when notary is not enabled."""
        # Import to patch at module level
        from app.services.provenance import NotaryNotEnabledError

        mock_service = MagicMock(spec=ProvenanceService)
        mock_service.sign_document.side_effect = NotaryNotEnabledError("Not enabled")

        with patch('app.api.endpoints.data.get_provenance_service', return_value=mock_service):
            response = client.post(
                f"/api/v1/data/?stamp_id={VALID_STAMP_ID}&sign=notary",
                files={"file": ("test.json", json.dumps({"data": "test"}).encode('utf-8'), "application/json")}
            )

        assert response.status_code == 400
        data = response.json()
        assert data["detail"]["code"] == "NOTARY_NOT_ENABLED"

    def test_sign_notary_when_not_configured(self, client):
        """Test signing when notary key is not configured."""
        from app.services.signing import NotConfiguredError

        mock_service = MagicMock(spec=ProvenanceService)
        mock_service.sign_document.side_effect = NotConfiguredError("No key")

        with patch('app.api.endpoints.data.get_provenance_service', return_value=mock_service):
            response = client.post(
                f"/api/v1/data/?stamp_id={VALID_STAMP_ID}&sign=notary",
                files={"file": ("test.json", json.dumps({"data": "test"}).encode('utf-8'), "application/json")}
            )

        assert response.status_code == 400
        data = response.json()
        assert data["detail"]["code"] == "NOTARY_NOT_CONFIGURED"

    def test_sign_notary_invalid_document_format(self, client):
        """Test signing with invalid document format."""
        from app.services.provenance import DocumentValidationError

        mock_service = MagicMock(spec=ProvenanceService)
        mock_service.sign_document.side_effect = DocumentValidationError("Missing data field")

        with patch('app.api.endpoints.data.get_provenance_service', return_value=mock_service):
            response = client.post(
                f"/api/v1/data/?stamp_id={VALID_STAMP_ID}&sign=notary",
                files={"file": ("test.json", json.dumps({"invalid": "doc"}).encode('utf-8'), "application/json")}
            )

        assert response.status_code == 400
        data = response.json()
        assert data["detail"]["code"] == "INVALID_DOCUMENT_FORMAT"

    def test_upload_without_sign_preserved(self, client):
        """Test that uploads without sign parameter work normally."""
        # This test just verifies the endpoint doesn't error with no sign param
        # The actual upload would need a real Swarm connection, so we just check parsing
        response = client.post(
            f"/api/v1/data/?stamp_id={VALID_STAMP_ID}",
            files={"file": ("test.json", b'{"test": "data"}', "application/json")}
        )

        # Will fail due to Swarm connection, but not due to sign parameter
        # We just want to ensure it's not a 400 related to signing
        assert response.status_code != 400 or "sign" not in response.json().get("detail", {}).get("code", "").lower()


class TestErrorResponseFormat:
    """Tests for error response format consistency."""

    @pytest.fixture
    def client(self):
        """Create a test client."""
        return TestClient(app)

    def test_error_response_has_code_and_message(self, client):
        """Test that error responses have code and message."""
        with patch('app.api.endpoints.data.get_provenance_service') as mock:
            mock.return_value = MagicMock(is_available=True)
            response = client.post(
                f"/api/v1/data/?stamp_id={VALID_STAMP_ID}&sign=wrong",
                files={"file": ("test.json", b'{"data": "test"}', "application/json")}
            )

        assert response.status_code == 400
        data = response.json()
        assert "detail" in data
        assert "code" in data["detail"]
        assert "message" in data["detail"]
        assert "suggestion" in data["detail"]

    def test_notary_not_enabled_error_format(self, client):
        """Test error format when notary not enabled."""
        from app.services.provenance import NotaryNotEnabledError

        mock_service = MagicMock(spec=ProvenanceService)
        mock_service.sign_document.side_effect = NotaryNotEnabledError("Not enabled")

        with patch('app.api.endpoints.data.get_provenance_service', return_value=mock_service):
            response = client.post(
                f"/api/v1/data/?stamp_id={VALID_STAMP_ID}&sign=notary",
                files={"file": ("test.json", json.dumps({"data": "test"}).encode('utf-8'), "application/json")}
            )

        data = response.json()
        assert data["detail"]["code"] == "NOTARY_NOT_ENABLED"
        assert "NOTARY_ENABLED=true" in data["detail"]["suggestion"]

    def test_notary_not_configured_error_format(self, client):
        """Test error format when notary not configured."""
        from app.services.signing import NotConfiguredError

        mock_service = MagicMock(spec=ProvenanceService)
        mock_service.sign_document.side_effect = NotConfiguredError("No key")

        with patch('app.api.endpoints.data.get_provenance_service', return_value=mock_service):
            response = client.post(
                f"/api/v1/data/?stamp_id={VALID_STAMP_ID}&sign=notary",
                files={"file": ("test.json", json.dumps({"data": "test"}).encode('utf-8'), "application/json")}
            )

        data = response.json()
        assert data["detail"]["code"] == "NOTARY_NOT_CONFIGURED"
        assert "NOTARY_PRIVATE_KEY" in data["detail"]["suggestion"]

    def test_invalid_document_error_format(self, client):
        """Test error format for invalid document."""
        from app.services.provenance import DocumentValidationError

        mock_service = MagicMock(spec=ProvenanceService)
        mock_service.sign_document.side_effect = DocumentValidationError("Missing data")

        with patch('app.api.endpoints.data.get_provenance_service', return_value=mock_service):
            response = client.post(
                f"/api/v1/data/?stamp_id={VALID_STAMP_ID}&sign=notary",
                files={"file": ("test.json", json.dumps({"no_data": "here"}).encode('utf-8'), "application/json")}
            )

        data = response.json()
        assert data["detail"]["code"] == "INVALID_DOCUMENT_FORMAT"
        assert "'data'" in data["detail"]["suggestion"]
