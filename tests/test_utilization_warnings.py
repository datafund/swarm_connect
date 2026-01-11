# tests/test_utilization_warnings.py
"""Tests for stamp utilization warnings and pre-upload validation."""
import pytest
from unittest.mock import patch, MagicMock

from app.services.swarm_api import (
    calculate_utilization_status,
    validate_stamp_for_upload,
    check_upload_failure_reason,
    StampValidationError,
    UTILIZATION_THRESHOLD_WARNING,
    UTILIZATION_THRESHOLD_CRITICAL,
    UTILIZATION_THRESHOLD_FULL
)


class TestCalculateUtilizationStatus:
    """Unit tests for the calculate_utilization_status function."""

    def test_ok_status_at_zero(self):
        """Test 0% utilization returns 'ok' status."""
        status, warning = calculate_utilization_status(0.0)
        assert status == "ok"
        assert warning is None

    def test_ok_status_at_50_percent(self):
        """Test 50% utilization returns 'ok' status."""
        status, warning = calculate_utilization_status(50.0)
        assert status == "ok"
        assert warning is None

    def test_ok_status_just_below_warning(self):
        """Test status just below warning threshold (79.99%)."""
        status, warning = calculate_utilization_status(79.99)
        assert status == "ok"
        assert warning is None

    def test_warning_status_at_threshold(self):
        """Test 80% utilization returns 'warning' status."""
        status, warning = calculate_utilization_status(80.0)
        assert status == "warning"
        assert warning is not None
        assert "80.0%" in warning
        assert "approaching full capacity" in warning

    def test_warning_status_at_90_percent(self):
        """Test 90% utilization returns 'warning' status."""
        status, warning = calculate_utilization_status(90.0)
        assert status == "warning"
        assert warning is not None
        assert "90.0%" in warning

    def test_critical_status_at_threshold(self):
        """Test 95% utilization returns 'critical' status."""
        status, warning = calculate_utilization_status(95.0)
        assert status == "critical"
        assert warning is not None
        assert "95.0%" in warning
        assert "nearly full" in warning

    def test_critical_status_at_99_percent(self):
        """Test 99% utilization returns 'critical' status."""
        status, warning = calculate_utilization_status(99.0)
        assert status == "critical"
        assert warning is not None
        assert "99.0%" in warning

    def test_full_status_at_100_percent(self):
        """Test 100% utilization returns 'full' status."""
        status, warning = calculate_utilization_status(100.0)
        assert status == "full"
        assert warning is not None
        assert "completely full" in warning
        assert "100%" in warning
        assert "purchase a new stamp" in warning.lower()

    def test_none_utilization(self):
        """Test None utilization returns None status."""
        status, warning = calculate_utilization_status(None)
        assert status is None
        assert warning is None


class TestValidateStampForUpload:
    """Tests for validate_stamp_for_upload function."""

    @patch('app.services.swarm_api.get_all_stamps_processed')
    def test_valid_stamp(self, mock_processed):
        """Test validation passes for a valid stamp."""
        mock_processed.return_value = [
            {
                "batchID": "abc123",
                "utilizationPercent": 50.0,
                "utilizationStatus": "ok",
                "utilizationWarning": None,
                "usable": True
            }
        ]

        result = validate_stamp_for_upload("abc123")

        assert result["batchID"] == "abc123"
        assert result["utilizationPercent"] == 50.0
        assert result["usable"] is True

    @patch('app.services.swarm_api.get_all_stamps_processed')
    def test_stamp_not_found(self, mock_processed):
        """Test validation fails for non-existent stamp."""
        mock_processed.return_value = []

        with pytest.raises(StampValidationError) as exc_info:
            validate_stamp_for_upload("nonexistent")

        assert exc_info.value.status == "not_found"
        assert "not found" in exc_info.value.message

    @patch('app.services.swarm_api.get_all_stamps_processed')
    def test_stamp_full_100_percent(self, mock_processed):
        """Test validation fails for 100% utilized stamp."""
        mock_processed.return_value = [
            {
                "batchID": "abc123",
                "utilizationPercent": 100.0,
                "utilizationStatus": "full",
                "utilizationWarning": "Stamp is full",
                "usable": False
            }
        ]

        with pytest.raises(StampValidationError) as exc_info:
            validate_stamp_for_upload("abc123")

        assert exc_info.value.status == "full"
        assert "100%" in exc_info.value.message
        assert exc_info.value.utilization_percent == 100.0

    @patch('app.services.swarm_api.get_all_stamps_processed')
    def test_stamp_not_usable(self, mock_processed):
        """Test validation fails for non-usable stamp."""
        mock_processed.return_value = [
            {
                "batchID": "abc123",
                "utilizationPercent": 50.0,
                "utilizationStatus": "ok",
                "utilizationWarning": None,
                "usable": False
            }
        ]

        with pytest.raises(StampValidationError) as exc_info:
            validate_stamp_for_upload("abc123")

        assert exc_info.value.status == "not_usable"
        assert "not usable" in exc_info.value.message

    @patch('app.services.swarm_api.get_all_stamps_processed')
    def test_case_insensitive_stamp_id(self, mock_processed):
        """Test stamp ID matching is case-insensitive."""
        mock_processed.return_value = [
            {
                "batchID": "ABC123",
                "utilizationPercent": 50.0,
                "utilizationStatus": "ok",
                "usable": True
            }
        ]

        result = validate_stamp_for_upload("abc123")
        assert result["batchID"] == "ABC123"


