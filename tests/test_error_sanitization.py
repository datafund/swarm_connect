# tests/test_error_sanitization.py
"""
Tests for error response sanitization (Issue #104).
Ensures internal details (paths, URLs, exception messages) are not leaked in HTTP responses.
"""
import io
import pytest
import httpx
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

VALID_STAMP_ID = "a" * 64


class TestDataUploadErrorSanitization:
    """Ensure data upload errors don't leak internal details."""

    @patch('app.api.endpoints.data.check_upload_failure_reason', return_value=None)
    @patch('app.api.endpoints.data.upload_data_to_swarm')
    def test_swarm_connection_error_sanitized(self, mock_upload, mock_failure):
        """Connection errors should not expose Bee node URL."""
        mock_upload.side_effect = httpx.ConnectError(
            "HTTPSConnectionPool(host='internal-bee.local', port=1633): Max retries exceeded"
        )
        response = client.post(
            f"/api/v1/data/?stamp_id={VALID_STAMP_ID}",
            files={"file": ("test.json", io.BytesIO(b'{"key":"val"}'), "application/json")}
        )
        assert response.status_code == 502
        detail = response.json()["detail"]
        assert "internal-bee.local" not in detail
        assert "1633" not in detail
        assert "HTTPSConnectionPool" not in detail

    @patch('app.api.endpoints.data.check_upload_failure_reason', return_value=None)
    @patch('app.api.endpoints.data.upload_data_to_swarm')
    def test_value_error_sanitized(self, mock_upload, mock_failure):
        """ValueError should not expose internal processing details."""
        mock_upload.side_effect = ValueError(
            "Invalid JSON at /app/services/swarm_api.py:123"
        )
        response = client.post(
            f"/api/v1/data/?stamp_id={VALID_STAMP_ID}",
            files={"file": ("test.json", io.BytesIO(b'{"key":"val"}'), "application/json")}
        )
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "/app/services/" not in detail
        assert "swarm_api.py" not in detail


class TestDataDownloadErrorSanitization:
    """Ensure download errors don't leak internal details."""

    @patch('app.api.endpoints.data.download_data_from_swarm')
    def test_download_connection_error_sanitized(self, mock_download):
        """Download errors should not expose Bee node URL."""
        mock_download.side_effect = httpx.ConnectError(
            "HTTPSConnectionPool(host='secret-bee.internal', port=1633): Connection refused"
        )
        ref = "a" * 64
        response = client.get(f"/api/v1/data/{ref}")
        assert response.status_code == 502
        detail = response.json()["detail"]
        assert "secret-bee.internal" not in detail
        assert "Connection refused" not in detail


class TestStampsErrorSanitization:
    """Ensure stamp endpoint errors don't leak internal details."""

    @patch('app.services.swarm_api.get_all_stamps_processed')
    def test_list_stamps_error_sanitized(self, mock_stamps):
        """List stamps error should not expose Bee node URL."""
        mock_stamps.side_effect = httpx.ConnectError(
            "HTTPSConnectionPool(host='bee.secret.net', port=1633): Max retries"
        )
        response = client.get("/api/v1/stamps/")
        assert response.status_code == 502
        detail = response.json()["detail"]
        assert "bee.secret.net" not in detail
        assert "Max retries" not in detail

    @patch('app.services.swarm_api.get_all_stamps_processed')
    def test_get_stamp_error_sanitized(self, mock_stamps):
        """Get stamp error should not expose Bee node URL."""
        mock_stamps.side_effect = httpx.ConnectError(
            "HTTPSConnectionPool(host='10.0.0.5', port=1633): Connection timed out"
        )
        response = client.get(f"/api/v1/stamps/{VALID_STAMP_ID}")
        assert response.status_code == 502
        detail = response.json()["detail"]
        assert "10.0.0.5" not in detail
        assert "timed out" not in detail

    @patch('app.services.swarm_api.check_sufficient_funds', return_value={"sufficient": True, "required_bzz": 0.01, "wallet_balance_bzz": 100.0, "shortfall_bzz": 0})
    @patch('app.services.swarm_api.purchase_postage_stamp')
    def test_purchase_stamp_error_sanitized(self, mock_purchase, mock_funds):
        """Purchase error should not expose internal API details."""
        mock_purchase.side_effect = httpx.ConnectError(
            "POST https://bee-node.internal:1633/stamps/100/17 failed"
        )
        response = client.post("/api/v1/stamps/", json={"amount": 100, "depth": 17})
        assert response.status_code == 502
        detail = response.json()["detail"]
        assert "bee-node.internal" not in detail
        assert "1633" not in detail

    @patch('app.services.swarm_api.check_sufficient_funds', return_value={"sufficient": True, "required_bzz": 0.01, "wallet_balance_bzz": 100.0, "shortfall_bzz": 0})
    @patch('app.services.swarm_api.purchase_postage_stamp')
    def test_purchase_value_error_sanitized(self, mock_purchase, mock_funds):
        """Purchase ValueError should not expose internal details."""
        mock_purchase.side_effect = ValueError(
            "Expected JSON but got HTML from https://internal-api:1633/stamps"
        )
        response = client.post("/api/v1/stamps/", json={"amount": 100, "depth": 17})
        assert response.status_code == 500
        detail = response.json()["detail"]
        assert "internal-api" not in detail
        assert "1633" not in detail

    @patch('app.services.swarm_api.extend_postage_stamp')
    @patch('app.services.swarm_api.check_sufficient_funds', return_value={"sufficient": True, "required_bzz": 0.01, "wallet_balance_bzz": 100.0, "shortfall_bzz": 0})
    @patch('app.services.swarm_api.calculate_stamp_total_cost', return_value=1000)
    @patch('app.services.swarm_api.get_all_stamps_processed', return_value=[{"batchID": "a" * 64, "depth": 17, "local": True}])
    def test_extend_stamp_error_sanitized(self, mock_stamps, mock_cost, mock_funds, mock_extend):
        """Extend error should not expose internal details."""
        mock_extend.side_effect = httpx.ConnectError(
            "PATCH https://bee.internal:1633/stamps/topup/test_id/500 failed"
        )
        response = client.patch(f"/api/v1/stamps/{VALID_STAMP_ID}/extend", json={"amount": 500})
        assert response.status_code == 502
        detail = response.json()["detail"]
        assert "bee.internal" not in detail


class TestManifestUploadErrorSanitization:
    """Ensure manifest upload errors don't leak internal details."""

    @patch('app.api.endpoints.data.check_upload_failure_reason', return_value=None)
    @patch('app.api.endpoints.data.upload_collection_to_swarm')
    @patch('app.api.endpoints.data.count_tar_files', return_value=1)
    @patch('app.api.endpoints.data.validate_tar')
    def test_manifest_connection_error_sanitized(self, mock_validate, mock_count, mock_upload, mock_failure):
        """Manifest upload errors should not expose Bee node URL."""
        mock_upload.side_effect = httpx.ConnectError(
            "HTTPSConnectionPool(host='bee.secret.net', port=1633): Max retries"
        )
        response = client.post(
            f"/api/v1/data/manifest?stamp_id={VALID_STAMP_ID}",
            files={"file": ("test.tar", io.BytesIO(b"tardata"), "application/x-tar")}
        )
        assert response.status_code == 502
        detail = response.json()["detail"]
        assert "bee.secret.net" not in detail
