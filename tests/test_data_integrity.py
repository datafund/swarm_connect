# tests/test_data_integrity.py
"""
Data integrity and consistency tests for the stamps API.
Ensures data merging, field mapping, and calculations work correctly.
"""
import pytest
from unittest.mock import patch, MagicMock
import datetime
from fastapi.testclient import TestClient

from app.main import app
from app.services.swarm_api import merge_stamp_data, calculate_usable_status

client = TestClient(app)


class TestDataMerging:
    """Tests for data merging between global and local stamp sources."""

    def test_merge_priority_local_over_global(self):
        """Test that local data takes priority over global data."""
        global_stamp = {
            "batchID": "test123",
            "amount": "1000000000",
            "owner": "global_owner",
            "immutable": True,
            "utilization": None,
            "usable": None,
            "label": None
        }

        local_stamp = {
            "batchID": "test123",
            "amount": 2000000000,  # Different amount
            "owner": "local_owner",  # Different owner
            "immutableFlag": False,  # Different immutability
            "utilization": 50,
            "usable": True,
            "label": "local-label"
        }

        result = merge_stamp_data(global_stamp, local_stamp)

        # Local data should take priority
        assert result["amount"] == "2000000000"  # Local amount (converted to string)
        assert result["owner"] == "local_owner"
        assert result["immutableFlag"] is False  # Local immutableFlag
        assert result["utilization"] == 50
        assert result["usable"] is True
        assert result["label"] == "local-label"

    def test_merge_field_mapping_immutable_variations(self):
        """Test proper mapping between 'immutable' and 'immutableFlag' fields."""
        test_cases = [
            {
                "name": "global_immutable_true",
                "global": {"immutable": True},
                "local": None,
                "expected_flag": True
            },
            {
                "name": "global_immutable_false",
                "global": {"immutable": False},
                "local": None,
                "expected_flag": False
            },
            {
                "name": "local_overrides_global",
                "global": {"immutable": True},
                "local": {"immutableFlag": False},
                "expected_flag": False
            },
            {
                "name": "local_only",
                "global": {},
                "local": {"immutableFlag": True},
                "expected_flag": True
            }
        ]

        for case in test_cases:
            global_stamp = {"batchID": "test", **case["global"]}
            local_stamp = case["local"]

            result = merge_stamp_data(global_stamp, local_stamp)

            assert result["immutableFlag"] == case["expected_flag"], f"Failed case: {case['name']}"

    def test_merge_preserves_global_fields_when_local_missing(self):
        """Test that global fields are preserved when local data is missing."""
        global_stamp = {
            "batchID": "test123",
            "amount": "1000000000",
            "depth": 18,
            "bucketDepth": 16,
            "batchTTL": 3600,
            "blockNumber": 12345,
            "exists": True
        }

        local_stamp = None  # No local data

        result = merge_stamp_data(global_stamp, local_stamp)

        # Should preserve all global data
        assert result["batchID"] == "test123"
        assert result["depth"] == 18
        assert result["bucketDepth"] == 16
        assert result["batchTTL"] == 3600
        assert result["blockNumber"] == 12345
        assert result["exists"] is True

    def test_merge_handles_partial_local_data(self):
        """Test merging when local data only has some fields."""
        global_stamp = {
            "batchID": "test123",
            "amount": "1000000000",
            "owner": "global_owner",
            "depth": 18,
            "immutable": False,
            "utilization": None,
            "usable": None
        }

        local_stamp = {
            "batchID": "test123",
            "utilization": 75,  # Only utilization available locally
            # Missing: owner, amount, usable, etc.
        }

        result = merge_stamp_data(global_stamp, local_stamp)

        # Should merge utilization from local, keep global data for others
        assert result["utilization"] == 75  # From local
        assert result["owner"] == "global_owner"  # From global
        assert result["amount"] == "1000000000"  # From global
        assert result["immutableFlag"] is False  # Mapped from global