class TestCheckUploadFailureReason:
    """Tests for check_upload_failure_reason function."""

    @patch('app.services.swarm_api.get_all_stamps_processed')
    def test_full_stamp_returns_enhanced_message(self, mock_processed):
        """Test that full stamp returns enhanced error message."""
        mock_processed.return_value = [
            {
                "batchID": "abc123",
                "utilizationPercent": 100.0,
                "utilizationStatus": "full"
            }
        ]

        result = check_upload_failure_reason("abc123", "Original error")

        assert result is not None
        assert "100%" in result
        assert "full" in result.lower()
        assert "purchase a new stamp" in result.lower()

    @patch('app.services.swarm_api.get_all_stamps_processed')
    def test_critical_stamp_returns_enhanced_message(self, mock_processed):
        """Test that critical utilization returns enhanced error message."""
        mock_processed.return_value = [
            {
                "batchID": "abc123",
                "utilizationPercent": 97.5,
                "utilizationStatus": "critical"
            }
        ]

        result = check_upload_failure_reason("abc123", "Original error")

        assert result is not None
        assert "97.5%" in result
        assert "Original error" in result

    @patch('app.services.swarm_api.get_all_stamps_processed')
    def test_ok_stamp_returns_none(self, mock_processed):
        """Test that ok stamp returns None (no enhanced message)."""
        mock_processed.return_value = [
            {
                "batchID": "abc123",
                "utilizationPercent": 50.0,
                "utilizationStatus": "ok"
            }
        ]

        result = check_upload_failure_reason("abc123", "Original error")
        assert result is None

    @patch('app.services.swarm_api.get_all_stamps_processed')
    def test_stamp_not_found_returns_none(self, mock_processed):
        """Test that non-existent stamp returns None."""
        mock_processed.return_value = []

        result = check_upload_failure_reason("nonexistent", "Original error")
        assert result is None


class TestUsableStatusWith100Percent:
    """Tests for usable status calculation with 100% utilization."""

    @patch('app.services.swarm_api.get_all_stamps')
    @patch('app.services.swarm_api.get_local_stamps')
    def test_usable_false_at_100_percent(self, mock_local, mock_global):
        """Test that usable is false when utilization is 100%."""
        from app.services.swarm_api import get_all_stamps_processed

        mock_global.return_value = [
            {
                "batchID": "test123",
                "depth": 17,
                "bucketDepth": 16,
                "batchTTL": 86400,
                "amount": "1000000"
            }
        ]
        # utilization=2, depth=17, bucketDepth=16 -> 100%
        mock_local.return_value = [
            {
                "batchID": "test123",
                "utilization": 2,
                "usable": True  # Local says usable, but we override at 100%
            }
        ]

        result = get_all_stamps_processed()

        assert len(result) == 1
        assert result[0]["utilizationPercent"] == 100.0
        assert result[0]["utilizationStatus"] == "full"
        assert result[0]["usable"] is False  # Should be overridden to False

    @patch('app.services.swarm_api.get_all_stamps')
    @patch('app.services.swarm_api.get_local_stamps')
    def test_usable_preserved_below_100_percent(self, mock_local, mock_global):
        """Test that local usable value is preserved below 100%."""
        from app.services.swarm_api import get_all_stamps_processed

        mock_global.return_value = [
            {
                "batchID": "test123",
                "depth": 17,
                "bucketDepth": 16,
                "batchTTL": 86400,
                "amount": "1000000"
            }
        ]
        # utilization=1, depth=17, bucketDepth=16 -> 50%
        mock_local.return_value = [
            {
                "batchID": "test123",
                "utilization": 1,
                "usable": True
            }
        ]

        result = get_all_stamps_processed()

        assert len(result) == 1
        assert result[0]["utilizationPercent"] == 50.0
        assert result[0]["usable"] is True


