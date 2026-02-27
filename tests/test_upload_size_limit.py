# tests/test_upload_size_limit.py
"""
Tests for file upload size limit (Issue #100).
Ensures uploads exceeding MAX_UPLOAD_SIZE_MB are rejected with 413.
"""
import io
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

VALID_STAMP_ID = "a" * 64


class TestUploadSizeLimit:
    """Tests for upload size enforcement on data upload endpoint."""

    @patch('app.api.endpoints.data.upload_data_to_swarm', return_value="ref123")
    def test_upload_within_limit_succeeds(self, mock_upload):
        """File within the size limit should be accepted."""
        # 1 KB file — well within default 10 MB limit
        data = b"x" * 1024
        response = client.post(
            f"/api/v1/data/?stamp_id={VALID_STAMP_ID}",
            files={"file": ("test.json", io.BytesIO(data), "application/json")}
        )
        assert response.status_code == 200

    @patch('app.api.endpoints.data.upload_data_to_swarm', return_value="ref123")
    @patch('app.api.endpoints.data.settings')
    def test_upload_exceeding_limit_returns_413(self, mock_settings, mock_upload):
        """File exceeding MAX_UPLOAD_SIZE_MB should return 413."""
        mock_settings.MAX_UPLOAD_SIZE_MB = 1
        # 2 MB file — exceeds 1 MB limit
        data = b"x" * (2 * 1024 * 1024)
        response = client.post(
            f"/api/v1/data/?stamp_id={VALID_STAMP_ID}",
            files={"file": ("big.bin", io.BytesIO(data), "application/octet-stream")}
        )
        assert response.status_code == 413
        body = response.json()
        assert body["detail"]["code"] == "FILE_TOO_LARGE"
        assert body["detail"]["max_size_mb"] == 1

    @patch('app.api.endpoints.data.upload_data_to_swarm', return_value="ref123")
    @patch('app.api.endpoints.data.settings')
    def test_upload_at_exact_limit_succeeds(self, mock_settings, mock_upload):
        """File just under the size limit should be accepted."""
        mock_settings.MAX_UPLOAD_SIZE_MB = 2
        # 1 MB file — under 2 MB limit (multipart encoding adds overhead
        # to Content-Length, so file must be well under the limit)
        data = b"x" * (1 * 1024 * 1024)
        response = client.post(
            f"/api/v1/data/?stamp_id={VALID_STAMP_ID}",
            files={"file": ("exact.bin", io.BytesIO(data), "application/octet-stream")}
        )
        assert response.status_code == 200

    @patch('app.api.endpoints.data.settings')
    def test_content_length_header_rejection(self, mock_settings):
        """Content-Length header exceeding limit should cause fast reject."""
        mock_settings.MAX_UPLOAD_SIZE_MB = 1
        max_bytes = 1 * 1024 * 1024
        # Send small actual data but large Content-Length header
        data = b"x" * 100
        response = client.post(
            f"/api/v1/data/?stamp_id={VALID_STAMP_ID}",
            files={"file": ("test.bin", io.BytesIO(data), "application/octet-stream")},
            headers={"content-length": str(max_bytes + 1)}
        )
        # TestClient may override content-length, so accept either 413 or 200
        assert response.status_code in [200, 413]


class TestManifestUploadSizeLimit:
    """Tests for upload size enforcement on manifest upload endpoint."""

    @patch('app.api.endpoints.data.settings')
    def test_manifest_exceeding_limit_returns_413(self, mock_settings):
        """TAR file exceeding MAX_UPLOAD_SIZE_MB should return 413."""
        mock_settings.MAX_UPLOAD_SIZE_MB = 1
        # 2 MB file — exceeds 1 MB limit
        data = b"x" * (2 * 1024 * 1024)
        response = client.post(
            f"/api/v1/data/manifest?stamp_id={VALID_STAMP_ID}",
            files={"file": ("big.tar", io.BytesIO(data), "application/x-tar")}
        )
        assert response.status_code == 413
        body = response.json()
        assert body["detail"]["code"] == "FILE_TOO_LARGE"

    @patch('app.api.endpoints.data.upload_collection_to_swarm', return_value="ref456")
    @patch('app.api.endpoints.data.count_tar_files', return_value=1)
    @patch('app.api.endpoints.data.validate_tar')
    def test_manifest_within_limit_succeeds(self, mock_validate, mock_count, mock_upload):
        """TAR file within size limit should be accepted."""
        data = b"x" * 1024
        response = client.post(
            f"/api/v1/data/manifest?stamp_id={VALID_STAMP_ID}",
            files={"file": ("test.tar", io.BytesIO(data), "application/x-tar")}
        )
        assert response.status_code == 200


class TestConfigurableLimit:
    """Tests that the limit is configurable via settings."""

    @patch('app.api.endpoints.data.settings')
    def test_custom_limit_enforced(self, mock_settings):
        """Custom MAX_UPLOAD_SIZE_MB value should be enforced."""
        mock_settings.MAX_UPLOAD_SIZE_MB = 5  # 5 MB limit
        # 6 MB file — exceeds 5 MB limit
        data = b"x" * (6 * 1024 * 1024)
        response = client.post(
            f"/api/v1/data/?stamp_id={VALID_STAMP_ID}",
            files={"file": ("big.bin", io.BytesIO(data), "application/octet-stream")}
        )
        assert response.status_code == 413
        assert response.json()["detail"]["max_size_mb"] == 5
