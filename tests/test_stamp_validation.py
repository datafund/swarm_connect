# tests/test_stamp_validation.py
"""
Tests for improved stamp validation with better error messages.
Issue #23: https://github.com/datafund/swarm_connect/issues/23
"""
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from app.main import app
from app.services.swarm_api import (
    validate_stamp_for_upload,
    check_upload_failure_reason,
    get_stamp_health_check,
    StampValidationError,
    UTILIZATION_THRESHOLD_WARNING,
    UTILIZATION_THRESHOLD_CRITICAL,
    UTILIZATION_THRESHOLD_FULL,
    TTL_THRESHOLD_LOW
)

client = TestClient(app)


# =============================================================================
# Test Data Fixtures
# =============================================================================

def make_stamp(
    batch_id="abc123",
    local=True,
    usable=True,
    utilization_percent=50.0,
    utilization_status="ok",
    batch_ttl=86400,
    expected_expiration="2026-01-12-17-30"
):
    """Helper to create stamp test data."""
    return {
        "batchID": batch_id,
        "local": local,
        "usable": usable,
        "utilizationPercent": utilization_percent,
        "utilizationStatus": utilization_status,
        "utilizationWarning": None,
        "batchTTL": batch_ttl,
        "expectedExpiration": expected_expiration,
        "depth": 20,
        "bucketDepth": 16,
        "amount": "100000000"
    }


# =============================================================================
# validate_stamp_for_upload() Tests
# =============================================================================

class TestValidateStampForUpload:
    """Tests for the validate_stamp_for_upload function."""

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_stamp_not_found(self, mock_get_stamps):
        """Should raise StampValidationError with NOT_FOUND code when stamp doesn't exist."""
        mock_get_stamps.return_value = []

        with pytest.raises(StampValidationError) as exc_info:
            validate_stamp_for_upload("nonexistent_stamp")

        assert exc_info.value.code == "NOT_FOUND"
        assert exc_info.value.status == "not_found"
        assert "not found" in exc_info.value.message.lower()
        assert exc_info.value.suggestion  # Should have a suggestion

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_stamp_not_local(self, mock_get_stamps):
        """Should raise StampValidationError with NOT_LOCAL code when stamp isn't owned by node."""
        mock_get_stamps.return_value = [make_stamp(batch_id="abc123", local=False)]

        with pytest.raises(StampValidationError) as exc_info:
            validate_stamp_for_upload("abc123")

        assert exc_info.value.code == "NOT_LOCAL"
        assert exc_info.value.status == "not_local"
        assert "not owned" in exc_info.value.message.lower()
        assert exc_info.value.suggestion
        assert exc_info.value.stamp_data is not None

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_stamp_expired(self, mock_get_stamps):
        """Should raise StampValidationError with EXPIRED code when TTL is 0."""
        mock_get_stamps.return_value = [make_stamp(batch_id="abc123", batch_ttl=0)]

        with pytest.raises(StampValidationError) as exc_info:
            validate_stamp_for_upload("abc123")

        assert exc_info.value.code == "EXPIRED"
        assert exc_info.value.status == "expired"
        assert "expired" in exc_info.value.message.lower()
        assert exc_info.value.suggestion

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_stamp_negative_ttl(self, mock_get_stamps):
        """Should treat negative TTL as expired."""
        mock_get_stamps.return_value = [make_stamp(batch_id="abc123", batch_ttl=-100)]

        with pytest.raises(StampValidationError) as exc_info:
            validate_stamp_for_upload("abc123")

        assert exc_info.value.code == "EXPIRED"

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_stamp_not_usable(self, mock_get_stamps):
        """Should raise StampValidationError with NOT_USABLE code when stamp isn't usable yet."""
        mock_get_stamps.return_value = [make_stamp(batch_id="abc123", usable=False)]

        with pytest.raises(StampValidationError) as exc_info:
            validate_stamp_for_upload("abc123")

        assert exc_info.value.code == "NOT_USABLE"
        assert exc_info.value.status == "not_usable"
        assert "30-90 seconds" in exc_info.value.message or "propagate" in exc_info.value.message.lower()
        assert exc_info.value.suggestion

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_stamp_full(self, mock_get_stamps):
        """Should raise StampValidationError with FULL code when stamp is at 100% utilization."""
        mock_get_stamps.return_value = [make_stamp(batch_id="abc123", utilization_percent=100.0)]

        with pytest.raises(StampValidationError) as exc_info:
            validate_stamp_for_upload("abc123")

        assert exc_info.value.code == "FULL"
        assert exc_info.value.status == "full"
        assert "100%" in exc_info.value.message or "full" in exc_info.value.message.lower()

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_valid_stamp_returns_info(self, mock_get_stamps):
        """Should return stamp info when stamp is valid."""
        mock_get_stamps.return_value = [make_stamp(batch_id="abc123")]

        result = validate_stamp_for_upload("abc123")

        assert result["batchID"] == "abc123"
        assert result["usable"] is True
        assert result["local"] is True
        assert "warnings" in result
        assert isinstance(result["warnings"], list)

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_valid_stamp_case_insensitive(self, mock_get_stamps):
        """Should match stamp ID case-insensitively."""
        mock_get_stamps.return_value = [make_stamp(batch_id="ABC123")]

        result = validate_stamp_for_upload("abc123")

        assert result["batchID"] == "ABC123"

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_low_ttl_warning(self, mock_get_stamps):
        """Should return LOW_TTL warning when TTL is below threshold."""
        mock_get_stamps.return_value = [make_stamp(batch_id="abc123", batch_ttl=1800)]  # 30 minutes

        result = validate_stamp_for_upload("abc123")

        assert any(w["code"] == "LOW_TTL" for w in result["warnings"])

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_nearly_full_warning(self, mock_get_stamps):
        """Should return NEARLY_FULL warning when utilization is 95%+."""
        mock_get_stamps.return_value = [make_stamp(batch_id="abc123", utilization_percent=97.0)]

        result = validate_stamp_for_upload("abc123")

        assert any(w["code"] == "NEARLY_FULL" for w in result["warnings"])

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_high_utilization_warning(self, mock_get_stamps):
        """Should return HIGH_UTILIZATION warning when utilization is 80%+."""
        mock_get_stamps.return_value = [make_stamp(batch_id="abc123", utilization_percent=85.0)]

        result = validate_stamp_for_upload("abc123")

        assert any(w["code"] == "HIGH_UTILIZATION" for w in result["warnings"])