class TestUsabilityCalculation:
    """Tests for stamp usability calculation logic."""

    def test_usable_valid_stamp(self):
        """Test that valid stamps are marked as usable."""
        valid_stamps = [
            {
                "exists": True,
                "batchTTL": 7200,  # 2 hours
                "immutableFlag": False,
                "depth": 18
            },
            {
                "exists": True,
                "batchTTL": 86400,  # 24 hours
                "immutableFlag": True,
                "depth": 20
            }
        ]

        for stamp in valid_stamps:
            assert calculate_usable_status(stamp) is True

    def test_usable_invalid_stamps(self):
        """Test that invalid stamps are marked as not usable."""
        invalid_stamps = [
            {
                "name": "expired",
                "stamp": {
                    "exists": True,
                    "batchTTL": 0,  # Expired
                    "immutableFlag": False,
                    "depth": 18
                }
            },
            {
                "name": "non_existent",
                "stamp": {
                    "exists": False,
                    "batchTTL": 7200,
                    "immutableFlag": False,
                    "depth": 18
                }
            },
            {
                "name": "depth_too_low",
                "stamp": {
                    "exists": True,
                    "batchTTL": 7200,
                    "immutableFlag": False,
                    "depth": 10  # Too low
                }
            },
            {
                "name": "depth_too_high",
                "stamp": {
                    "exists": True,
                    "batchTTL": 7200,
                    "immutableFlag": False,
                    "depth": 40  # Too high
                }
            },
            {
                "name": "immutable_low_ttl",
                "stamp": {
                    "exists": True,
                    "batchTTL": 1800,  # 30 minutes - below immutable threshold
                    "immutableFlag": True,
                    "depth": 18
                }
            }
        ]

        for case in invalid_stamps:
            result = calculate_usable_status(case["stamp"])
            assert result is False, f"Expected False for case: {case['name']}"

    def test_usable_edge_cases(self):
        """Test usability calculation edge cases."""
        edge_cases = [
            {
                "name": "missing_exists_field",
                "stamp": {
                    "batchTTL": 7200,
                    "immutableFlag": False,
                    "depth": 18
                    # exists field missing - should default to True
                },
                "expected": True
            },
            {
                "name": "negative_ttl",
                "stamp": {
                    "exists": True,
                    "batchTTL": -100,  # Negative TTL
                    "immutableFlag": False,
                    "depth": 18
                },
                "expected": False
            },
            {
                "name": "string_ttl",
                "stamp": {
                    "exists": True,
                    "batchTTL": "invalid",  # String instead of int
                    "immutableFlag": False,
                    "depth": 18
                },
                "expected": False  # Should handle gracefully
            }
        ]

        for case in edge_cases:
            result = calculate_usable_status(case["stamp"])
            assert result == case["expected"], f"Failed case: {case['name']}"


class TestFieldConsistency:
    """Tests for field consistency across different API responses."""

    @patch('app.services.swarm_api.get_all_stamps_processed')
    def test_field_types_consistency(self, mock_get_stamps):
        """Test that field types are consistent across responses."""
        stamp_data = {
            "batchID": "type_test_123",
            "amount": "1500000000",  # Should be string
            "blockNumber": 12345,    # Should be int or None
            "owner": "0x1234567890abcdef",  # Should be string or None
            "immutableFlag": True,   # Should be boolean
            "depth": 18,             # Should be int
            "bucketDepth": 16,       # Should be int
            "batchTTL": 3600,        # Should be int
            "utilization": 75,       # Should be int or None
            "usable": True,          # Should be boolean or None
            "label": "test-label",   # Should be string or None
            "expectedExpiration": "2024-12-01-15-30",  # Should be string
            "local": True            # Should be boolean
        }

        mock_get_stamps.return_value = [stamp_data]

        # Test list endpoint
        list_response = client.get("/api/v1/stamps/")
        assert list_response.status_code == 200

        list_stamp = list_response.json()["stamps"][0]

        # Test detail endpoint
        detail_response = client.get(f"/api/v1/stamps/{stamp_data['batchID']}")
        assert detail_response.status_code == 200

        detail_stamp = detail_response.json()

        # Verify field types
        type_checks = [
            ("batchID", str),
            ("amount", str),
            ("immutableFlag", bool),
            ("depth", int),
            ("bucketDepth", int),
            ("batchTTL", int),
            ("expectedExpiration", str),
            ("local", bool)
        ]

        for field_name, expected_type in type_checks:
            list_value = list_stamp[field_name]
            detail_value = detail_stamp[field_name]

            assert isinstance(list_value, expected_type), f"List endpoint {field_name} wrong type"
            assert isinstance(detail_value, expected_type), f"Detail endpoint {field_name} wrong type"
            assert list_value == detail_value, f"Value mismatch for {field_name}"

    @patch('app.services.swarm_api.get_all_stamps_processed')
    def test_required_fields_always_present(self, mock_get_stamps):
        """Test that required fields are always present in responses."""
        minimal_stamp_data = {
            "batchID": "minimal_test",
            "amount": "1000000000",
            "immutableFlag": False,
            "depth": 18,
            "bucketDepth": 16,
            "batchTTL": 3600,
            "expectedExpiration": "2024-12-01-15-30",
            "local": False
            # All optional fields omitted
        }

        mock_get_stamps.return_value = [minimal_stamp_data]

        response = client.get("/api/v1/stamps/")
        assert response.status_code == 200

        stamp = response.json()["stamps"][0]

        required_fields = [
            "batchID", "amount", "immutableFlag", "depth",
            "bucketDepth", "batchTTL", "expectedExpiration", "local"
        ]

        for field in required_fields:
            assert field in stamp, f"Required field {field} missing"
            assert stamp[field] is not None, f"Required field {field} is None"


