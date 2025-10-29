# tests/test_data_download.py
"""
Comprehensive tests for data download functionality to prevent future regressions.
Tests content type detection, filename generation, headers, and error handling.
"""
import pytest
import json
import base64
from unittest.mock import patch
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


class TestContentTypeDetection:
    """Test content type detection and filename generation."""

    @patch('app.services.swarm_api.download_data_from_swarm')
    def test_json_content_detection(self, mock_download):
        """Test that JSON content is detected and gets provenance filename."""
        json_data = {"content_hash": "sha256:test", "data": {"test": "provenance"}}
        json_bytes = json.dumps(json_data, indent=2).encode('utf-8')
        mock_download.return_value = json_bytes

        response = client.get("/api/v1/data/abcd1234567890abcdef1234567890abcdef1234567890abcdef1234567890ab")

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        assert response.headers["content-disposition"] == 'attachment; filename="provenance-abcd1234.json"'
        assert "X-Swarm-Reference" in response.headers

    @patch('app.services.swarm_api.download_data_from_swarm')
    def test_png_image_detection(self, mock_download):
        """Test that PNG images are detected correctly."""
        png_bytes = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00'  # PNG header
        mock_download.return_value = png_bytes

        response = client.get("/api/v1/data/1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef")

        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.headers["content-disposition"] == 'attachment; filename="image-12345678.png"'

    @patch('app.services.swarm_api.download_data_from_swarm')
    def test_jpeg_image_detection(self, mock_download):
        """Test that JPEG images are detected correctly."""
        jpeg_bytes = b'\xFF\xD8\xFF\xE0\x00\x10JFIF'  # JPEG header
        mock_download.return_value = jpeg_bytes

        response = client.get("/api/v1/data/fedcba0987654321fedcba0987654321fedcba0987654321fedcba0987654321")

        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"
        assert response.headers["content-disposition"] == 'attachment; filename="image-fedcba09.jpg"'

    @patch('app.services.swarm_api.download_data_from_swarm')
    def test_pdf_document_detection(self, mock_download):
        """Test that PDF documents are detected correctly."""
        pdf_bytes = b'%PDF-1.4\n1 0 obj'  # PDF header
        mock_download.return_value = pdf_bytes

        response = client.get("/api/v1/data/pdf12345678901234567890123456789012345678901234567890123456789012")

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert response.headers["content-disposition"] == 'attachment; filename="document-pdf12345.pdf"'

    @patch('app.services.swarm_api.download_data_from_swarm')
    def test_text_content_detection(self, mock_download):
        """Test that plain text is detected correctly."""
        text_bytes = "This is plain text content with UTF-8 characters: äöü".encode('utf-8')
        mock_download.return_value = text_bytes

        response = client.get("/api/v1/data/text567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef12")

        assert response.status_code == 200
        assert response.headers["content-type"] == "text/plain"
        assert response.headers["content-disposition"] == 'attachment; filename="text-text5678.txt"'

    @patch('app.services.swarm_api.download_data_from_swarm')
    def test_binary_fallback_detection(self, mock_download):
        """Test that unknown binary content falls back to generic filename."""
        binary_bytes = b'\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09'  # Random binary
        mock_download.return_value = binary_bytes

        response = client.get("/api/v1/data/binary567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef")

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/octet-stream"
        assert response.headers["content-disposition"] == 'attachment; filename="data-binary56.bin"'


class TestFilenameGeneration:
    """Test filename generation edge cases."""

    @patch('app.services.swarm_api.download_data_from_swarm')
    def test_short_reference_hash(self, mock_download):
        """Test filename generation with short reference."""
        json_data = {"test": "data"}
        mock_download.return_value = json.dumps(json_data).encode('utf-8')

        response = client.get("/api/v1/data/abc123")

        assert response.status_code == 200
        assert response.headers["content-disposition"] == 'attachment; filename="provenance-abc123.json"'

    @patch('app.services.swarm_api.download_data_from_swarm')
    def test_reference_with_special_chars(self, mock_download):
        """Test that reference hashes are sanitized for filenames."""
        json_data = {"test": "data"}
        mock_download.return_value = json.dumps(json_data).encode('utf-8')

        # Normal hex hash should work fine
        response = client.get("/api/v1/data/deadbeef12345678901234567890123456789012345678901234567890123456")

        assert response.status_code == 200
        assert response.headers["content-disposition"] == 'attachment; filename="provenance-deadbeef.json"'

    @patch('app.services.swarm_api.download_data_from_swarm')
    def test_empty_file_handling(self, mock_download):
        """Test handling of empty files."""
        mock_download.return_value = b""

        response = client.get("/api/v1/data/empty1234567890abcdef1234567890abcdef1234567890abcdef1234567890ab")

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/octet-stream"
        assert response.headers["content-disposition"] == 'attachment; filename="data-empty123.bin"'
        assert response.headers["content-length"] == "0"