# =============================================================================
# check_upload_failure_reason() Tests
# =============================================================================

class TestCheckUploadFailureReason:
    """Tests for the check_upload_failure_reason function."""

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_not_found_returns_structured_error(self, mock_get_stamps):
        """Should return structured error when stamp not found."""
        mock_get_stamps.return_value = []

        result = check_upload_failure_reason("nonexistent", "Some error")

        assert result is not None
        assert result["code"] == "NOT_FOUND"
        assert "message" in result
        assert "suggestion" in result

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_not_local_returns_structured_error(self, mock_get_stamps):
        """Should return structured error when stamp is not local."""
        mock_get_stamps.return_value = [make_stamp(batch_id="abc123", local=False)]

        result = check_upload_failure_reason("abc123", "Some error")

        assert result is not None
        assert result["code"] == "NOT_LOCAL"
        assert result["stamp_status"] is not None
        assert result["stamp_status"]["local"] is False

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_expired_returns_structured_error(self, mock_get_stamps):
        """Should return structured error when stamp is expired."""
        mock_get_stamps.return_value = [make_stamp(batch_id="abc123", batch_ttl=0)]

        result = check_upload_failure_reason("abc123", "Some error")

        assert result is not None
        assert result["code"] == "EXPIRED"

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_not_usable_returns_structured_error(self, mock_get_stamps):
        """Should return structured error when stamp is not usable."""
        mock_get_stamps.return_value = [make_stamp(batch_id="abc123", usable=False)]

        result = check_upload_failure_reason("abc123", "Some error")

        assert result is not None
        assert result["code"] == "NOT_USABLE"
        assert "30-90" in result["suggestion"] or "propagat" in result["suggestion"].lower()

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_full_returns_structured_error(self, mock_get_stamps):
        """Should return structured error when stamp is full."""
        mock_get_stamps.return_value = [make_stamp(batch_id="abc123", utilization_percent=100.0)]

        result = check_upload_failure_reason("abc123", "Some error")

        assert result is not None
        assert result["code"] == "FULL"

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_nearly_full_returns_structured_error(self, mock_get_stamps):
        """Should return structured error when stamp is nearly full."""
        mock_get_stamps.return_value = [make_stamp(batch_id="abc123", utilization_percent=97.0, utilization_status="critical")]

        result = check_upload_failure_reason("abc123", "Original error message")

        assert result is not None
        assert result["code"] == "NEARLY_FULL"
        # Should include original error for context
        assert "original_error" in result or "Original error" in result.get("message", "")

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_valid_stamp_returns_none(self, mock_get_stamps):
        """Should return None when stamp is valid (cause unknown)."""
        mock_get_stamps.return_value = [make_stamp(batch_id="abc123")]

        result = check_upload_failure_reason("abc123", "Some error")

        assert result is None


