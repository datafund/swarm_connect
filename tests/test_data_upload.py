# tests/test_data_upload.py
"""
Essential tests for data upload functionality to prevent future regressions.
Tests file uploads, error handling, and basic functionality.
"""
import pytest
import json
import io
from unittest.mock import patch
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


class TestFileUploadBasics:
    """Test basic file upload functionality."""

    @patch('app.services.swarm_api.upload_data_to_swarm')
    def test_successful_json_upload(self, mock_upload):
        """Test successful upload of valid JSON file."""
        mock_upload.return_value = "test_reference_123"

        # Create test JSON file (any structure - no enforcement)
        test_data = {"any": "data", "structure": "works"}
        json_content = json.dumps(test_data).encode('utf-8')

        files = {"file": ("test.json", io.BytesIO(json_content), "application/json")}
        response = client.post(
            "/api/v1/data/?stamp_id=test_stamp&content_type=application/json",
            files=files
        )

        assert response.status_code == 200
        assert response.json()["reference"] == "test_reference_123"
        assert "test.json" in response.json()["message"]
        mock_upload.assert_called_once()

    @patch('app.services.swarm_api.upload_data_to_swarm')
    def test_successful_binary_upload(self, mock_upload):
        """Test successful upload of binary file."""
        mock_upload.return_value = "binary_reference_456"

        # Create test binary content
        binary_content = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00'  # PNG header

        files = {"file": ("test.png", io.BytesIO(binary_content), "image/png")}
        response = client.post(
            "/api/v1/data/?stamp_id=test_stamp&content_type=image/png",
            files=files
        )

        assert response.status_code == 200
        assert response.json()["reference"] == "binary_reference_456"
        mock_upload.assert_called_once()

    @patch('app.services.swarm_api.upload_data_to_swarm')
    def test_upload_with_example_file_structure(self, mock_upload):
        """Test upload with the same structure as example_upload.json."""
        mock_upload.return_value = "example_reference_789"

        # Use the exact structure from example_upload.json
        test_data = {
            "content_hash": "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
            "provenance_standard": "DaTA v1.0.0",
            "encryption": "none",
            "data": {
                "version": "1.0",
                "timestamp": "2024-01-15T10:30:00Z",
                "creator": {
                    "name": "Data Processing Pipeline",
                    "version": "2.1.0",
                    "identifier": "0x1234567890abcdef"
                }
            },
            "stamp_id": "0xfe2f8b4c1d9e7a5f3b8e2d6c4a9f1e8d7c5b3a1f9e7d5b3a1c8e6f4a2d9b7c3a1"
        }
        json_content = json.dumps(test_data, indent=2).encode('utf-8')

        files = {"file": ("example_upload.json", io.BytesIO(json_content), "application/json")}
        response = client.post(
            "/api/v1/data/?stamp_id=test_stamp&content_type=application/json",
            files=files
        )

        assert response.status_code == 200
        mock_upload.assert_called_once()


class TestErrorHandling:
    """Test error handling and validation."""

    def test_missing_stamp_id(self):
        """Test upload without stamp_id parameter."""
        test_data = {"test": "data"}
        json_content = json.dumps(test_data).encode('utf-8')

        files = {"file": ("test.json", io.BytesIO(json_content), "application/json")}
        response = client.post("/api/v1/data/", files=files)

        assert response.status_code == 422  # Validation error

    def test_missing_file(self):
        """Test upload without file."""
        response = client.post("/api/v1/data/?stamp_id=test_stamp")

        assert response.status_code == 422  # Validation error

    def test_empty_file(self):
        """Test upload with empty file."""
        files = {"file": ("empty.json", io.BytesIO(b""), "application/json")}
        response = client.post(
            "/api/v1/data/?stamp_id=test_stamp&content_type=application/json",
            files=files
        )

        # Should accept empty file (let Swarm handle it)
        assert response.status_code in [200, 502]  # Success or Swarm error

    def test_malformed_json(self):
        """Test upload with malformed JSON."""
        malformed_json = b'{"test": "data", invalid}'

        files = {"file": ("bad.json", io.BytesIO(malformed_json), "application/json")}
        response = client.post(
            "/api/v1/data/?stamp_id=test_stamp&content_type=application/json",
            files=files
        )

        # Should still upload (gateway doesn't validate JSON structure - that's correct!)
        assert response.status_code in [200, 502]

    @patch('app.services.swarm_api.upload_data_to_swarm')
    def test_swarm_api_error(self, mock_upload):
        """Test handling of Swarm API errors."""
        from requests.exceptions import RequestException
        mock_upload.side_effect = RequestException("Swarm API unavailable")

        test_data = {"test": "data"}
        json_content = json.dumps(test_data).encode('utf-8')

        files = {"file": ("test.json", io.BytesIO(json_content), "application/json")}
        response = client.post(
            "/api/v1/data/?stamp_id=test_stamp&content_type=application/json",
            files=files
        )

        assert response.status_code == 502
        assert "Failed to upload data to Swarm" in response.json()["detail"]

    @patch('app.services.swarm_api.upload_data_to_swarm')
    def test_unexpected_error(self, mock_upload):
        """Test handling of unexpected errors."""
        mock_upload.side_effect = Exception("Unexpected error")

        test_data = {"test": "data"}
        json_content = json.dumps(test_data).encode('utf-8')

        files = {"file": ("test.json", io.BytesIO(json_content), "application/json")}
        response = client.post(
            "/api/v1/data/?stamp_id=test_stamp&content_type=application/json",
            files=files
        )

        assert response.status_code == 500
        assert "Internal server error" in response.json()["detail"]