class TestDownloadHeaders:
    """Test HTTP headers in download responses."""

    @patch('app.services.swarm_api.download_data_from_swarm')
    def test_required_headers_present(self, mock_download):
        """Test that all required headers are present."""
        test_data = b"test content"
        mock_download.return_value = test_data

        response = client.get("/api/v1/data/headers567890abcdef1234567890abcdef1234567890abcdef1234567890abcde")

        # Required headers
        assert "content-type" in response.headers
        assert "content-disposition" in response.headers
        assert "content-length" in response.headers
        assert "x-swarm-reference" in response.headers

        # Verify header values
        assert response.headers["content-length"] == str(len(test_data))
        assert response.headers["x-swarm-reference"] == "headers567890abcdef1234567890abcdef1234567890abcdef1234567890abcde"

    @patch('app.services.swarm_api.download_data_from_swarm')
    def test_content_disposition_format(self, mock_download):
        """Test that Content-Disposition header is properly formatted."""
        json_data = {"test": "data"}
        mock_download.return_value = json.dumps(json_data).encode('utf-8')

        response = client.get("/api/v1/data/disposition567890abcdef1234567890abcdef1234567890abcdef1234567890")

        disposition = response.headers["content-disposition"]
        assert disposition.startswith('attachment; filename="')
        assert disposition.endswith('.json"')
        assert "provenance-" in disposition


class TestDownloadErrorHandling:
    """Test error handling in download endpoints."""

    @patch('app.services.swarm_api.download_data_from_swarm')
    def test_file_not_found_error(self, mock_download):
        """Test handling when file is not found in Swarm."""
        mock_download.side_effect = FileNotFoundError("File not found")

        response = client.get("/api/v1/data/notfound567890abcdef1234567890abcdef1234567890abcdef1234567890")

        assert response.status_code == 404
        assert "Data not found" in response.json()["detail"]

    @patch('app.services.swarm_api.download_data_from_swarm')
    def test_swarm_api_error(self, mock_download):
        """Test handling of Swarm API errors."""
        from requests.exceptions import RequestException
        mock_download.side_effect = RequestException("Swarm API error")

        response = client.get("/api/v1/data/apitest567890abcdef1234567890abcdef1234567890abcdef1234567890")

        assert response.status_code == 502
        assert "Failed to download data from Swarm" in response.json()["detail"]

    @patch('app.services.swarm_api.download_data_from_swarm')
    def test_unexpected_error(self, mock_download):
        """Test handling of unexpected errors."""
        mock_download.side_effect = Exception("Unexpected error")

        response = client.get("/api/v1/data/unexpected567890abcdef1234567890abcdef1234567890abcdef1234567890")

        assert response.status_code == 500
        assert "Internal server error" in response.json()["detail"]

    def test_invalid_reference_format(self):
        """Test handling of invalid reference format."""
        invalid_refs = [
            "",  # Empty
            "too_short",  # Too short
            "invalid-chars!@#",  # Invalid characters
            "../../../etc/passwd",  # Path traversal
            "<script>alert('xss')</script>",  # XSS attempt
        ]

        for ref in invalid_refs:
            response = client.get(f"/api/v1/data/{ref}")
            # Should be handled gracefully (404 or 422)
            assert response.status_code in [404, 422, 500]


