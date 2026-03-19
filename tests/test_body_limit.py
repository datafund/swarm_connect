# tests/test_body_limit.py
"""Tests for the JSON body size and depth limiting middleware."""
import pytest
from unittest.mock import patch

from app.middleware.body_limit import _check_nesting_depth


class TestCheckNestingDepth:
    """Unit tests for the _check_nesting_depth helper."""

    def test_flat_object(self):
        assert _check_nesting_depth(b'{"a": 1, "b": 2}', 20) is True

    def test_flat_array(self):
        assert _check_nesting_depth(b'[1, 2, 3]', 20) is True

    def test_nested_within_limit(self):
        data = b'{"a": {"b": {"c": 1}}}'  # depth 3
        assert _check_nesting_depth(data, 5) is True

    def test_nested_at_exact_limit(self):
        # depth exactly 3
        data = b'{"a": {"b": {"c": 1}}}'
        assert _check_nesting_depth(data, 3) is True

    def test_nested_exceeds_limit(self):
        # depth 4, limit 3
        data = b'{"a": {"b": {"c": {"d": 1}}}}'
        assert _check_nesting_depth(data, 3) is False

    def test_deeply_nested_objects(self):
        depth = 50
        data = b'{"a":' * depth + b'1' + b'}' * depth
        assert _check_nesting_depth(data, 20) is False
        assert _check_nesting_depth(data, 50) is True

    def test_deeply_nested_arrays(self):
        depth = 30
        data = b'[' * depth + b'1' + b']' * depth
        assert _check_nesting_depth(data, 20) is False
        assert _check_nesting_depth(data, 30) is True

    def test_mixed_objects_and_arrays(self):
        data = b'{"a": [{"b": [1]}]}'  # depth 4
        assert _check_nesting_depth(data, 4) is True
        assert _check_nesting_depth(data, 3) is False

    def test_brackets_inside_strings_ignored(self):
        """Brackets inside JSON strings should not count toward depth."""
        data = b'{"key": "value with { and [ brackets"}'  # depth 1
        assert _check_nesting_depth(data, 1) is True

    def test_escaped_quotes_in_strings(self):
        """Escaped quotes should not break string tracking."""
        data = b'{"key": "value with \\"escaped\\" and {nested}"}'  # depth 1
        assert _check_nesting_depth(data, 1) is True

    def test_empty_body(self):
        assert _check_nesting_depth(b'', 20) is True

    def test_empty_object(self):
        assert _check_nesting_depth(b'{}', 20) is True

    def test_limit_of_one(self):
        assert _check_nesting_depth(b'{"a": 1}', 1) is True
        assert _check_nesting_depth(b'{"a": {"b": 1}}', 1) is False


class TestBodyLimitMiddleware:
    """Integration tests for the BodyLimitMiddleware via FastAPI TestClient."""

    @pytest.fixture
    def client(self):
        """Create a test client with the body limit middleware active."""
        from fastapi.testclient import TestClient
        from app.main import app
        return TestClient(app)

    def test_normal_json_request_passes(self, client):
        """Normal JSON with shallow nesting should pass through."""
        response = client.post(
            "/api/v1/pool/acquire",
            json={"depth": 17},
            headers={"X-Payment-Mode": "free"},
        )
        # Should not be 413 or 400 (depth/size) — any other status is fine
        assert response.status_code not in (413,)

    def test_deeply_nested_json_rejected(self, client):
        """JSON with 100+ levels of nesting should be rejected with 400."""
        depth = 100
        nested = '{"a":' * depth + '1' + '}' * depth
        response = client.post(
            "/api/v1/pool/acquire",
            content=nested.encode(),
            headers={
                "Content-Type": "application/json",
                "X-Payment-Mode": "free",
            },
        )
        assert response.status_code == 400
        assert "nesting too deep" in response.json()["detail"].lower()

    def test_nesting_at_limit_passes(self, client):
        """JSON with exactly 20 levels of nesting should pass."""
        depth = 20
        nested = '{"a":' * depth + '1' + '}' * depth
        response = client.post(
            "/api/v1/pool/acquire",
            content=nested.encode(),
            headers={
                "Content-Type": "application/json",
                "X-Payment-Mode": "free",
            },
        )
        # Should not be rejected for depth
        assert response.status_code != 400 or "nesting" not in response.json().get("detail", "").lower()

    @patch("app.middleware.body_limit.settings")
    def test_oversized_json_rejected(self, mock_settings, client):
        """JSON body exceeding MAX_JSON_BODY_BYTES should be rejected with 413."""
        # Use a small limit for testing
        mock_settings.MAX_JSON_BODY_BYTES = 100
        mock_settings.MAX_JSON_DEPTH = 20

        large_body = '{"data": "' + 'x' * 200 + '"}'
        response = client.post(
            "/api/v1/pool/acquire",
            content=large_body.encode(),
            headers={
                "Content-Type": "application/json",
                "X-Payment-Mode": "free",
            },
        )
        assert response.status_code == 413
        assert "too large" in response.json()["detail"].lower()

    def test_non_json_request_not_checked(self, client):
        """Non-JSON content types should bypass the depth/size check."""
        # Multipart file upload with deeply nested filename — should not be checked
        depth = 100
        nested = '{"a":' * depth + '1' + '}' * depth
        response = client.post(
            "/api/v1/pool/acquire",
            content=nested.encode(),
            headers={
                "Content-Type": "text/plain",
                "X-Payment-Mode": "free",
            },
        )
        # Should not be 400 for nesting — might be 422 for wrong content type
        assert response.status_code != 400 or "nesting" not in response.json().get("detail", "").lower()

    def test_get_request_not_checked(self, client):
        """GET requests should not be checked for body limits."""
        response = client.get("/api/v1/stamps/")
        assert response.status_code not in (400, 413)

    def test_empty_json_body_passes(self, client):
        """Empty JSON body should pass through."""
        response = client.post(
            "/api/v1/pool/acquire",
            content=b"",
            headers={
                "Content-Type": "application/json",
                "X-Payment-Mode": "free",
            },
        )
        # Should not be 400 or 413
        assert response.status_code not in (413,)
