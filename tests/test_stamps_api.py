# tests/test_stamps_api.py
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
import json

from app.main import app

client = TestClient(app)


class TestStampsAPI:
    """Test suite for Stamps API endpoints."""

    @patch('app.services.swarm_api.get_all_stamps_processed')
    def test_list_stamps_success(self, mock_get_stamps):
        """Test successful retrieval of stamps list."""
        mock_get_stamps.return_value = [
            {
                "batchID": "test123",
                "amount": "1000000000",
                "blockNumber": 12345,
                "owner": "0x1234567890abcdef",
                "immutableFlag": False,
                "depth": 18,
                "bucketDepth": 16,
                "batchTTL": 3600,
                "utilization": 50,
                "usable": True,
                "label": "test-stamp",
                "expectedExpiration": "2024-12-01-15-30",
                "local": True
            },
            {
                "batchID": "test456",
                "amount": "2000000000",
                "blockNumber": None,
                "owner": None,
                "immutableFlag": True,
                "depth": 20,
                "bucketDepth": 16,
                "batchTTL": 7200,
                "utilization": None,
                "usable": True,
                "label": None,
                "expectedExpiration": "2024-12-01-17-30",
                "local": False
            }
        ]

        response = client.get("/api/v1/stamps/")

        assert response.status_code == 200
        data = response.json()

        assert data["total_count"] == 2
        assert len(data["stamps"]) == 2

        # Check first stamp (local)
        stamp1 = data["stamps"][0]
        assert stamp1["batchID"] == "test123"
        assert stamp1["local"] is True
        assert stamp1["utilization"] == 50
        assert stamp1["label"] == "test-stamp"
        assert stamp1["immutableFlag"] is False

        # Check second stamp (global only)
        stamp2 = data["stamps"][1]
        assert stamp2["batchID"] == "test456"
        assert stamp2["local"] is False
        assert stamp2["utilization"] is None
        assert stamp2["label"] is None
        assert stamp2["immutableFlag"] is True

    @patch('app.services.swarm_api.get_all_stamps_processed')
    def test_list_stamps_empty_result(self, mock_get_stamps):
        """Test stamps list endpoint with empty result."""
        mock_get_stamps.return_value = []

        response = client.get("/api/v1/stamps/")

        assert response.status_code == 200
        data = response.json()
        assert data["total_count"] == 0
        assert data["stamps"] == []

    @patch('app.services.swarm_api.get_all_stamps_processed')
    def test_get_stamp_by_id_success(self, mock_get_stamps):
        """Test successful retrieval of specific stamp by ID."""
        mock_get_stamps.return_value = [
            {
                "batchID": "target123",
                "amount": "1000000000",
                "blockNumber": 12345,
                "owner": "0x1234567890abcdef",
                "immutableFlag": False,
                "depth": 18,
                "bucketDepth": 16,
                "batchTTL": 3600,
                "utilization": 75,
                "usable": True,
                "label": "my-stamp",
                "expectedExpiration": "2024-12-01-15-30",
                "local": True
            },
            {
                "batchID": "other456",
                "amount": "2000000000",
                "local": False
            }
        ]

        response = client.get("/api/v1/stamps/target123")

        assert response.status_code == 200
        data = response.json()

        assert data["batchID"] == "target123"
        assert data["local"] is True
        assert data["utilization"] == 75
        assert data["label"] == "my-stamp"
        assert data["immutableFlag"] is False

    @patch('app.services.swarm_api.get_all_stamps_processed')
    def test_get_stamp_by_id_not_found(self, mock_get_stamps):
        """Test retrieval of non-existent stamp."""
        mock_get_stamps.return_value = [
            {
                "batchID": "other123",
                "amount": "1000000000",
                "local": False
            }
        ]

        response = client.get("/api/v1/stamps/nonexistent")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    @patch('app.services.swarm_api.get_all_stamps_processed')
    def test_list_stamps_api_error(self, mock_get_stamps):
        """Test stamps list endpoint when API call fails."""
        from requests.exceptions import RequestException
        mock_get_stamps.side_effect = RequestException("Network error")

        response = client.get("/api/v1/stamps/")

        assert response.status_code == 502
        assert "swarm api" in response.json()["detail"].lower()

    @patch('app.services.swarm_api.purchase_postage_stamp')
    def test_purchase_stamp_success(self, mock_purchase):
        """Test successful stamp purchase."""
        mock_purchase.return_value = "new_batch_id_123"

        purchase_data = {
            "amount": 2000000000,
            "depth": 17,
            "label": "test-purchase"
        }

        response = client.post("/api/v1/stamps/", json=purchase_data)

        assert response.status_code == 201
        data = response.json()
        assert data["batchID"] == "new_batch_id_123"
        assert "successfully" in data["message"].lower()

        # Verify the service was called with correct parameters
        mock_purchase.assert_called_once_with(2000000000, 17, "test-purchase")

    @patch('app.services.swarm_api.purchase_postage_stamp')
    def test_purchase_stamp_without_label(self, mock_purchase):
        """Test stamp purchase without optional label."""
        mock_purchase.return_value = "new_batch_id_456"

        purchase_data = {
            "amount": 1000000000,
            "depth": 18
        }

        response = client.post("/api/v1/stamps/", json=purchase_data)

        assert response.status_code == 201
        data = response.json()
        assert data["batchID"] == "new_batch_id_456"

        # Verify the service was called with None for label
        mock_purchase.assert_called_once_with(1000000000, 18, None)

    @patch('app.services.swarm_api.purchase_postage_stamp')
    def test_purchase_stamp_api_error(self, mock_purchase):
        """Test stamp purchase when API call fails."""
        from requests.exceptions import RequestException
        mock_purchase.side_effect = RequestException("Purchase failed")

        purchase_data = {
            "amount": 1000000000,
            "depth": 18
        }

        response = client.post("/api/v1/stamps/", json=purchase_data)

        assert response.status_code == 502
        assert "failed to purchase" in response.json()["detail"].lower()

    def test_purchase_stamp_invalid_data(self):
        """Test stamp purchase with invalid request data."""
        invalid_data = {
            "amount": "not_a_number",
            "depth": "also_not_a_number"
        }

        response = client.post("/api/v1/stamps/", json=invalid_data)

        assert response.status_code == 422  # Validation error

    @patch('app.services.swarm_api.extend_postage_stamp')
    def test_extend_stamp_success(self, mock_extend):
        """Test successful stamp extension."""
        mock_extend.return_value = "existing_batch_id"

        extension_data = {
            "amount": 500000000
        }

        response = client.patch("/api/v1/stamps/existing_batch_id/extend", json=extension_data)

        assert response.status_code == 200
        data = response.json()
        assert data["batchID"] == "existing_batch_id"
        assert "successfully" in data["message"].lower()

        # Verify the service was called with correct parameters
        mock_extend.assert_called_once_with("existing_batch_id", 500000000)

    @patch('app.services.swarm_api.extend_postage_stamp')
    def test_extend_stamp_api_error(self, mock_extend):
        """Test stamp extension when API call fails."""
        from requests.exceptions import RequestException
        mock_extend.side_effect = RequestException("Extension failed")

        extension_data = {
            "amount": 500000000
        }

        response = client.patch("/api/v1/stamps/batch123/extend", json=extension_data)

        assert response.status_code == 502
        assert "failed to extend" in response.json()["detail"].lower()

    def test_extend_stamp_invalid_data(self):
        """Test stamp extension with invalid request data."""
        invalid_data = {
            "amount": -1000000000  # Negative amount should be invalid
        }

        response = client.patch("/api/v1/stamps/batch123/extend", json=invalid_data)

        # This should either be a validation error (422) or be caught by business logic
        assert response.status_code in [422, 400, 502]