# =============================================================================
# get_stamp_health_check() Tests
# =============================================================================

class TestGetStampHealthCheck:
    """Tests for the get_stamp_health_check function."""

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_healthy_stamp(self, mock_get_stamps):
        """Should return can_upload=True with no errors for healthy stamp."""
        mock_get_stamps.return_value = [make_stamp(batch_id="abc123")]

        result = get_stamp_health_check("abc123")

        assert result["stamp_id"] == "abc123"
        assert result["can_upload"] is True
        assert len(result["errors"]) == 0
        assert result["status"]["exists"] is True
        assert result["status"]["local"] is True
        assert result["status"]["usable"] is True

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_not_found_stamp(self, mock_get_stamps):
        """Should return can_upload=False with NOT_FOUND error."""
        mock_get_stamps.return_value = []

        result = get_stamp_health_check("nonexistent")

        assert result["can_upload"] is False
        assert len(result["errors"]) == 1
        assert result["errors"][0]["code"] == "NOT_FOUND"
        assert result["status"]["exists"] is False

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_not_local_stamp(self, mock_get_stamps):
        """Should return can_upload=False with NOT_LOCAL error."""
        mock_get_stamps.return_value = [make_stamp(batch_id="abc123", local=False)]

        result = get_stamp_health_check("abc123")

        assert result["can_upload"] is False
        assert any(e["code"] == "NOT_LOCAL" for e in result["errors"])

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_expired_stamp(self, mock_get_stamps):
        """Should return can_upload=False with EXPIRED error."""
        mock_get_stamps.return_value = [make_stamp(batch_id="abc123", batch_ttl=0)]

        result = get_stamp_health_check("abc123")

        assert result["can_upload"] is False
        assert any(e["code"] == "EXPIRED" for e in result["errors"])

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_not_usable_stamp(self, mock_get_stamps):
        """Should return can_upload=False with NOT_USABLE error."""
        mock_get_stamps.return_value = [make_stamp(batch_id="abc123", usable=False)]

        result = get_stamp_health_check("abc123")

        assert result["can_upload"] is False
        assert any(e["code"] == "NOT_USABLE" for e in result["errors"])

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_full_stamp(self, mock_get_stamps):
        """Should return can_upload=False with FULL error."""
        mock_get_stamps.return_value = [make_stamp(batch_id="abc123", utilization_percent=100.0)]

        result = get_stamp_health_check("abc123")

        assert result["can_upload"] is False
        assert any(e["code"] == "FULL" for e in result["errors"])

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_low_ttl_warning(self, mock_get_stamps):
        """Should return warning for low TTL."""
        mock_get_stamps.return_value = [make_stamp(batch_id="abc123", batch_ttl=1800)]

        result = get_stamp_health_check("abc123")

        assert result["can_upload"] is True  # Still usable
        assert any(w["code"] == "LOW_TTL" for w in result["warnings"])

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_nearly_full_warning(self, mock_get_stamps):
        """Should return warning for nearly full stamp."""
        mock_get_stamps.return_value = [make_stamp(batch_id="abc123", utilization_percent=97.0)]

        result = get_stamp_health_check("abc123")

        assert result["can_upload"] is True  # Still usable
        assert any(w["code"] == "NEARLY_FULL" for w in result["warnings"])

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_high_utilization_warning(self, mock_get_stamps):
        """Should return warning for high utilization."""
        mock_get_stamps.return_value = [make_stamp(batch_id="abc123", utilization_percent=85.0)]

        result = get_stamp_health_check("abc123")

        assert result["can_upload"] is True
        assert any(w["code"] == "HIGH_UTILIZATION" for w in result["warnings"])

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_multiple_errors(self, mock_get_stamps):
        """Should return multiple errors when multiple issues exist."""
        # Stamp that is not local AND not usable
        mock_get_stamps.return_value = [make_stamp(batch_id="abc123", local=False, usable=False)]

        result = get_stamp_health_check("abc123")

        assert result["can_upload"] is False
        assert len(result["errors"]) >= 1  # At least NOT_LOCAL


# =============================================================================
# /stamps/{stamp_id}/check Endpoint Tests
# =============================================================================