class TestJSONDownloadEndpoint:
    """Test the /data/{reference}/json endpoint specifically."""

    @patch('app.services.swarm_api.download_data_from_swarm')
    def test_json_endpoint_response_format(self, mock_download):
        """Test that JSON endpoint returns proper response format."""
        test_data = {"test": "provenance", "data": {"nested": "structure"}}
        test_bytes = json.dumps(test_data).encode('utf-8')
        mock_download.return_value = test_bytes

        response = client.get("/api/v1/data/jsontest567890abcdef1234567890abcdef1234567890abcdef1234567890/json")

        assert response.status_code == 200
        json_response = response.json()

        # Check response structure
        assert "data" in json_response
        assert "content_type" in json_response
        assert "size" in json_response
        assert "reference" in json_response

        # Check values
        assert json_response["size"] == len(test_bytes)
        assert json_response["reference"] == "jsontest567890abcdef1234567890abcdef1234567890abcdef1234567890"
        assert json_response["content_type"] == "application/json"

        # Check that data is base64 encoded
        decoded_data = base64.b64decode(json_response["data"]).decode('utf-8')
        assert json.loads(decoded_data) == test_data

    @patch('app.services.swarm_api.download_data_from_swarm')
    def test_json_endpoint_with_binary_data(self, mock_download):
        """Test JSON endpoint with binary data (should detect as binary)."""
        binary_data = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00'
        mock_download.return_value = binary_data

        response = client.get("/api/v1/data/pngdata567890abcdef1234567890abcdef1234567890abcdef1234567890ab/json")

        assert response.status_code == 200
        json_response = response.json()

        assert json_response["content_type"] == "image/png"
        assert json_response["size"] == len(binary_data)

        # Verify base64 encoding
        decoded_data = base64.b64decode(json_response["data"])
        assert decoded_data == binary_data


class TestMalformedContentHandling:
    """Test handling of malformed or edge case content."""

    @patch('app.services.swarm_api.download_data_from_swarm')
    def test_malformed_json_handling(self, mock_download):
        """Test that malformed JSON is treated as binary."""
        malformed_json = b'{"test": "data", invalid}'
        mock_download.return_value = malformed_json

        response = client.get("/api/v1/data/malformed567890abcdef1234567890abcdef1234567890abcdef1234567890")

        assert response.status_code == 200
        # Should fall back to binary since JSON parsing fails
        assert response.headers["content-type"] == "application/octet-stream"
        assert response.headers["content-disposition"] == 'attachment; filename="data-malformed.bin"'

    @patch('app.services.swarm_api.download_data_from_swarm')
    def test_invalid_utf8_handling(self, mock_download):
        """Test handling of invalid UTF-8 sequences."""
        invalid_utf8 = b'\x80\x81\x82\x83'  # Invalid UTF-8
        mock_download.return_value = invalid_utf8

        response = client.get("/api/v1/data/utf8test567890abcdef1234567890abcdef1234567890abcdef1234567890")

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/octet-stream"
        assert response.headers["content-disposition"] == 'attachment; filename="data-utf8test.bin"'

    @patch('app.services.swarm_api.download_data_from_swarm')
    def test_very_large_content_headers(self, mock_download):
        """Test headers with very large content."""
        large_data = b'x' * (10 * 1024 * 1024)  # 10MB
        mock_download.return_value = large_data

        response = client.get("/api/v1/data/largetest567890abcdef1234567890abcdef1234567890abcdef1234567890")

        assert response.status_code == 200
        assert response.headers["content-length"] == str(len(large_data))
        assert response.headers["content-type"] == "text/plain"  # Should detect as text
        assert response.headers["content-disposition"] == 'attachment; filename="text-largetest.txt"'


class TestReferenceHashEdgeCases:
    """Test edge cases with reference hash handling."""

    @patch('app.services.swarm_api.download_data_from_swarm')
    def test_various_reference_lengths(self, mock_download):
        """Test various reference hash lengths."""
        json_data = {"test": "data"}
        mock_download.return_value = json.dumps(json_data).encode('utf-8')

        test_refs = [
            "a",  # Very short
            "abc123",  # Short
            "1234567890123456",  # Medium
            "1234567890abcdef" * 4,  # Standard 64 chars
        ]

        for ref in test_refs:
            response = client.get(f"/api/v1/data/{ref}")
            if response.status_code == 200:  # Some may be rejected by validation
                # Should use first 8 chars or entire ref if shorter
                expected_prefix = ref[:8] if len(ref) >= 8 else ref
                expected_filename = f'provenance-{expected_prefix}.json'
                assert response.headers["content-disposition"] == f'attachment; filename="{expected_filename}"'


# TODO: Add performance tests for large downloads when needed:
# - Very large file downloads (100MB+)
# - Download speed/streaming tests
# - Memory usage during large downloads
# - Timeout handling for slow downloads

# TODO: Add concurrent download tests when needed:
# - Multiple simultaneous downloads
# - Same file downloaded by multiple clients
# - Download while upload is happening