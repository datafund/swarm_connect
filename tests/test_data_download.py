# tests/test_data_download.py
"""
Comprehensive tests for data download functionality to prevent future regressions.
Tests content type detection, filename generation, headers, and error handling.
"""
import pytest
import httpx
import json
import base64
from unittest.mock import patch
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

# Valid 64-char hex reference for reuse in tests
VALID_REF = "a" * 64


class TestContentTypeDetection:
    """Test content type detection and filename generation."""

    @patch('app.api.endpoints.data.download_data_from_swarm')
    def test_json_content_detection(self, mock_download):
        """Test that JSON content is detected and gets provenance filename."""
        json_data = {"content_hash": "sha256:test", "data": {"test": "provenance"}}
        json_bytes = json.dumps(json_data, indent=2).encode('utf-8')
        mock_download.return_value = json_bytes

        response = client.get("/api/v1/data/abcd1234567890abcdef1234567890abcdef1234567890abcdef12345678abcd")

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        assert response.headers["content-disposition"] == 'attachment; filename="provenance-abcd1234.json"'
        assert "X-Swarm-Reference" in response.headers

    @patch('app.api.endpoints.data.download_data_from_swarm')
    def test_png_image_detection(self, mock_download):
        """Test that PNG images are detected correctly."""
        png_bytes = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00'  # PNG header
        mock_download.return_value = png_bytes

        response = client.get("/api/v1/data/1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef")

        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.headers["content-disposition"] == 'attachment; filename="image-12345678.png"'

    @patch('app.api.endpoints.data.download_data_from_swarm')
    def test_jpeg_image_detection(self, mock_download):
        """Test that JPEG images are detected correctly."""
        jpeg_bytes = b'\xFF\xD8\xFF\xE0\x00\x10JFIF'  # JPEG header
        mock_download.return_value = jpeg_bytes

        response = client.get("/api/v1/data/fedcba0987654321fedcba0987654321fedcba0987654321fedcba0987654321")

        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"
        assert response.headers["content-disposition"] == 'attachment; filename="image-fedcba09.jpg"'

    @patch('app.api.endpoints.data.download_data_from_swarm')
    def test_pdf_document_detection(self, mock_download):
        """Test that PDF documents are detected correctly."""
        pdf_bytes = b'%PDF-1.4\n1 0 obj'  # PDF header
        mock_download.return_value = pdf_bytes

        response = client.get("/api/v1/data/ddf1234567890abcdef1234567890abcdef1234567890abcdef1234567890abc")

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert response.headers["content-disposition"] == 'attachment; filename="document-ddf12345.pdf"'

    @patch('app.api.endpoints.data.download_data_from_swarm')
    def test_text_content_detection(self, mock_download):
        """Test that plain text is detected correctly."""
        text_bytes = "This is plain text content with UTF-8 characters: äöü".encode('utf-8')
        mock_download.return_value = text_bytes

        response = client.get("/api/v1/data/eee1567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        assert response.headers["content-disposition"] == 'attachment; filename="text-eee15678.txt"'

    @patch('app.api.endpoints.data.download_data_from_swarm')
    def test_binary_fallback_detection(self, mock_download):
        """Test that truly non-UTF-8 binary content falls back to octet-stream."""
        binary_bytes = b'\x80\x81\x82\x83\x84\x85\x86\x87\x88\x89'  # Invalid UTF-8
        mock_download.return_value = binary_bytes

        response = client.get("/api/v1/data/fff0567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef")

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/octet-stream"
        assert response.headers["content-disposition"] == 'attachment; filename="data-fff05678.bin"'


class TestFilenameGeneration:
    """Test filename generation edge cases."""

    @patch('app.api.endpoints.data.download_data_from_swarm')
    def test_short_reference_hash(self, mock_download):
        """Test that short references are rejected by regex validation."""
        json_data = {"test": "data"}
        mock_download.return_value = json.dumps(json_data).encode('utf-8')

        response = client.get("/api/v1/data/abc123")

        # Short references fail the 64-128 hex char regex validation
        assert response.status_code == 422

    @patch('app.api.endpoints.data.download_data_from_swarm')
    def test_reference_with_special_chars(self, mock_download):
        """Test that reference hashes are sanitized for filenames."""
        json_data = {"test": "data"}
        mock_download.return_value = json.dumps(json_data).encode('utf-8')

        # Normal hex hash should work fine
        response = client.get("/api/v1/data/deadbeef12345678901234567890123456789012345678901234567890123456")

        assert response.status_code == 200
        assert response.headers["content-disposition"] == 'attachment; filename="provenance-deadbeef.json"'

    @patch('app.api.endpoints.data.download_data_from_swarm')
    def test_empty_file_handling(self, mock_download):
        """Test handling of empty files."""
        mock_download.return_value = b""

        response = client.get("/api/v1/data/e0001234567890abcdef1234567890abcdef1234567890abcdef1234567890ab")

        assert response.status_code == 200
        # Empty bytes decode as valid UTF-8, so detected as text/plain
        assert response.headers["content-type"].startswith("text/plain")
        assert response.headers["content-disposition"] == 'attachment; filename="text-e0001234.txt"'
        assert response.headers["content-length"] == "0"


class TestDownloadHeaders:
    """Test HTTP headers in download responses."""

    @patch('app.api.endpoints.data.download_data_from_swarm')
    def test_required_headers_present(self, mock_download):
        """Test that all required headers are present."""
        test_data = b"test content"
        mock_download.return_value = test_data

        response = client.get("/api/v1/data/aaa1567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef")

        # Required headers
        assert "content-type" in response.headers
        assert "content-disposition" in response.headers
        assert "content-length" in response.headers
        assert "x-swarm-reference" in response.headers

        # Verify header values
        assert response.headers["content-length"] == str(len(test_data))
        assert response.headers["x-swarm-reference"] == "aaa1567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"

    @patch('app.api.endpoints.data.download_data_from_swarm')
    def test_content_disposition_format(self, mock_download):
        """Test that Content-Disposition header is properly formatted."""
        json_data = {"test": "data"}
        mock_download.return_value = json.dumps(json_data).encode('utf-8')

        response = client.get("/api/v1/data/bbb1567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef")

        disposition = response.headers["content-disposition"]
        assert disposition.startswith('attachment; filename="')
        assert disposition.endswith('.json"')
        assert "provenance-" in disposition


class TestDownloadErrorHandling:
    """Test error handling in download endpoints."""

    @patch('app.api.endpoints.data.download_data_from_swarm')
    def test_file_not_found_error(self, mock_download):
        """Test handling when file is not found in Swarm."""
        mock_download.side_effect = FileNotFoundError("File not found")

        response = client.get("/api/v1/data/ccc1567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef")

        assert response.status_code == 404
        assert "Data not found" in response.json()["detail"]

    @patch('app.api.endpoints.data.download_data_from_swarm')
    def test_swarm_api_error(self, mock_download):
        """Test handling of Swarm API errors."""
        mock_download.side_effect = httpx.HTTPError("Swarm API error")

        response = client.get("/api/v1/data/ddd1567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef")

        assert response.status_code == 502
        assert "Failed to download data from Swarm" in response.json()["detail"]

    @patch('app.api.endpoints.data.download_data_from_swarm')
    def test_unexpected_error(self, mock_download):
        """Test handling of unexpected errors."""
        mock_download.side_effect = Exception("Unexpected error")

        response = client.get("/api/v1/data/eee0567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef")

        assert response.status_code == 500
        assert "Internal server error" in response.json()["detail"]

    @patch('app.api.endpoints.data.download_data_from_swarm')
    def test_invalid_reference_format(self, mock_download):
        """Test handling of invalid reference format - rejected by regex validation."""
        mock_download.side_effect = httpx.HTTPError("Bad request")

        # These refs are rejected by the 64-128 hex char regex with 422
        invalid_refs = [
            "too_short",  # Too short and non-hex
            "abc123",  # Too short (< 64 chars)
            "zzzz" * 16,  # 64 chars but non-hex
            "invalid" + "a" * 57,  # 64 chars but contains non-hex prefix
        ]

        for ref in invalid_refs:
            response = client.get(f"/api/v1/data/{ref}")
            assert response.status_code == 422, f"Expected 422 for ref '{ref}', got {response.status_code}"

        # Refs with special chars that break URL routing get 404 (path resolution)
        special_refs = [
            "../../../etc/passwd",  # Path traversal
            "<script>alert('xss')</script>",  # XSS attempt
            "invalid-chars!@#",  # Invalid characters with special URL chars
        ]

        for ref in special_refs:
            response = client.get(f"/api/v1/data/{ref}")
            assert response.status_code in [404, 422], f"Expected 404 or 422 for ref '{ref}', got {response.status_code}"

        # Empty string routes to a different URL pattern (405 Method Not Allowed)
        response = client.get("/api/v1/data/")
        assert response.status_code == 405


class TestJSONDownloadEndpoint:
    """Test the /data/{reference}/json endpoint specifically."""

    @patch('app.api.endpoints.data.download_data_from_swarm')
    def test_json_endpoint_response_format(self, mock_download):
        """Test that JSON endpoint returns proper response format."""
        test_data = {"test": "provenance", "data": {"nested": "structure"}}
        test_bytes = json.dumps(test_data).encode('utf-8')
        mock_download.return_value = test_bytes

        response = client.get("/api/v1/data/fff1567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef/json")

        assert response.status_code == 200
        json_response = response.json()

        # Check response structure
        assert "data" in json_response
        assert "content_type" in json_response
        assert "size" in json_response
        assert "reference" in json_response

        # Check values
        assert json_response["size"] == len(test_bytes)
        assert json_response["reference"] == "fff1567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"
        assert json_response["content_type"] == "application/json"

        # Check that data is base64 encoded
        decoded_data = base64.b64decode(json_response["data"]).decode('utf-8')
        assert json.loads(decoded_data) == test_data

    @patch('app.api.endpoints.data.download_data_from_swarm')
    def test_json_endpoint_with_binary_data(self, mock_download):
        """Test JSON endpoint with binary data (should detect as binary)."""
        binary_data = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00'
        mock_download.return_value = binary_data

        response = client.get("/api/v1/data/aab1567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef/json")

        assert response.status_code == 200
        json_response = response.json()

        assert json_response["content_type"] == "image/png"
        assert json_response["size"] == len(binary_data)

        # Verify base64 encoding
        decoded_data = base64.b64decode(json_response["data"])
        assert decoded_data == binary_data

    @patch('app.api.endpoints.data.download_data_from_swarm')
    def test_json_endpoint_response_structure_validation(self, mock_download):
        """Test that all response fields have correct types and are present."""
        test_data = b"test content"
        mock_download.return_value = test_data

        response = client.get("/api/v1/data/aac1567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef/json")

        assert response.status_code == 200
        json_response = response.json()

        # Validate field types
        assert isinstance(json_response["data"], str), "data field should be string (base64)"
        assert isinstance(json_response["content_type"], str), "content_type should be string"
        assert isinstance(json_response["size"], int), "size should be integer"
        assert isinstance(json_response["reference"], str), "reference should be string"

        # Validate field values
        assert len(json_response["data"]) > 0, "data field should not be empty"
        assert json_response["size"] > 0, "size should be positive"
        assert json_response["content_type"] in ["text/plain", "application/octet-stream"], "should detect as text or binary"

    @patch('app.api.endpoints.data.download_data_from_swarm')
    def test_base64_encoding_integrity(self, mock_download):
        """Test that base64 encoding preserves data integrity perfectly."""
        # Test various data types to ensure no corruption
        test_cases = [
            ("JSON", json.dumps({"test": "data", "unicode": "测试🚀"}).encode('utf-8')),
            ("Binary", b'\x00\x01\x02\x03\xFF\xFE\xFD\xFC'),
            ("PNG", b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x01\x00\x01\x00'),
            ("UTF-8 text", "Hello 世界! 🌍 Testing Unicode".encode('utf-8')),
            ("Large binary", b'\xAA' * 10000),  # Large repetitive data
            ("Mixed binary", bytes(range(256))),  # All possible byte values
        ]

        for idx, (test_name, original_data) in enumerate(test_cases):
            mock_download.return_value = original_data
            ref = f"{idx:04d}" + "aad1567890abcdef1234567890abcdef1234567890abcdef12345678abcd"

            response = client.get(f"/api/v1/data/{ref}/json")

            assert response.status_code == 200, f"Failed for {test_name}"
            json_response = response.json()

            # Decode and verify integrity
            decoded_data = base64.b64decode(json_response["data"])
            assert decoded_data == original_data, f"Data corruption in {test_name}"
            assert json_response["size"] == len(original_data), f"Size mismatch in {test_name}"

    @patch('app.api.endpoints.data.download_data_from_swarm')
    def test_json_endpoint_error_responses(self, mock_download):
        """Test that JSON endpoint returns proper JSON error responses."""
        # Test 404 error
        mock_download.side_effect = FileNotFoundError("File not found")

        response = client.get("/api/v1/data/aae1567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef/json")

        assert response.status_code == 404
        assert response.headers["content-type"] == "application/json"
        error_json = response.json()
        assert "detail" in error_json
        assert "Data not found" in error_json["detail"]

        # Test 502 error
        mock_download.side_effect = httpx.HTTPError("Swarm error")

        response = client.get("/api/v1/data/aaf1567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef/json")

        assert response.status_code == 502
        assert response.headers["content-type"] == "application/json"
        error_json = response.json()
        assert "detail" in error_json
        assert "Failed to download data from Swarm" in error_json["detail"]

    @patch('app.api.endpoints.data.download_data_from_swarm')
    def test_json_vs_raw_endpoint_consistency(self, mock_download):
        """Test that JSON and raw endpoints detect content types consistently."""
        test_cases = [
            ("JSON data", json.dumps({"test": "data"}).encode('utf-8'), "application/json"),
            ("PNG image", b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00', "image/png"),
            ("Plain text", "Hello world".encode('utf-8'), "text/plain"),
            ("Binary data", b'\x80\x81\x82\x83', "application/octet-stream"),
        ]

        reference_base = "aab0567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"

        for test_name, test_data, expected_type in test_cases:
            mock_download.return_value = test_data

            # Test raw endpoint
            raw_response = client.get(f"/api/v1/data/{reference_base}")
            assert raw_response.status_code == 200, f"Raw endpoint failed for {test_name}"
            raw_content_type = raw_response.headers["content-type"]

            # Test JSON endpoint
            json_response = client.get(f"/api/v1/data/{reference_base}/json")
            assert json_response.status_code == 200, f"JSON endpoint failed for {test_name}"
            json_content_type = json_response.json()["content_type"]

            # Both should detect the same base content type (raw may include charset)
            raw_base_type = raw_content_type.split(";")[0].strip()
            assert raw_base_type == json_content_type, f"Content type mismatch for {test_name}: raw={raw_base_type}, json={json_content_type}"
            assert raw_base_type == expected_type, f"Wrong content type for {test_name}: expected={expected_type}, got={raw_base_type}"

    @patch('app.api.endpoints.data.download_data_from_swarm')
    def test_json_endpoint_empty_file_handling(self, mock_download):
        """Test JSON endpoint with empty files."""
        mock_download.return_value = b""

        response = client.get("/api/v1/data/aac0567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef/json")

        assert response.status_code == 200
        json_response = response.json()

        assert json_response["size"] == 0
        assert json_response["data"] == ""  # Empty base64
        assert json_response["content_type"] == "text/plain"  # Empty bytes decode as valid UTF-8
        assert json_response["reference"] == "aac0567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"

    @patch('app.api.endpoints.data.download_data_from_swarm')
    def test_json_endpoint_large_file_handling(self, mock_download):
        """Test JSON endpoint with large files (base64 overhead)."""
        # 1MB of data
        large_data = b'A' * (1024 * 1024)
        mock_download.return_value = large_data

        response = client.get("/api/v1/data/aad0567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef/json")

        assert response.status_code == 200
        json_response = response.json()

        assert json_response["size"] == len(large_data)

        # Verify base64 encoding worked for large file
        decoded_data = base64.b64decode(json_response["data"])
        assert len(decoded_data) == len(large_data)
        assert decoded_data == large_data

        # Base64 encoding increases size by ~33%
        base64_size = len(json_response["data"])
        expected_base64_size = (len(large_data) + 2) // 3 * 4  # Base64 encoding formula
        assert base64_size == expected_base64_size

    @patch('app.api.endpoints.data.download_data_from_swarm')
    def test_json_endpoint_unicode_handling(self, mock_download):
        """Test JSON endpoint with Unicode content."""
        unicode_data = {
            "message": "Hello 世界! 🌍",
            "languages": ["English", "中文", "Español", "العربية"],
            "emoji": "🚀🌟💫⭐"
        }
        unicode_bytes = json.dumps(unicode_data, ensure_ascii=False).encode('utf-8')
        mock_download.return_value = unicode_bytes

        response = client.get("/api/v1/data/aae0567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef/json")

        assert response.status_code == 200
        json_response = response.json()

        assert json_response["content_type"] == "application/json"
        assert json_response["size"] == len(unicode_bytes)

        # Verify Unicode is preserved through base64 encoding
        decoded_data = base64.b64decode(json_response["data"]).decode('utf-8')
        decoded_json = json.loads(decoded_data)
        assert decoded_json == unicode_data

    @patch('app.api.endpoints.data.download_data_from_swarm')
    def test_json_endpoint_content_type_accuracy(self, mock_download):
        """Test that JSON endpoint content type detection is accurate."""
        test_cases = [
            # (data, expected_content_type, description)
            (b'\xFF\xD8\xFF\xE0', "image/jpeg", "JPEG header"),
            (b'GIF87a', "image/gif", "GIF87a header"),
            (b'GIF89a', "image/gif", "GIF89a header"),
            (b'%PDF-1.4', "application/pdf", "PDF header"),
            (json.dumps({"valid": "json"}).encode(), "application/json", "Valid JSON"),
            (b'{"invalid": json}', "text/plain", "Invalid JSON but valid UTF-8 is text"),
            ("Plain text content".encode('utf-8'), "text/plain", "UTF-8 text"),
            (b'\x80\x81\x82', "application/octet-stream", "Invalid UTF-8 should be binary"),
        ]

        for idx, (test_data, expected_type, description) in enumerate(test_cases):
            mock_download.return_value = test_data
            ref = f"{idx:04d}" + "aaf0567890abcdef1234567890abcdef1234567890abcdef12345678abcd"

            response = client.get(f"/api/v1/data/{ref}/json")

            assert response.status_code == 200, f"Failed for: {description}"
            json_response = response.json()
            assert json_response["content_type"] == expected_type, f"Wrong content type for {description}: expected {expected_type}, got {json_response['content_type']}"


class TestMalformedContentHandling:
    """Test handling of malformed or edge case content."""

    @patch('app.api.endpoints.data.download_data_from_swarm')
    def test_malformed_json_handling(self, mock_download):
        """Test that malformed JSON is treated as binary."""
        malformed_json = b'{"test": "data", invalid}'
        mock_download.return_value = malformed_json

        response = client.get("/api/v1/data/aab2567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef")

        assert response.status_code == 200
        # Malformed JSON is still valid UTF-8, so detected as text/plain
        assert response.headers["content-type"].startswith("text/plain")
        assert response.headers["content-disposition"] == 'attachment; filename="text-aab25678.txt"'

    @patch('app.api.endpoints.data.download_data_from_swarm')
    def test_invalid_utf8_handling(self, mock_download):
        """Test handling of invalid UTF-8 sequences."""
        invalid_utf8 = b'\x80\x81\x82\x83'  # Invalid UTF-8
        mock_download.return_value = invalid_utf8

        response = client.get("/api/v1/data/aac2567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef")

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/octet-stream"
        assert response.headers["content-disposition"] == 'attachment; filename="data-aac25678.bin"'

    @patch('app.api.endpoints.data.download_data_from_swarm')
    def test_very_large_content_headers(self, mock_download):
        """Test headers with very large content."""
        large_data = b'x' * (10 * 1024 * 1024)  # 10MB
        mock_download.return_value = large_data

        response = client.get("/api/v1/data/aad2567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef")

        assert response.status_code == 200
        assert response.headers["content-length"] == str(len(large_data))
        assert response.headers["content-type"].startswith("text/plain")  # Should detect as text
        assert response.headers["content-disposition"] == 'attachment; filename="text-aad25678.txt"'


class TestReferenceHashEdgeCases:
    """Test edge cases with reference hash handling."""

    @patch('app.api.endpoints.data.download_data_from_swarm')
    def test_short_references_rejected(self, mock_download):
        """Test that references shorter than 64 hex chars are rejected."""
        json_data = {"test": "data"}
        mock_download.return_value = json.dumps(json_data).encode('utf-8')

        short_refs = [
            "a",  # Very short
            "abc123",  # Short
            "1234567890123456",  # Medium (16 chars)
            "abcdef" * 10,  # 60 chars - still too short
        ]

        for ref in short_refs:
            response = client.get(f"/api/v1/data/{ref}")
            assert response.status_code == 422, f"Expected 422 for short ref of length {len(ref)}"

    @patch('app.api.endpoints.data.download_data_from_swarm')
    def test_valid_reference_lengths(self, mock_download):
        """Test that valid 64 and 128 char hex references are accepted."""
        json_data = {"test": "data"}
        mock_download.return_value = json.dumps(json_data).encode('utf-8')

        valid_refs = [
            "1234567890abcdef" * 4,  # Standard 64 chars
            "abcdef1234567890" * 8,  # 128 chars (also valid)
        ]

        for ref in valid_refs:
            response = client.get(f"/api/v1/data/{ref}")
            assert response.status_code == 200, f"Expected 200 for valid ref of length {len(ref)}"
            expected_prefix = ref[:8]
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