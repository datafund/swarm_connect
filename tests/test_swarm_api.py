# tests/test_swarm_api.py
import pytest
from unittest.mock import patch, MagicMock
import datetime
from typing import Dict, Any, List

from app.services.swarm_api import (
    get_all_stamps,
    get_local_stamps,
    merge_stamp_data,
    calculate_usable_status,
    get_all_stamps_processed,
    get_chainstate,
    calculate_stamp_amount,
    calculate_stamp_total_cost,
    check_sufficient_funds
)


class TestSwarmAPIFunctions:
    """Test suite for Swarm API service functions."""

    def test_merge_stamp_data_with_local_data(self):
        """Test merging global stamp data with local stamp information."""
        global_stamp = {
            "batchID": "test123",
            "amount": "1000000000",
            "immutable": True,
            "depth": 18,
            "bucketDepth": 16,
            "batchTTL": 3600,
            "blockNumber": None,
            "owner": None
        }

        local_stamp = {
            "batchID": "test123",
            "utilization": 50,
            "usable": True,
            "label": "my-test-stamp",
            "amount": 8000000000,
            "owner": "0x1234567890abcdef",
            "immutableFlag": True,
            "blockNumber": 12345
        }

        result = merge_stamp_data(global_stamp, local_stamp)

        # Local data should take precedence
        assert result["utilization"] == 50
        assert result["usable"] is True
        assert result["label"] == "my-test-stamp"
        assert result["amount"] == "8000000000"  # Converted to string
        assert result["owner"] == "0x1234567890abcdef"
        assert result["blockNumber"] == 12345
        assert result["immutableFlag"] is True

        # Global data should remain for non-overridden fields
        assert result["batchID"] == "test123"
        assert result["depth"] == 18
        assert result["bucketDepth"] == 16

    def test_merge_stamp_data_immutable_field_mapping(self):
        """Test that global 'immutable' field maps to 'immutableFlag'."""
        global_stamp = {
            "batchID": "test123",
            "immutable": True,  # Global API uses 'immutable'
            "depth": 18
        }

        local_stamp = None  # No local data

        result = merge_stamp_data(global_stamp, local_stamp)

        # Should map global 'immutable' to 'immutableFlag'
        assert result["immutableFlag"] is True
        assert "immutable" not in result or result.get("immutable") is True

    def test_merge_stamp_data_local_takes_precedence_for_immutable(self):
        """Test that local immutableFlag takes precedence over global immutable."""
        global_stamp = {
            "batchID": "test123",
            "immutable": True,
            "depth": 18
        }

        local_stamp = {
            "batchID": "test123",
            "immutableFlag": False  # Local overrides global
        }

        result = merge_stamp_data(global_stamp, local_stamp)

        # Local immutableFlag should take precedence
        assert result["immutableFlag"] is False

    def test_calculate_usable_status_valid_stamp(self):
        """Test usability calculation for a valid stamp."""
        stamp = {
            "exists": True,
            "batchTTL": 7200,  # 2 hours
            "immutableFlag": False,
            "depth": 18
        }

        result = calculate_usable_status(stamp)
        assert result is True

    def test_calculate_usable_status_expired_stamp(self):
        """Test usability calculation for an expired stamp."""
        stamp = {
            "exists": True,
            "batchTTL": 0,  # Expired
            "immutableFlag": False,
            "depth": 18
        }

        result = calculate_usable_status(stamp)
        assert result is False

    def test_calculate_usable_status_immutable_stamp_low_ttl(self):
        """Test usability calculation for immutable stamp with low TTL."""
        stamp = {
            "exists": True,
            "batchTTL": 1800,  # 30 minutes - below immutable threshold
            "immutableFlag": True,
            "depth": 18
        }

        result = calculate_usable_status(stamp)
        assert result is False

    def test_calculate_usable_status_invalid_depth(self):
        """Test usability calculation for stamp with invalid depth."""
        stamp = {
            "exists": True,
            "batchTTL": 7200,
            "immutableFlag": False,
            "depth": 10  # Too low
        }

        result = calculate_usable_status(stamp)
        assert result is False

    def test_calculate_usable_status_nonexistent_stamp(self):
        """Test usability calculation for non-existent stamp."""
        stamp = {
            "exists": False,
            "batchTTL": 7200,
            "immutableFlag": False,
            "depth": 18
        }

        result = calculate_usable_status(stamp)
        assert result is False

    @patch('app.services.swarm_api.requests.get')
    def test_get_all_stamps_success(self, mock_get):
        """Test successful retrieval of all stamps."""
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "batches": [
                {"batchID": "test123", "amount": "1000000000", "depth": 18},
                {"batchID": "test456", "amount": "8000000000", "depth": 20}
            ]
        }
        mock_get.return_value = mock_response

        result = get_all_stamps()

        assert len(result) == 2
        assert result[0]["batchID"] == "test123"
        assert result[1]["batchID"] == "test456"

    @patch('app.services.swarm_api.requests.get')
    def test_get_all_stamps_direct_list_response(self, mock_get):
        """Test handling of direct list response from API."""
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = [
            {"batchID": "test123", "amount": "1000000000"},
            {"batchID": "test456", "amount": "8000000000"}
        ]
        mock_get.return_value = mock_response

        result = get_all_stamps()

        assert len(result) == 2
        assert isinstance(result, list)

    @patch('app.services.swarm_api.requests.get')
    def test_get_local_stamps_success(self, mock_get):
        """Test successful retrieval of local stamps."""
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "stamps": [
                {"batchID": "test123", "utilization": 50, "usable": True},
                {"batchID": "test456", "utilization": 30, "usable": True}
            ]
        }
        mock_get.return_value = mock_response

        result = get_local_stamps()

        assert len(result) == 2
        assert result[0]["utilization"] == 50
        assert result[1]["utilization"] == 30

    @patch('app.services.swarm_api.requests.get')
    def test_get_local_stamps_failure_returns_empty(self, mock_get):
        """Test that local stamps failure returns empty list without raising."""
        mock_get.side_effect = Exception("Network error")

        result = get_local_stamps()

        assert result == []

    @patch('app.services.swarm_api.get_local_stamps')
    @patch('app.services.swarm_api.get_all_stamps')
    def test_get_all_stamps_processed_integration(self, mock_global, mock_local):
        """Test complete stamp processing with data merging."""
        # Mock global stamps data
        mock_global.return_value = [
            {
                "batchID": "local123",
                "amount": "1000000000",
                "immutable": True,
                "depth": 18,
                "bucketDepth": 16,
                "batchTTL": 7200
            },
            {
                "batchID": "global456",
                "amount": "8000000000",
                "immutable": False,
                "depth": 20,
                "bucketDepth": 16,
                "batchTTL": 3600
            }
        ]

        # Mock local stamps data - only one stamp exists locally
        mock_local.return_value = [
            {
                "batchID": "local123",
                "utilization": 75,
                "usable": True,
                "label": "my-local-stamp",
                "immutableFlag": True
            }
        ]

        result = get_all_stamps_processed()

        assert len(result) == 2

        # First stamp should have local data merged
        local_stamp = next(s for s in result if s["batchID"] == "local123")
        assert local_stamp["local"] is True
        assert local_stamp["utilization"] == 75
        assert local_stamp["label"] == "my-local-stamp"
        assert local_stamp["immutableFlag"] is True

        # Second stamp should be global only
        global_stamp = next(s for s in result if s["batchID"] == "global456")
        assert global_stamp["local"] is False
        assert global_stamp["utilization"] is None
        assert global_stamp["label"] is None
        assert global_stamp["immutableFlag"] is False  # Mapped from 'immutable'

    @patch('app.services.swarm_api.get_local_stamps')
    @patch('app.services.swarm_api.get_all_stamps')
    def test_get_all_stamps_processed_expiration_calculation(self, mock_global, mock_local):
        """Test that expiration times are calculated correctly."""
        mock_global.return_value = [
            {
                "batchID": "test123",
                "batchTTL": 3600,  # 1 hour
                "depth": 18,
                "amount": "1000000000"
            }
        ]
        mock_local.return_value = []

        # Mock datetime module inside the function where it's imported
        mock_now = datetime.datetime(2024, 1, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)
        with patch('datetime.datetime') as mock_datetime_class:
            mock_datetime_class.now.return_value = mock_now
            mock_datetime_class.timedelta = datetime.timedelta
            mock_datetime_class.timezone = datetime.timezone

            result = get_all_stamps_processed()

            assert len(result) == 1
            # Should be 1 hour later: 2024-01-01-13-00
            assert result[0]["expectedExpiration"] == "2024-01-01-13-00"

    def test_merge_stamp_data_none_local_stamp(self):
        """Test merging when local stamp is None."""
        global_stamp = {
            "batchID": "test123",
            "immutable": True,
            "depth": 18
        }

        result = merge_stamp_data(global_stamp, None)

        # Should map immutable field and keep global data
        assert result["batchID"] == "test123"
        assert result["immutableFlag"] is True
        assert result["depth"] == 18