class TestUtilizationStatusInAPI:
    """Tests for utilization status fields in API responses."""

    @patch('app.services.swarm_api.get_all_stamps_processed')
    def test_stamps_list_includes_status_fields(self, mock_processed):
        """Test that /api/v1/stamps/ includes utilizationStatus and utilizationWarning."""
        from fastapi.testclient import TestClient
        from app.main import app

        mock_processed.return_value = [
            {
                "batchID": "abc123",
                "amount": "1000000",
                "depth": 20,
                "bucketDepth": 16,
                "batchTTL": 86400,
                "utilization": 4,
                "utilizationPercent": 25.0,
                "utilizationStatus": "ok",
                "utilizationWarning": None,
                "usable": True,
                "label": None,
                "blockNumber": None,
                "owner": None,
                "immutableFlag": None,
                "expectedExpiration": "2025-01-01-00-00",
                "local": True
            }
        ]

        client = TestClient(app)
        response = client.get("/api/v1/stamps/")

        assert response.status_code == 200
        data = response.json()
        assert len(data["stamps"]) == 1
        assert data["stamps"][0]["utilizationStatus"] == "ok"
        assert data["stamps"][0]["utilizationWarning"] is None

    @patch('app.services.swarm_api.get_all_stamps_processed')
    def test_warning_status_in_response(self, mock_processed):
        """Test warning status appears correctly in response."""
        from fastapi.testclient import TestClient
        from app.main import app

        mock_processed.return_value = [
            {
                "batchID": "abc123",
                "amount": "1000000",
                "depth": 20,
                "bucketDepth": 16,
                "batchTTL": 86400,
                "utilization": 14,  # 14/16 = 87.5%
                "utilizationPercent": 87.5,
                "utilizationStatus": "warning",
                "utilizationWarning": "Stamp is approaching full capacity (87.5% utilized).",
                "usable": True,
                "label": None,
                "blockNumber": None,
                "owner": None,
                "immutableFlag": None,
                "expectedExpiration": "2025-01-01-00-00",
                "local": True
            }
        ]

        client = TestClient(app)
        response = client.get("/api/v1/stamps/abc123")

        assert response.status_code == 200
        data = response.json()
        assert data["utilizationStatus"] == "warning"
        assert data["utilizationWarning"] is not None
        assert "87.5%" in data["utilizationWarning"]

    @patch('app.services.swarm_api.get_all_stamps_processed')
    def test_full_status_in_response(self, mock_processed):
        """Test full status appears correctly in response."""
        from fastapi.testclient import TestClient
        from app.main import app

        mock_processed.return_value = [
            {
                "batchID": "abc123",
                "amount": "1000000",
                "depth": 20,
                "bucketDepth": 16,
                "batchTTL": 86400,
                "utilization": 16,  # 16/16 = 100%
                "utilizationPercent": 100.0,
                "utilizationStatus": "full",
                "utilizationWarning": "Stamp is completely full (100% utilized).",
                "usable": False,  # Not usable when full
                "label": None,
                "blockNumber": None,
                "owner": None,
                "immutableFlag": None,
                "expectedExpiration": "2025-01-01-00-00",
                "local": True
            }
        ]

        client = TestClient(app)
        response = client.get("/api/v1/stamps/abc123")

        assert response.status_code == 200
        data = response.json()
        assert data["utilizationStatus"] == "full"
        assert data["utilizationWarning"] is not None
        assert "100%" in data["utilizationWarning"]
        assert data["usable"] is False


class TestPreUploadValidationInEndpoints:
    """Tests for opt-in pre-upload validation in data endpoints."""

    @patch('app.services.swarm_api.get_all_stamps_processed')
    def test_upload_with_validation_and_full_stamp(self, mock_processed):
        """Test upload fails with validation when stamp is full."""
        from fastapi.testclient import TestClient
        from app.main import app
        import io

        mock_processed.return_value = [
            {
                "batchID": "fullstamp",
                "utilizationPercent": 100.0,
                "utilizationStatus": "full",
                "usable": False
            }
        ]

        client = TestClient(app)
        response = client.post(
            "/api/v1/data/?stamp_id=fullstamp&validate_stamp=true",
            files={"file": ("test.json", io.BytesIO(b'{"test": true}'), "application/json")}
        )

        assert response.status_code == 400
        assert "100%" in response.json()["detail"]

    @patch('app.services.swarm_api.get_all_stamps_processed')
    def test_upload_with_validation_and_not_found_stamp(self, mock_processed):
        """Test upload returns 404 when stamp not found (with validation)."""
        from fastapi.testclient import TestClient
        from app.main import app
        import io

        mock_processed.return_value = []

        client = TestClient(app)
        response = client.post(
            "/api/v1/data/?stamp_id=nonexistent&validate_stamp=true",
            files={"file": ("test.json", io.BytesIO(b'{"test": true}'), "application/json")}
        )

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    @patch('app.api.endpoints.data.upload_data_to_swarm')
    def test_upload_without_validation_skips_check(self, mock_upload):
        """Test upload without validation skips stamp check."""
        from fastapi.testclient import TestClient
        from app.main import app
        import io

        mock_upload.return_value = "abc123reference"

        client = TestClient(app)
        response = client.post(
            "/api/v1/data/?stamp_id=anystamp",  # No validate_stamp parameter
            files={"file": ("test.json", io.BytesIO(b'{"test": true}'), "application/json")}
        )

        # Should succeed without checking stamp
        assert response.status_code == 200
        mock_upload.assert_called_once()
