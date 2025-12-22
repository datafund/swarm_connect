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
                "amount": "8000000000",
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
                "amount": "8000000000",
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
        assert "swarm bee node" in response.json()["detail"].lower()

    @patch('app.services.swarm_api.check_sufficient_funds')
    @patch('app.services.swarm_api.purchase_postage_stamp')
    def test_purchase_stamp_success_with_amount(self, mock_purchase, mock_funds):
        """Test successful stamp purchase with legacy amount."""
        mock_purchase.return_value = "new_batch_id_123"
        mock_funds.return_value = {"sufficient": True, "wallet_balance_bzz": 10.0, "required_bzz": 1.0, "shortfall_bzz": 0.0}

        purchase_data = {
            "amount": 8000000000,
            "depth": 17,
            "label": "test-purchase"
        }

        response = client.post("/api/v1/stamps/", json=purchase_data)

        assert response.status_code == 201
        data = response.json()
        assert data["batchID"] == "new_batch_id_123"
        assert "successfully" in data["message"].lower()

        # Verify the service was called with correct parameters
        mock_purchase.assert_called_once_with(amount=8000000000, depth=17, label="test-purchase")

    @patch('app.services.swarm_api.check_sufficient_funds')
    @patch('app.services.swarm_api.calculate_stamp_total_cost')
    @patch('app.services.swarm_api.calculate_stamp_amount')
    @patch('app.services.swarm_api.get_chainstate')
    @patch('app.services.swarm_api.purchase_postage_stamp')
    def test_purchase_stamp_with_duration(self, mock_purchase, mock_chainstate, mock_calc_amount, mock_calc_cost, mock_funds):
        """Test stamp purchase with duration_hours."""
        mock_purchase.return_value = "new_batch_id_duration"
        mock_chainstate.return_value = {"currentPrice": "100000"}
        mock_calc_amount.return_value = 1800000000  # 25 hours worth
        mock_calc_cost.return_value = 235929600000000  # total cost
        mock_funds.return_value = {"sufficient": True, "wallet_balance_bzz": 10.0, "required_bzz": 0.024, "shortfall_bzz": 0.0}

        purchase_data = {
            "duration_hours": 25
        }

        response = client.post("/api/v1/stamps/", json=purchase_data)

        assert response.status_code == 201
        data = response.json()
        assert data["batchID"] == "new_batch_id_duration"

        # Verify chainstate was called to get current price
        mock_chainstate.assert_called_once()
        # Verify amount was calculated from duration
        mock_calc_amount.assert_called_once_with(25, 100000)

    @patch('app.services.swarm_api.check_sufficient_funds')
    @patch('app.services.swarm_api.calculate_stamp_total_cost')
    @patch('app.services.swarm_api.calculate_stamp_amount')
    @patch('app.services.swarm_api.get_chainstate')
    @patch('app.services.swarm_api.purchase_postage_stamp')
    def test_purchase_stamp_with_defaults(self, mock_purchase, mock_chainstate, mock_calc_amount, mock_calc_cost, mock_funds):
        """Test stamp purchase with empty body uses defaults (25 hours, depth 17)."""
        mock_purchase.return_value = "new_batch_id_defaults"
        mock_chainstate.return_value = {"currentPrice": "100000"}
        mock_calc_amount.return_value = 1800000000
        mock_calc_cost.return_value = 235929600000000
        mock_funds.return_value = {"sufficient": True, "wallet_balance_bzz": 10.0, "required_bzz": 0.024, "shortfall_bzz": 0.0}

        response = client.post("/api/v1/stamps/", json={})

        assert response.status_code == 201
        data = response.json()
        assert data["batchID"] == "new_batch_id_defaults"

        # Verify default duration of 25 hours was used
        mock_calc_amount.assert_called_once_with(25, 100000)
        # Verify default depth of 17 was used (small size default)
        mock_purchase.assert_called_once_with(amount=1800000000, depth=17, label=None)

    @patch('app.services.swarm_api.check_sufficient_funds')
    @patch('app.services.swarm_api.calculate_stamp_total_cost')
    @patch('app.services.swarm_api.calculate_stamp_amount')
    @patch('app.services.swarm_api.get_chainstate')
    @patch('app.services.swarm_api.purchase_postage_stamp')
    def test_purchase_stamp_with_size_small(self, mock_purchase, mock_chainstate, mock_calc_amount, mock_calc_cost, mock_funds):
        """Test stamp purchase with size='small' uses depth 17."""
        mock_purchase.return_value = "new_batch_id_small"
        mock_chainstate.return_value = {"currentPrice": "100000"}
        mock_calc_amount.return_value = 1800000000
        mock_calc_cost.return_value = 235929600000000
        mock_funds.return_value = {"sufficient": True, "wallet_balance_bzz": 10.0, "required_bzz": 0.024, "shortfall_bzz": 0.0}

        response = client.post("/api/v1/stamps/", json={"size": "small"})

        assert response.status_code == 201
        mock_purchase.assert_called_once_with(amount=1800000000, depth=17, label=None)

    @patch('app.services.swarm_api.check_sufficient_funds')
    @patch('app.services.swarm_api.calculate_stamp_total_cost')
    @patch('app.services.swarm_api.calculate_stamp_amount')
    @patch('app.services.swarm_api.get_chainstate')
    @patch('app.services.swarm_api.purchase_postage_stamp')
    def test_purchase_stamp_with_size_medium(self, mock_purchase, mock_chainstate, mock_calc_amount, mock_calc_cost, mock_funds):
        """Test stamp purchase with size='medium' uses depth 20."""
        mock_purchase.return_value = "new_batch_id_medium"
        mock_chainstate.return_value = {"currentPrice": "100000"}
        mock_calc_amount.return_value = 1800000000
        mock_calc_cost.return_value = 1887436800000000
        mock_funds.return_value = {"sufficient": True, "wallet_balance_bzz": 10.0, "required_bzz": 0.19, "shortfall_bzz": 0.0}

        response = client.post("/api/v1/stamps/", json={"size": "medium"})

        assert response.status_code == 201
        mock_purchase.assert_called_once_with(amount=1800000000, depth=20, label=None)

    @patch('app.services.swarm_api.check_sufficient_funds')
    @patch('app.services.swarm_api.calculate_stamp_total_cost')
    @patch('app.services.swarm_api.calculate_stamp_amount')
    @patch('app.services.swarm_api.get_chainstate')
    @patch('app.services.swarm_api.purchase_postage_stamp')
    def test_purchase_stamp_with_size_large(self, mock_purchase, mock_chainstate, mock_calc_amount, mock_calc_cost, mock_funds):
        """Test stamp purchase with size='large' uses depth 22."""
        mock_purchase.return_value = "new_batch_id_large"
        mock_chainstate.return_value = {"currentPrice": "100000"}
        mock_calc_amount.return_value = 1800000000
        mock_calc_cost.return_value = 7549747200000000
        mock_funds.return_value = {"sufficient": True, "wallet_balance_bzz": 10.0, "required_bzz": 0.75, "shortfall_bzz": 0.0}

        response = client.post("/api/v1/stamps/", json={"size": "large"})

        assert response.status_code == 201
        mock_purchase.assert_called_once_with(amount=1800000000, depth=22, label=None)

    @patch('app.services.swarm_api.check_sufficient_funds')
    @patch('app.services.swarm_api.calculate_stamp_total_cost')
    @patch('app.services.swarm_api.calculate_stamp_amount')
    @patch('app.services.swarm_api.get_chainstate')
    @patch('app.services.swarm_api.purchase_postage_stamp')
    def test_purchase_stamp_size_overrides_depth(self, mock_purchase, mock_chainstate, mock_calc_amount, mock_calc_cost, mock_funds):
        """Test that size parameter overrides explicit depth."""
        mock_purchase.return_value = "new_batch_id_override"
        mock_chainstate.return_value = {"currentPrice": "100000"}
        mock_calc_amount.return_value = 1800000000
        mock_calc_cost.return_value = 1887436800000000
        mock_funds.return_value = {"sufficient": True, "wallet_balance_bzz": 10.0, "required_bzz": 0.19, "shortfall_bzz": 0.0}

        # size="medium" should override depth=17
        response = client.post("/api/v1/stamps/", json={"size": "medium", "depth": 17})

        assert response.status_code == 201
        # Should use depth 20 from size="medium", not 17
        mock_purchase.assert_called_once_with(amount=1800000000, depth=20, label=None)

    def test_purchase_stamp_invalid_size(self):
        """Test stamp purchase with invalid size value."""
        response = client.post("/api/v1/stamps/", json={"size": "extra-large"})

        assert response.status_code == 422  # Validation error

    @patch('app.services.swarm_api.check_sufficient_funds')
    @patch('app.services.swarm_api.calculate_stamp_total_cost')
    @patch('app.services.swarm_api.purchase_postage_stamp')
    def test_purchase_stamp_insufficient_funds(self, mock_purchase, mock_calc_cost, mock_funds):
        """Test stamp purchase fails with insufficient funds."""
        mock_calc_cost.return_value = 1000000000000000000  # Very high cost
        mock_funds.return_value = {
            "sufficient": False,
            "wallet_balance_bzz": 0.5,
            "required_bzz": 100.0,
            "shortfall_bzz": 99.5
        }

        purchase_data = {
            "amount": 8000000000,
            "depth": 17
        }

        response = client.post("/api/v1/stamps/", json=purchase_data)

        assert response.status_code == 400
        assert "insufficient funds" in response.json()["detail"].lower()
        assert "99.5" in response.json()["detail"]  # shortfall amount
        # Verify purchase was never called
        mock_purchase.assert_not_called()

    @patch('app.services.swarm_api.check_sufficient_funds')
    @patch('app.services.swarm_api.calculate_stamp_total_cost')
    @patch('app.services.swarm_api.purchase_postage_stamp')
    def test_purchase_stamp_api_error(self, mock_purchase, mock_calc_cost, mock_funds):
        """Test stamp purchase when API call fails."""
        from requests.exceptions import RequestException
        mock_calc_cost.return_value = 131072000000000
        mock_funds.return_value = {"sufficient": True, "wallet_balance_bzz": 10.0, "required_bzz": 0.013, "shortfall_bzz": 0.0}
        mock_purchase.side_effect = RequestException("Purchase failed")

        purchase_data = {
            "amount": 1000000000,
            "depth": 18
        }

        response = client.post("/api/v1/stamps/", json=purchase_data)

        assert response.status_code == 502
        assert "could not purchase" in response.json()["detail"].lower()

    def test_purchase_stamp_invalid_data(self):
        """Test stamp purchase with invalid request data."""
        invalid_data = {
            "amount": "not_a_number",
            "depth": "also_not_a_number"
        }

        response = client.post("/api/v1/stamps/", json=invalid_data)

        assert response.status_code == 422  # Validation error

    @patch('app.services.swarm_api.check_sufficient_funds')
    @patch('app.services.swarm_api.calculate_stamp_total_cost')
    @patch('app.services.swarm_api.get_all_stamps_processed')
    @patch('app.services.swarm_api.extend_postage_stamp')
    def test_extend_stamp_success_with_amount(self, mock_extend, mock_get_stamps, mock_calc_cost, mock_funds):
        """Test successful stamp extension with legacy amount."""
        mock_extend.return_value = "existing_batch_id"
        mock_get_stamps.return_value = [
            {"batchID": "existing_batch_id", "depth": 17, "amount": "1000000000", "batchTTL": 3600,
             "bucketDepth": 16, "expectedExpiration": "2024-12-01-15-30", "local": True, "immutableFlag": False}
        ]
        mock_calc_cost.return_value = 1048576000000000
        mock_funds.return_value = {"sufficient": True, "wallet_balance_bzz": 10.0, "required_bzz": 0.1, "shortfall_bzz": 0.0}

        extension_data = {
            "amount": 8000000000
        }

        response = client.patch("/api/v1/stamps/existing_batch_id/extend", json=extension_data)

        assert response.status_code == 200
        data = response.json()
        assert data["batchID"] == "existing_batch_id"
        assert "successfully" in data["message"].lower()

        # Verify the service was called with correct parameters
        mock_extend.assert_called_once_with(stamp_id="existing_batch_id", amount=8000000000)

    @patch('app.services.swarm_api.check_sufficient_funds')
    @patch('app.services.swarm_api.calculate_stamp_total_cost')
    @patch('app.services.swarm_api.calculate_stamp_amount')
    @patch('app.services.swarm_api.get_chainstate')
    @patch('app.services.swarm_api.get_all_stamps_processed')
    @patch('app.services.swarm_api.extend_postage_stamp')
    def test_extend_stamp_with_duration(self, mock_extend, mock_get_stamps, mock_chainstate, mock_calc_amount, mock_calc_cost, mock_funds):
        """Test stamp extension with duration_hours."""
        mock_extend.return_value = "existing_batch_id"
        mock_get_stamps.return_value = [
            {"batchID": "existing_batch_id", "depth": 17, "amount": "1000000000", "batchTTL": 3600,
             "bucketDepth": 16, "expectedExpiration": "2024-12-01-15-30", "local": True, "immutableFlag": False}
        ]
        mock_chainstate.return_value = {"currentPrice": "100000"}
        mock_calc_amount.return_value = 1800000000
        mock_calc_cost.return_value = 235929600000000
        mock_funds.return_value = {"sufficient": True, "wallet_balance_bzz": 10.0, "required_bzz": 0.024, "shortfall_bzz": 0.0}

        extension_data = {
            "duration_hours": 25
        }

        response = client.patch("/api/v1/stamps/existing_batch_id/extend", json=extension_data)

        assert response.status_code == 200
        data = response.json()
        assert data["batchID"] == "existing_batch_id"

        # Verify amount was calculated from duration
        mock_calc_amount.assert_called_once_with(25, 100000)

    @patch('app.services.swarm_api.check_sufficient_funds')
    @patch('app.services.swarm_api.calculate_stamp_total_cost')
    @patch('app.services.swarm_api.calculate_stamp_amount')
    @patch('app.services.swarm_api.get_chainstate')
    @patch('app.services.swarm_api.get_all_stamps_processed')
    @patch('app.services.swarm_api.extend_postage_stamp')
    def test_extend_stamp_with_defaults(self, mock_extend, mock_get_stamps, mock_chainstate, mock_calc_amount, mock_calc_cost, mock_funds):
        """Test stamp extension with empty body uses default 25 hours."""
        mock_extend.return_value = "existing_batch_id"
        mock_get_stamps.return_value = [
            {"batchID": "existing_batch_id", "depth": 17, "amount": "1000000000", "batchTTL": 3600,
             "bucketDepth": 16, "expectedExpiration": "2024-12-01-15-30", "local": True, "immutableFlag": False}
        ]
        mock_chainstate.return_value = {"currentPrice": "100000"}
        mock_calc_amount.return_value = 1800000000
        mock_calc_cost.return_value = 235929600000000
        mock_funds.return_value = {"sufficient": True, "wallet_balance_bzz": 10.0, "required_bzz": 0.024, "shortfall_bzz": 0.0}

        response = client.patch("/api/v1/stamps/existing_batch_id/extend", json={})

        assert response.status_code == 200

        # Verify default duration of 25 hours was used
        mock_calc_amount.assert_called_once_with(25, 100000)

    @patch('app.services.swarm_api.get_all_stamps_processed')
    def test_extend_stamp_not_found(self, mock_get_stamps):
        """Test stamp extension fails when stamp not found."""
        mock_get_stamps.return_value = [
            {"batchID": "other_batch", "depth": 17, "amount": "1000000000", "batchTTL": 3600,
             "bucketDepth": 16, "expectedExpiration": "2024-12-01-15-30", "local": True, "immutableFlag": False}
        ]

        response = client.patch("/api/v1/stamps/nonexistent_batch/extend", json={})

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    @patch('app.services.swarm_api.check_sufficient_funds')
    @patch('app.services.swarm_api.calculate_stamp_total_cost')
    @patch('app.services.swarm_api.get_all_stamps_processed')
    @patch('app.services.swarm_api.extend_postage_stamp')
    def test_extend_stamp_api_error(self, mock_extend, mock_get_stamps, mock_calc_cost, mock_funds):
        """Test stamp extension when API call fails."""
        from requests.exceptions import RequestException
        mock_get_stamps.return_value = [
            {"batchID": "batch123", "depth": 17, "amount": "1000000000", "batchTTL": 3600,
             "bucketDepth": 16, "expectedExpiration": "2024-12-01-15-30", "local": True, "immutableFlag": False}
        ]
        mock_calc_cost.return_value = 1048576000000000
        mock_funds.return_value = {"sufficient": True, "wallet_balance_bzz": 10.0, "required_bzz": 0.1, "shortfall_bzz": 0.0}
        mock_extend.side_effect = RequestException("Extension failed")

        extension_data = {
            "amount": 8000000000
        }

        response = client.patch("/api/v1/stamps/batch123/extend", json=extension_data)

        assert response.status_code == 502
        assert "could not extend" in response.json()["detail"].lower()

    @patch('app.services.swarm_api.get_all_stamps_processed')
    def test_extend_stamp_invalid_data(self, mock_get_stamps):
        """Test stamp extension with invalid request data."""
        mock_get_stamps.return_value = [
            {"batchID": "batch123", "depth": 17, "amount": "1000000000", "batchTTL": 3600,
             "bucketDepth": 16, "expectedExpiration": "2024-12-01-15-30", "local": True, "immutableFlag": False}
        ]

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
                "blockNumber": 12345,  # This will be mapped to 'start' in the response
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
        optional_fields = ["start", "owner", "utilization", "usable", "label"]
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
                "amount": "8000000000"
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
                "amount": "8000000000"
            }
        ]

        response = client.get("/api/v1/stamps/")
        assert response.status_code == 200

        stamps = response.json()["stamps"]
        for stamp in stamps:
            assert isinstance(stamp["local"], bool)