class TestFileNameHandling:
    """Test various file name scenarios."""

    @patch('app.services.swarm_api.upload_data_to_swarm')
    def test_file_with_special_characters(self, mock_upload):
        """Test file names with special characters."""
        mock_upload.return_value = "special_ref_123"

        test_data = {"test": "data"}
        json_content = json.dumps(test_data).encode('utf-8')

        special_names = [
            "file with spaces.json",
            "file-with-dashes.json",
            "file_with_underscores.json",
            "file.with.dots.json",
            "unicode-файл-🚀.json"
        ]

        for filename in special_names:
            files = {"file": (filename, io.BytesIO(json_content), "application/json")}
            response = client.post(
                "/api/v1/data/?stamp_id=test_stamp&content_type=application/json",
                files=files
            )

            assert response.status_code == 200, f"Failed for filename: {filename}"
            assert filename in response.json()["message"]

    @patch('app.services.swarm_api.upload_data_to_swarm')
    def test_file_without_extension(self, mock_upload):
        """Test file without extension."""
        mock_upload.return_value = "no_ext_ref_456"

        test_data = {"test": "data"}
        json_content = json.dumps(test_data).encode('utf-8')

        files = {"file": ("datafile", io.BytesIO(json_content), "application/json")}
        response = client.post(
            "/api/v1/data/?stamp_id=test_stamp&content_type=application/json",
            files=files
        )

        assert response.status_code == 200


class TestStampIdValidation:
    """Test stamp ID validation and edge cases."""

    @patch('app.services.swarm_api.upload_data_to_swarm')
    def test_various_stamp_id_formats(self, mock_upload):
        """Test various stamp ID formats."""
        mock_upload.return_value = "stamp_test_ref"

        test_data = {"test": "data"}
        json_content = json.dumps(test_data).encode('utf-8')

        stamp_ids = [
            "simple_stamp",
            "000de42079daebd58347bb38ce05bdc477701d93651d3bba318a9aee3fbd786a",  # 64 char hex
            "0x1234567890abcdef",  # Hex with prefix
            "stamp-with-dashes",
            "stamp_with_underscores",
            "UPPERCASE_STAMP",
            "123456789"  # Numeric
        ]

        for stamp_id in stamp_ids:
            files = {"file": ("test.json", io.BytesIO(json_content), "application/json")}
            response = client.post(
                f"/api/v1/data/?stamp_id={stamp_id}&content_type=application/json",
                files=files
            )

            assert response.status_code == 200, f"Failed for stamp_id: {stamp_id}"

    def test_empty_stamp_id(self):
        """Test with empty stamp ID."""
        test_data = {"test": "data"}
        json_content = json.dumps(test_data).encode('utf-8')

        files = {"file": ("test.json", io.BytesIO(json_content), "application/json")}
        response = client.post(
            "/api/v1/data/?stamp_id=&content_type=application/json",
            files=files
        )

        # Should be rejected due to empty stamp_id
        assert response.status_code in [422, 400, 502]


class TestContentTypeHandling:
    """Test content type validation and handling."""

    @patch('app.services.swarm_api.upload_data_to_swarm')
    def test_various_content_types(self, mock_upload):
        """Test various content types."""
        mock_upload.return_value = "content_type_ref"

        test_data = {"test": "data"}
        json_content = json.dumps(test_data).encode('utf-8')

        content_types = [
            "application/json",
            "application/octet-stream",
            "text/plain",
            "image/png",
            "application/pdf",
            "custom/type"
        ]

        for content_type in content_types:
            files = {"file": ("test.json", io.BytesIO(json_content), "application/json")}
            response = client.post(
                f"/api/v1/data/?stamp_id=test_stamp&content_type={content_type}",
                files=files
            )

            assert response.status_code == 200, f"Failed for content-type: {content_type}"

    def test_invalid_content_type_format(self):
        """Test with invalid content-type format."""
        test_data = {"test": "data"}
        json_content = json.dumps(test_data).encode('utf-8')

        files = {"file": ("test.json", io.BytesIO(json_content), "application/json")}
        response = client.post(
            "/api/v1/data/?stamp_id=test_stamp&content_type=invalid-content-type",
            files=files
        )

        # Should still work (Swarm accepts any content-type string)
        assert response.status_code in [200, 502]


# TODO: Add performance tests for large files when needed:
# - Large JSON files (1MB+)
# - Large binary files (1MB+)
# - Memory usage monitoring
# - Upload timeout handling

# TODO: Add concurrent upload tests when needed:
# - Multiple simultaneous uploads
# - Race condition testing
# - Thread safety verification

# TODO: Add SWIP compliance validation tests when/if enforcement is added:
# - Required field validation
# - Schema validation
# - Version compatibility checks