class TestStampCostCalculations:
    """Test suite for stamp cost calculation functions."""

    def test_calculate_stamp_amount_basic(self):
        """Test basic amount calculation from duration."""
        # 25 hours at price 100000 = 25 * 720 * 100000 = 1,800,000,000
        result = calculate_stamp_amount(25, 100000)
        assert result == 1800000000

    def test_calculate_stamp_amount_one_hour(self):
        """Test amount calculation for 1 hour."""
        # 1 hour at price 150000 = 1 * 720 * 150000 = 108,000,000
        result = calculate_stamp_amount(1, 150000)
        assert result == 108000000

    def test_calculate_stamp_amount_large_duration(self):
        """Test amount calculation for longer duration (30 days)."""
        # 720 hours (30 days) at price 100000 = 720 * 720 * 100000 = 51,840,000,000
        result = calculate_stamp_amount(720, 100000)
        assert result == 51840000000

    def test_calculate_stamp_total_cost_depth_17(self):
        """Test total cost calculation at depth 17."""
        # amount * 2^17 = 1000000000 * 131072 = 131,072,000,000,000
        result = calculate_stamp_total_cost(1000000000, 17)
        assert result == 131072000000000

    def test_calculate_stamp_total_cost_depth_20(self):
        """Test total cost calculation at depth 20."""
        # amount * 2^20 = 1000000000 * 1048576 = 1,048,576,000,000,000
        result = calculate_stamp_total_cost(1000000000, 20)
        assert result == 1048576000000000

    def test_calculate_stamp_total_cost_increases_with_depth(self):
        """Test that total cost increases exponentially with depth."""
        amount = 1000000000
        cost_17 = calculate_stamp_total_cost(amount, 17)
        cost_18 = calculate_stamp_total_cost(amount, 18)
        cost_19 = calculate_stamp_total_cost(amount, 19)

        # Each depth increase should double the cost
        assert cost_18 == cost_17 * 2
        assert cost_19 == cost_18 * 2

    @patch('app.services.swarm_api.requests.get')
    def test_get_chainstate_success(self, mock_get):
        """Test successful chainstate retrieval."""
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "chainTip": 43778502,
            "block": 43778495,
            "totalAmount": "464165918747",
            "currentPrice": "149324"
        }
        mock_get.return_value = mock_response

        result = get_chainstate()

        assert result["currentPrice"] == "149324"
        assert result["block"] == 43778495

    @patch('app.services.swarm_api.requests.get')
    def test_get_chainstate_missing_price(self, mock_get):
        """Test chainstate retrieval fails when currentPrice is missing."""
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "block": 43778495
            # Missing currentPrice
        }
        mock_get.return_value = mock_response

        with pytest.raises(ValueError) as excinfo:
            get_chainstate()

        assert "currentPrice" in str(excinfo.value)

    @patch('app.services.swarm_api.get_wallet_info')
    def test_check_sufficient_funds_enough(self, mock_wallet):
        """Test funds check when sufficient funds available."""
        mock_wallet.return_value = {
            "bzzBalance": "10000000000000000"  # 1 BZZ
        }

        # Request 0.5 BZZ worth
        result = check_sufficient_funds(5000000000000000)

        assert result["sufficient"] is True
        assert result["wallet_balance_bzz"] == 1.0
        assert result["required_bzz"] == 0.5
        assert result["shortfall_bzz"] == 0.0

    @patch('app.services.swarm_api.get_wallet_info')
    def test_check_sufficient_funds_not_enough(self, mock_wallet):
        """Test funds check when insufficient funds available."""
        mock_wallet.return_value = {
            "bzzBalance": "5000000000000000"  # 0.5 BZZ
        }

        # Request 1 BZZ worth
        result = check_sufficient_funds(10000000000000000)

        assert result["sufficient"] is False
        assert result["wallet_balance_bzz"] == 0.5
        assert result["required_bzz"] == 1.0
        assert result["shortfall_bzz"] == 0.5

    @patch('app.services.swarm_api.get_wallet_info')
    def test_check_sufficient_funds_exact_amount(self, mock_wallet):
        """Test funds check when exactly enough funds available."""
        mock_wallet.return_value = {
            "bzzBalance": "10000000000000000"  # 1 BZZ
        }

        # Request exactly 1 BZZ
        result = check_sufficient_funds(10000000000000000)

        assert result["sufficient"] is True
        assert result["shortfall_bzz"] == 0.0

    def test_calculate_stamp_amount_zero_price(self):
        """Test amount calculation with zero price."""
        result = calculate_stamp_amount(25, 0)
        assert result == 0

    def test_calculate_stamp_total_cost_zero_amount(self):
        """Test total cost with zero amount."""
        result = calculate_stamp_total_cost(0, 17)
        assert result == 0