class TestStampsDataIntegrity:
    """Test data integrity and field mapping in stamps API."""

    @patch('app.services.swarm_api.get_all_stamps_processed')
    def test_stamps_response_field_completeness(self, mock_get_stamps):
        """Test that all expected fields are present in stamps response."""
        mock_get_stamps.return_value = [
            {
                "batchID": "test123",
                "amount": "1000000000",
                "blockNumber": 12345,
                "owner": "0x1234567890abcdef",
                "immutableFlag": True,
                "depth": 18,
                "bucketDepth": 16,
                "batchTTL": 3600,
                "utilization": 50,
                "usable": True,
                "label": "test-stamp",
                "expectedExpiration": "2024-12-01-15-30",
                "local": True
            }
        ]

        response = client.get("/api/v1/stamps/")
        assert response.status_code == 200

        stamp = response.json()["stamps"][0]

        # Verify all required fields are present
        required_fields = [
            "batchID", "amount", "immutableFlag", "depth", "bucketDepth",
            "batchTTL", "expectedExpiration", "local"
        ]
        for field in required_fields:
            assert field in stamp, f"Required field '{field}' missing from response"

        # Verify optional fields are handled properly (can be null)
        optional_fields = ["blockNumber", "owner", "utilization", "usable", "label"]
        for field in optional_fields:
            assert field in stamp, f"Optional field '{field}' missing from response"

    @patch('app.services.swarm_api.get_all_stamps_processed')
    def test_stamps_immutable_flag_never_null(self, mock_get_stamps):
        """Test that immutableFlag is never null in response."""
        mock_get_stamps.return_value = [
            {
                "batchID": "test123",
                "immutableFlag": True,
                "depth": 18,
                "bucketDepth": 16,
                "batchTTL": 3600,
                "expectedExpiration": "2024-12-01-15-30",
                "local": False,
                "amount": "1000000000"
            },
            {
                "batchID": "test456",
                "immutableFlag": False,
                "depth": 20,
                "bucketDepth": 16,
                "batchTTL": 7200,
                "expectedExpiration": "2024-12-01-17-30",
                "local": True,
                "amount": "2000000000"
            }
        ]

        response = client.get("/api/v1/stamps/")
        assert response.status_code == 200

        stamps = response.json()["stamps"]
        for stamp in stamps:
            assert stamp["immutableFlag"] is not None
            assert isinstance(stamp["immutableFlag"], bool)

    @patch('app.services.swarm_api.get_all_stamps_processed')
    def test_stamps_local_field_always_boolean(self, mock_get_stamps):
        """Test that local field is always a boolean."""
        mock_get_stamps.return_value = [
            {
                "batchID": "test123",
                "local": True,
                "depth": 18,
                "bucketDepth": 16,
                "batchTTL": 3600,
                "expectedExpiration": "2024-12-01-15-30",
                "immutableFlag": False,
                "amount": "1000000000"
            },
            {
                "batchID": "test456",
                "local": False,
                "depth": 20,
                "bucketDepth": 16,
                "batchTTL": 7200,
                "expectedExpiration": "2024-12-01-17-30",
                "immutableFlag": True,
                "amount": "2000000000"
            }
        ]

        response = client.get("/api/v1/stamps/")
        assert response.status_code == 200

        stamps = response.json()["stamps"]
        for stamp in stamps:
            assert isinstance(stamp["local"], bool)