class TestExpirationCalculation:
    """Tests for expiration time calculation accuracy."""

    @patch('app.services.swarm_api.get_local_stamps')
    @patch('app.services.swarm_api.get_all_stamps')
    def test_expiration_calculation_accuracy(self, mock_global, mock_local):
        """Test that expiration calculations are accurate."""
        # Mock current time
        test_time = datetime.datetime(2024, 6, 15, 10, 30, 0, tzinfo=datetime.timezone.utc)

        mock_global.return_value = [
            {
                "batchID": "expiration_test",
                "batchTTL": 7200,  # 2 hours
                "depth": 18,
                "amount": "1000000000"
            }
        ]
        mock_local.return_value = []

        with patch('datetime.datetime') as mock_datetime:
            mock_datetime.now.return_value = test_time
            mock_datetime.timedelta = datetime.timedelta
            mock_datetime.timezone = datetime.timezone

            from app.services.swarm_api import get_all_stamps_processed
            result = get_all_stamps_processed()

            assert len(result) == 1
            # Should be 2 hours later: 2024-06-15-12-30
            assert result[0]["expectedExpiration"] == "2024-06-15-12-30"

    @patch('app.services.swarm_api.get_all_stamps_processed')
    def test_expiration_format_consistency(self, mock_get_stamps):
        """Test that expiration format is consistent."""
        stamps_with_various_ttls = [
            {"batchID": "test1", "batchTTL": 60, "expectedExpiration": "2024-12-01-15-31"},     # 1 minute
            {"batchID": "test2", "batchTTL": 3600, "expectedExpiration": "2024-12-01-16-30"},   # 1 hour
            {"batchID": "test3", "batchTTL": 86400, "expectedExpiration": "2024-12-02-15-30"},  # 1 day
        ]

        for stamp_data in stamps_with_various_ttls:
            full_stamp = {
                **stamp_data,
                "amount": "1000000000",
                "immutableFlag": False,
                "depth": 18,
                "bucketDepth": 16,
                "local": False
            }

            mock_get_stamps.return_value = [full_stamp]

            response = client.get("/api/v1/stamps/")
            assert response.status_code == 200

            stamp = response.json()["stamps"][0]
            expiration = stamp["expectedExpiration"]

            # Verify format: YYYY-MM-DD-HH-MM
            import re
            pattern = r'^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}$'
            assert re.match(pattern, expiration), f"Invalid expiration format: {expiration}"


class TestConcurrentDataIntegrity:
    """Tests for data integrity under concurrent operations."""

    @patch('app.services.swarm_api.get_all_stamps_processed')
    def test_multiple_simultaneous_requests(self, mock_get_stamps):
        """Test data consistency with multiple simultaneous requests."""
        import threading
        import time

        stamp_data = {
            "batchID": "concurrent_test",
            "amount": "1000000000",
            "immutableFlag": False,
            "depth": 18,
            "bucketDepth": 16,
            "batchTTL": 3600,
            "expectedExpiration": "2024-12-01-15-30",
            "local": True
        }

        mock_get_stamps.return_value = [stamp_data]

        results = []
        errors = []

        def make_request():
            try:
                response = client.get("/api/v1/stamps/")
                results.append(response.json())
            except Exception as e:
                errors.append(e)

        # Create multiple threads
        threads = []
        for _ in range(10):
            thread = threading.Thread(target=make_request)
            threads.append(thread)

        # Start all threads
        for thread in threads:
            thread.start()

        # Wait for completion
        for thread in threads:
            thread.join()

        # Verify results
        assert len(errors) == 0, f"Errors occurred: {errors}"
        assert len(results) == 10, "Not all requests completed"

        # All results should be identical
        first_result = results[0]
        for i, result in enumerate(results[1:], 1):
            assert result == first_result, f"Result {i} differs from first result"

    def test_data_consistency_during_modifications(self):
        """Test that data remains consistent during stamp modifications."""
        # This would be more relevant with a real database
        # For now, test that API responses are stable

        with patch('app.services.swarm_api.get_all_stamps_processed') as mock_get_stamps:
            stamp_data = {
                "batchID": "modification_test",
                "amount": "1000000000",
                "immutableFlag": False,
                "depth": 18,
                "bucketDepth": 16,
                "batchTTL": 3600,
                "expectedExpiration": "2024-12-01-15-30",
                "local": True
            }

            mock_get_stamps.return_value = [stamp_data]

            # Multiple rapid requests should return consistent data
            responses = []
            for _ in range(5):
                response = client.get("/api/v1/stamps/")
                assert response.status_code == 200
                responses.append(response.json())

            # All responses should be identical
            first_response = responses[0]
            for response in responses[1:]:
                assert response == first_response