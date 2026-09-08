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
        """A file of exactly MAX_UPLOAD_SIZE_MB is accepted.

        This test used to send 1 MB against a 2 MB limit and call that "exact",
        with a comment explaining that multipart overhead meant the file had to
        be "well under the limit". That comment described the bug: Content-Length
        includes the multipart envelope, and it was being compared against the
        limit that applies to the file, so the advertised ceiling was unreachable
        by a few hundred bytes. Testing a value nowhere near the boundary is what
        let it survive.
        """
        mock_settings.MAX_UPLOAD_SIZE_MB = 2
        data = b"x" * (2 * 1024 * 1024)
        response = client.post(
            f"/api/v1/data/?stamp_id={VALID_STAMP_ID}",
            files={"file": ("exact.bin", io.BytesIO(data), "application/octet-stream")}
        )
        assert response.status_code == 200, response.text

    @patch('app.api.endpoints.data.upload_data_to_swarm', return_value="ref123")
    @patch('app.api.endpoints.data.settings')
    def test_one_byte_over_the_limit_is_still_rejected(self, mock_settings, mock_upload):
        """The envelope allowance must not become slack in the limit itself.

        Content-Length gets an 8 KB allowance so the envelope does not count
        against the file, but the file's own length is still measured exactly.
        """
        mock_settings.MAX_UPLOAD_SIZE_MB = 2
        data = b"x" * (2 * 1024 * 1024 + 1)
        response = client.post(
            f"/api/v1/data/?stamp_id={VALID_STAMP_ID}",
            files={"file": ("over.bin", io.BytesIO(data), "application/octet-stream")}
        )
        assert response.status_code == 413
        assert response.json()["detail"]["code"] == "FILE_TOO_LARGE"

    @patch('app.api.endpoints.data.upload_data_to_swarm', return_value="ref123")
    @patch('app.api.endpoints.data.settings')
    def test_a_long_filename_does_not_eat_into_the_limit(self, mock_settings, mock_upload):
        """The envelope varies with the filename, so the ceiling must not.

        A caller uploading a file at the limit should not be rejected because
        the name they chose is long.
        """
        mock_settings.MAX_UPLOAD_SIZE_MB = 2
        data = b"x" * (2 * 1024 * 1024)
        response = client.post(
            f"/api/v1/data/?stamp_id={VALID_STAMP_ID}",
            files={"file": ("a" * 200 + ".bin", io.BytesIO(data), "application/octet-stream")}
        )
        assert response.status_code == 200, response.text

    @patch('app.api.endpoints.data.settings')
    def test_content_length_header_rejection(self, mock_settings):
        """A declared Content-Length well over the limit is rejected.

        The declared length must exceed the limit by more than the envelope
        allowance, or this asserts nothing: a value one byte over now falls
        inside the slack that exists so the multipart wrapper does not count
        against the file.
        """
        mock_settings.MAX_UPLOAD_SIZE_MB = 1
        max_bytes = 1 * 1024 * 1024
        data = b"x" * 100
        response = client.post(
            f"/api/v1/data/?stamp_id={VALID_STAMP_ID}",
            files={"file": ("test.bin", io.BytesIO(data), "application/octet-stream")},
            headers={"content-length": str(max_bytes * 2)}
        )
        assert response.status_code == 413
        assert response.json()["detail"]["code"] == "FILE_TOO_LARGE"


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