class TestStampCheckEndpoint:
    """Tests for the /stamps/{stamp_id}/check endpoint."""

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_check_healthy_stamp(self, mock_get_stamps):
        """Should return 200 with can_upload=True for healthy stamp."""
        mock_get_stamps.return_value = [make_stamp(batch_id="abc123")]

        response = client.get("/api/v1/stamps/abc123/check")

        assert response.status_code == 200
        data = response.json()
        assert data["stamp_id"] == "abc123"
        assert data["can_upload"] is True
        assert len(data["errors"]) == 0

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_check_not_found_stamp(self, mock_get_stamps):
        """Should return 200 with can_upload=False for not found stamp."""
        mock_get_stamps.return_value = []

        response = client.get("/api/v1/stamps/nonexistent/check")

        assert response.status_code == 200
        data = response.json()
        assert data["can_upload"] is False
        assert any(e["code"] == "NOT_FOUND" for e in data["errors"])

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_check_stamp_with_warnings(self, mock_get_stamps):
        """Should return warnings in response."""
        mock_get_stamps.return_value = [make_stamp(batch_id="abc123", utilization_percent=85.0)]

        response = client.get("/api/v1/stamps/abc123/check")

        assert response.status_code == 200
        data = response.json()
        assert data["can_upload"] is True
        assert len(data["warnings"]) > 0
        assert "status" in data
        assert data["status"]["utilizationPercent"] == 85.0

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_check_stamp_not_usable(self, mock_get_stamps):
        """Should show NOT_USABLE error with propagation suggestion."""
        mock_get_stamps.return_value = [make_stamp(batch_id="abc123", usable=False)]

        response = client.get("/api/v1/stamps/abc123/check")

        assert response.status_code == 200
        data = response.json()
        assert data["can_upload"] is False
        not_usable_error = next(e for e in data["errors"] if e["code"] == "NOT_USABLE")
        assert "30-90" in not_usable_error["suggestion"] or "propagat" in not_usable_error["message"].lower()


# =============================================================================
# Integration Tests - Upload with validate_stamp=True
# =============================================================================

class TestUploadWithValidation:
    """Tests for upload endpoints with stamp validation enabled."""

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_upload_with_not_local_stamp(self, mock_get_stamps):
        """Should return 400 with structured error for non-local stamp."""
        mock_get_stamps.return_value = [make_stamp(batch_id="abc123", local=False)]

        response = client.post(
            "/api/v1/data/?stamp_id=abc123&validate_stamp=true",
            files={"file": ("test.json", b'{"test": "data"}', "application/json")}
        )

        assert response.status_code == 400
        data = response.json()
        assert "detail" in data
        detail = data["detail"]
        assert detail["code"] == "NOT_LOCAL"
        assert "message" in detail
        assert "suggestion" in detail
        assert detail["stamp_id"] == "abc123"

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_upload_with_not_usable_stamp(self, mock_get_stamps):
        """Should return 400 with propagation delay message."""
        mock_get_stamps.return_value = [make_stamp(batch_id="abc123", usable=False)]

        response = client.post(
            "/api/v1/data/?stamp_id=abc123&validate_stamp=true",
            files={"file": ("test.json", b'{"test": "data"}', "application/json")}
        )

        assert response.status_code == 400
        data = response.json()
        detail = data["detail"]
        assert detail["code"] == "NOT_USABLE"
        # Should mention propagation delay
        assert "30-90" in detail["suggestion"] or "30-90" in detail["message"]

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_upload_with_not_found_stamp(self, mock_get_stamps):
        """Should return 404 with structured error for not found stamp."""
        mock_get_stamps.return_value = []

        response = client.post(
            "/api/v1/data/?stamp_id=nonexistent&validate_stamp=true",
            files={"file": ("test.json", b'{"test": "data"}', "application/json")}
        )

        assert response.status_code == 404
        data = response.json()
        detail = data["detail"]
        assert detail["code"] == "NOT_FOUND"

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_upload_with_expired_stamp(self, mock_get_stamps):
        """Should return 400 with expired message."""
        mock_get_stamps.return_value = [make_stamp(batch_id="abc123", batch_ttl=0)]

        response = client.post(
            "/api/v1/data/?stamp_id=abc123&validate_stamp=true",
            files={"file": ("test.json", b'{"test": "data"}', "application/json")}
        )

        assert response.status_code == 400
        data = response.json()
        detail = data["detail"]
        assert detail["code"] == "EXPIRED"

    @patch("app.services.swarm_api.get_all_stamps_processed")
    def test_upload_with_full_stamp(self, mock_get_stamps):
        """Should return 400 with full stamp message."""
        mock_get_stamps.return_value = [make_stamp(batch_id="abc123", utilization_percent=100.0)]

        response = client.post(
            "/api/v1/data/?stamp_id=abc123&validate_stamp=true",
            files={"file": ("test.json", b'{"test": "data"}', "application/json")}
        )

        assert response.status_code == 400
        data = response.json()
        detail = data["detail"]
        assert detail["code"] == "FULL"
