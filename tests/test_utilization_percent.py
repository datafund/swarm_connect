# tests/test_utilization_percent.py
"""Tests for utilization percentage calculation."""
import pytest
from unittest.mock import patch, MagicMock

from app.services.swarm_api import calculate_utilization_percent


class TestCalculateUtilizationPercent:
    """Unit tests for the calculate_utilization_percent function."""

    def test_basic_calculation(self):
        """Test basic percentage calculation."""
        # depth=17, bucketDepth=16 -> totalBuckets = 2^1 = 2
        # utilization=1 -> 1/2 * 100 = 50%
        result = calculate_utilization_percent(1, 17, 16)
        assert result == 50.0

    def test_full_utilization(self):
        """Test 100% utilization."""
        # depth=17, bucketDepth=16 -> totalBuckets = 2
        # utilization=2 -> 2/2 * 100 = 100%
        result = calculate_utilization_percent(2, 17, 16)
        assert result == 100.0

    def test_zero_utilization(self):
        """Test 0% utilization."""
        result = calculate_utilization_percent(0, 20, 16)
        assert result == 0.0

    def test_larger_depth_difference(self):
        """Test with larger depth difference."""
        # depth=20, bucketDepth=16 -> totalBuckets = 2^4 = 16
        # utilization=4 -> 4/16 * 100 = 25%
        result = calculate_utilization_percent(4, 20, 16)
        assert result == 25.0

    def test_depth_22(self):
        """Test with depth 22."""
        # depth=22, bucketDepth=16 -> totalBuckets = 2^6 = 64
        # utilization=32 -> 32/64 * 100 = 50%
        result = calculate_utilization_percent(32, 22, 16)
        assert result == 50.0

    def test_equal_depth_and_bucket_depth(self):
        """Test when depth equals bucketDepth (1 bucket total)."""
        # depth=16, bucketDepth=16 -> totalBuckets = 2^0 = 1
        # utilization=1 -> 1/1 * 100 = 100%
        result = calculate_utilization_percent(1, 16, 16)
        assert result == 100.0

    def test_partial_percent_rounded(self):
        """Test that result is rounded to 2 decimal places."""
        # depth=20, bucketDepth=16 -> totalBuckets = 16
        # utilization=1 -> 1/16 * 100 = 6.25%
        result = calculate_utilization_percent(1, 20, 16)
        assert result == 6.25

    def test_more_precision_rounding(self):
        """Test rounding with values that produce more decimal places."""
        # depth=20, bucketDepth=16 -> totalBuckets = 16
        # utilization=3 -> 3/16 * 100 = 18.75%
        result = calculate_utilization_percent(3, 20, 16)
        assert result == 18.75

    def test_capped_at_100(self):
        """Test that result is capped at 100% (defensive)."""
        # If somehow utilization > totalBuckets
        # depth=17, bucketDepth=16 -> totalBuckets = 2
        # utilization=5 -> would be 250%, capped to 100%
        result = calculate_utilization_percent(5, 17, 16)
        assert result == 100.0

    def test_none_utilization(self):
        """Test that None utilization returns None."""
        result = calculate_utilization_percent(None, 20, 16)
        assert result is None

    def test_none_depth(self):
        """Test that None depth returns None."""
        result = calculate_utilization_percent(5, None, 16)
        assert result is None

    def test_none_bucket_depth(self):
        """Test that None bucketDepth returns None."""
        result = calculate_utilization_percent(5, 20, None)
        assert result is None

    def test_all_none(self):
        """Test that all None values return None."""
        result = calculate_utilization_percent(None, None, None)
        assert result is None


class TestUtilizationPercentInStampList:
    """Integration tests for utilizationPercent in stamp list endpoint."""

    @patch('app.services.swarm_api.get_all_stamps')
    @patch('app.services.swarm_api.get_local_stamps')
    def test_utilization_percent_in_processed_stamps(self, mock_local, mock_global):
        """Test that get_all_stamps_processed includes utilizationPercent."""
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
        assert result[0]["utilization"] == 1

    @patch('app.services.swarm_api.get_all_stamps')
    @patch('app.services.swarm_api.get_local_stamps')
    def test_utilization_percent_null_when_no_utilization(self, mock_local, mock_global):
        """Test that utilizationPercent is None when utilization is not available."""
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
        mock_local.return_value = []  # No local data

        result = get_all_stamps_processed()

        assert len(result) == 1
        assert result[0]["utilizationPercent"] is None
        assert result[0]["utilization"] is None


class TestUtilizationPercentInAPI:
    """Tests for utilizationPercent in API responses."""

    @patch('app.services.swarm_api.get_all_stamps_processed')
    def test_stamps_list_includes_utilization_percent(self, mock_processed):
        """Test that /api/v1/stamps/ includes utilizationPercent."""
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
        assert data["stamps"][0]["utilizationPercent"] == 25.0

    @patch('app.services.swarm_api.get_all_stamps_processed')
    def test_single_stamp_includes_utilization_percent(self, mock_processed):
        """Test that /api/v1/stamps/{id} includes utilizationPercent."""
        from fastapi.testclient import TestClient
        from app.main import app

        mock_processed.return_value = [
            {
                "batchID": "abc123",
                "amount": "1000000",
                "depth": 20,
                "bucketDepth": 16,
                "batchTTL": 86400,
                "utilization": 8,
                "utilizationPercent": 50.0,
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
        assert data["utilizationPercent"] == 50.0
        assert data["utilization"] == 8

    @patch('app.services.swarm_api.get_all_stamps_processed')
    def test_utilization_percent_null_in_response(self, mock_processed):
        """Test that utilizationPercent can be null in response."""
        from fastapi.testclient import TestClient
        from app.main import app

        mock_processed.return_value = [
            {
                "batchID": "abc123",
                "amount": "1000000",
                "depth": 20,
                "bucketDepth": 16,
                "batchTTL": 86400,
                "utilization": None,
                "utilizationPercent": None,
                "usable": None,
                "label": None,
                "blockNumber": None,
                "owner": None,
                "immutableFlag": None,
                "expectedExpiration": "2025-01-01-00-00",
                "local": False
            }
        ]

        client = TestClient(app)
        response = client.get("/api/v1/stamps/abc123")

        assert response.status_code == 200
        data = response.json()
        assert data["utilizationPercent"] is None
        assert data["utilization"] is None
