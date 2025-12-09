# tests/test_stamps_edge_cases.py
"""
Comprehensive edge case and boundary testing for stamps API endpoints.
Tests designed to prevent regressions and ensure robustness.
"""
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
import json

from app.main import app

client = TestClient(app)


class TestStampPurchaseEdgeCases:
    """Edge cases and boundary tests for POST /api/v1/stamps/"""

    def test_purchase_stamp_minimum_valid_amount(self):
        """Test purchasing stamp with minimum valid amount."""
        purchase_data = {
            "amount": 1,  # Minimum possible amount
            "depth": 16   # Minimum reasonable depth
        }

        with patch('app.services.swarm_api.purchase_postage_stamp') as mock_purchase:
            mock_purchase.return_value = "test_batch_id"
            response = client.post("/api/v1/stamps/", json=purchase_data)

            assert response.status_code == 201
            mock_purchase.assert_called_once_with(amount=1, depth=16, label=None)

    def test_purchase_stamp_maximum_reasonable_amount(self):
        """Test purchasing stamp with very large amount."""
        purchase_data = {
            "amount": 999999999999999999,  # Very large amount
            "depth": 20
        }

        with patch('app.services.swarm_api.purchase_postage_stamp') as mock_purchase:
            mock_purchase.return_value = "test_batch_id"
            response = client.post("/api/v1/stamps/", json=purchase_data)

            assert response.status_code == 201

    def test_purchase_stamp_zero_amount(self):
        """Test purchasing stamp with zero amount should fail validation."""
        purchase_data = {
            "amount": 0,
            "depth": 17
        }

        response = client.post("/api/v1/stamps/", json=purchase_data)
        assert response.status_code == 422  # Validation error

    def test_purchase_stamp_negative_amount(self):
        """Test purchasing stamp with negative amount should fail."""
        purchase_data = {
            "amount": -1000000000,
            "depth": 17
        }

        response = client.post("/api/v1/stamps/", json=purchase_data)
        assert response.status_code == 422

    def test_purchase_stamp_minimum_depth(self):
        """Test purchasing stamp with minimum valid depth."""
        purchase_data = {
            "amount": 8000000000,
            "depth": 16  # Minimum reasonable depth
        }

        with patch('app.services.swarm_api.purchase_postage_stamp') as mock_purchase:
            mock_purchase.return_value = "test_batch_id"
            response = client.post("/api/v1/stamps/", json=purchase_data)

            assert response.status_code == 201

    def test_purchase_stamp_maximum_depth(self):
        """Test purchasing stamp with maximum valid depth."""
        purchase_data = {
            "amount": 8000000000,
            "depth": 32  # Maximum reasonable depth
        }

        with patch('app.services.swarm_api.purchase_postage_stamp') as mock_purchase:
            mock_purchase.return_value = "test_batch_id"
            response = client.post("/api/v1/stamps/", json=purchase_data)

            assert response.status_code == 201

    def test_purchase_stamp_invalid_low_depth(self):
        """Test purchasing stamp with too low depth should fail."""
        purchase_data = {
            "amount": 8000000000,
            "depth": 5  # Too low
        }

        response = client.post("/api/v1/stamps/", json=purchase_data)
        assert response.status_code == 422

    def test_purchase_stamp_invalid_high_depth(self):
        """Test purchasing stamp with too high depth should fail."""
        purchase_data = {
            "amount": 8000000000,
            "depth": 50  # Too high
        }

        response = client.post("/api/v1/stamps/", json=purchase_data)
        assert response.status_code == 422

    def test_purchase_stamp_very_long_label(self):
        """Test purchasing stamp with extremely long label."""
        purchase_data = {
            "amount": 8000000000,
            "depth": 17,
            "label": "a" * 1000  # Very long label
        }

        with patch('app.services.swarm_api.purchase_postage_stamp') as mock_purchase:
            mock_purchase.return_value = "test_batch_id"
            response = client.post("/api/v1/stamps/", json=purchase_data)

            # Should either succeed or fail gracefully with validation error
            assert response.status_code in [201, 422]

    def test_purchase_stamp_special_characters_label(self):
        """Test purchasing stamp with special characters in label."""
        special_labels = [
            "test-stamp_123",
            "test.stamp@domain",
            "测试标签",  # Chinese characters
            "🚀📝💰",    # Emojis
            "label with spaces",
            "",          # Empty string
        ]

        for label in special_labels:
            purchase_data = {
                "amount": 8000000000,
                "depth": 17,
                "label": label
            }

            with patch('app.services.swarm_api.purchase_postage_stamp') as mock_purchase:
                mock_purchase.return_value = "test_batch_id"
                response = client.post("/api/v1/stamps/", json=purchase_data)

                # Should handle gracefully
                assert response.status_code in [201, 422], f"Failed for label: {label}"

    @patch('app.services.swarm_api.purchase_postage_stamp')
    def test_purchase_stamp_malformed_swarm_response(self, mock_purchase):
        """Test handling of malformed response from Swarm API."""
        mock_purchase.side_effect = ValueError("Invalid response format")

        purchase_data = {
            "amount": 8000000000,
            "depth": 17
        }

        response = client.post("/api/v1/stamps/", json=purchase_data)
        assert response.status_code == 502

    @patch('app.services.swarm_api.purchase_postage_stamp')
    def test_purchase_stamp_empty_batch_id_response(self, mock_purchase):
        """Test handling when Swarm API returns empty batch ID."""
        mock_purchase.return_value = ""  # Empty batch ID

        purchase_data = {
            "amount": 8000000000,
            "depth": 17
        }

        response = client.post("/api/v1/stamps/", json=purchase_data)
        # Should handle gracefully, either accept or reject
        assert response.status_code in [201, 502]


class TestStampDetailsEdgeCases:
    """Edge cases and boundary tests for GET /api/v1/stamps/{stamp_id}"""

    @patch('app.services.swarm_api.get_all_stamps_processed')
    def test_get_stamp_malformed_id(self, mock_get_stamps):
        """Test retrieving stamp with malformed ID."""
        mock_get_stamps.return_value = []

        malformed_ids = [
            "",                    # Empty string
            "too_short",          # Too short
            "a" * 100,            # Too long
            "invalid-chars!@#",   # Special characters
            "123",                # Numbers only
            "zzz" * 20,           # Invalid hex characters
        ]

        for stamp_id in malformed_ids:
            response = client.get(f"/api/v1/stamps/{stamp_id}")
            assert response.status_code == 404, f"Failed for ID: {stamp_id}"

    @patch('app.services.swarm_api.get_all_stamps_processed')
    def test_get_stamp_partial_data_scenarios(self, mock_get_stamps):
        """Test stamp retrieval with various missing field scenarios."""
        test_scenarios = [
            {
                "name": "missing_owner",
                "data": {
                    "batchID": "test123",
                    "amount": "1000000000",
                    "immutableFlag": True,
                    "depth": 18,
                    "bucketDepth": 16,
                    "batchTTL": 3600,
                    "expectedExpiration": "2024-12-01-15-30",
                    "local": False
                    # owner is missing
                }
            },
            {
                "name": "missing_utilization",
                "data": {
                    "batchID": "test456",
                    "amount": "8000000000",
                    "owner": "0x1234567890abcdef",
                    "immutableFlag": False,
                    "depth": 20,
                    "bucketDepth": 16,
                    "batchTTL": 7200,
                    "expectedExpiration": "2024-12-01-17-30",
                    "local": True
                    # utilization is missing
                }
            },
            {
                "name": "all_optional_fields_missing",
                "data": {
                    "batchID": "test789",
                    "amount": "500000000",
                    "immutableFlag": True,
                    "depth": 17,
                    "bucketDepth": 16,
                    "batchTTL": 1800,
                    "expectedExpiration": "2024-12-01-12-30",
                    "local": False
                    # All optional fields missing
                }
            }
        ]

        for scenario in test_scenarios:
            mock_get_stamps.return_value = [scenario["data"]]

            response = client.get(f"/api/v1/stamps/{scenario['data']['batchID']}")

            assert response.status_code == 200, f"Failed scenario: {scenario['name']}"
            data = response.json()

            # Verify required fields are present
            assert data["batchID"] == scenario["data"]["batchID"]
            assert "expectedExpiration" in data
            assert "local" in data

    @patch('app.services.swarm_api.get_all_stamps_processed')
    def test_get_stamp_extreme_ttl_values(self, mock_get_stamps):
        """Test stamp retrieval with extreme TTL values."""
        extreme_ttl_scenarios = [
            {"batchTTL": 0, "name": "zero_ttl"},
            {"batchTTL": 1, "name": "minimum_ttl"},
            {"batchTTL": 999999999, "name": "very_large_ttl"},
            {"batchTTL": -1, "name": "negative_ttl"},  # Should be handled gracefully
        ]

        for scenario in extreme_ttl_scenarios:
            stamp_data = {
                "batchID": f"test_{scenario['name']}",
                "amount": "1000000000",
                "immutableFlag": False,
                "depth": 18,
                "bucketDepth": 16,
                "batchTTL": scenario["batchTTL"],
                "expectedExpiration": "2024-12-01-15-30",
                "local": False
            }

            mock_get_stamps.return_value = [stamp_data]

            response = client.get(f"/api/v1/stamps/{stamp_data['batchID']}")

            assert response.status_code == 200, f"Failed for {scenario['name']}"
            data = response.json()
            assert "expectedExpiration" in data


class TestStampExtensionEdgeCases:
    """Edge cases and boundary tests for PATCH /api/v1/stamps/{stamp_id}/extend"""

    @patch('app.services.swarm_api.extend_postage_stamp')
    def test_extend_stamp_minimum_amount(self, mock_extend):
        """Test extending stamp with minimum amount."""
        mock_extend.return_value = "test_batch_id"

        extension_data = {"amount": 1}

        response = client.patch("/api/v1/stamps/test_batch_id/extend", json=extension_data)
        assert response.status_code == 200
        mock_extend.assert_called_once_with(stamp_id="test_batch_id", amount=1)

    @patch('app.services.swarm_api.extend_postage_stamp')
    def test_extend_stamp_maximum_amount(self, mock_extend):
        """Test extending stamp with very large amount."""
        mock_extend.return_value = "test_batch_id"

        extension_data = {"amount": 999999999999999999}

        response = client.patch("/api/v1/stamps/test_batch_id/extend", json=extension_data)
        assert response.status_code == 200

    def test_extend_stamp_zero_amount(self):
        """Test extending stamp with zero amount should fail."""
        extension_data = {"amount": 0}

        response = client.patch("/api/v1/stamps/test_batch_id/extend", json=extension_data)
        assert response.status_code == 422

    def test_extend_stamp_negative_amount(self):
        """Test extending stamp with negative amount should fail."""
        extension_data = {"amount": -1000000000}

        response = client.patch("/api/v1/stamps/test_batch_id/extend", json=extension_data)
        assert response.status_code == 422

    @patch('app.services.swarm_api.extend_postage_stamp')
    def test_extend_nonexistent_stamp(self, mock_extend):
        """Test extending non-existent stamp."""
        from requests.exceptions import RequestException
        mock_extend.side_effect = RequestException("Stamp not found")

        extension_data = {"amount": 8000000000}

        response = client.patch("/api/v1/stamps/nonexistent_id/extend", json=extension_data)
        assert response.status_code == 502

    @patch('app.services.swarm_api.extend_postage_stamp')
    def test_extend_stamp_batch_id_mismatch(self, mock_extend):
        """Test when returned batch ID doesn't match request."""
        mock_extend.return_value = "different_batch_id"  # Different from request

        extension_data = {"amount": 8000000000}

        response = client.patch("/api/v1/stamps/original_batch_id/extend", json=extension_data)

        # Should still succeed but return the actual batch ID from API
        assert response.status_code == 200
        data = response.json()
        assert data["batchID"] == "different_batch_id"


class TestIntegrationWorkflows:
    """Integration tests for complete stamp workflows."""

    @patch('app.services.swarm_api.purchase_postage_stamp')
    @patch('app.services.swarm_api.get_all_stamps_processed')
    @patch('app.services.swarm_api.extend_postage_stamp')
    def test_complete_stamp_lifecycle(self, mock_extend, mock_get_stamps, mock_purchase):
        """Test complete stamp lifecycle: purchase → get details → extend → get details."""

        # Setup mocks
        batch_id = "lifecycle_test_batch"
        mock_purchase.return_value = batch_id
        mock_extend.return_value = batch_id

        initial_stamp_data = {
            "batchID": batch_id,
            "amount": "8000000000",
            "immutableFlag": False,
            "depth": 18,
            "bucketDepth": 16,
            "batchTTL": 3600,
            "expectedExpiration": "2024-12-01-15-30",
            "local": True,
            "utilization": None,
            "usable": True,
            "label": "lifecycle-test"
        }

        extended_stamp_data = {**initial_stamp_data, "amount": "4000000000", "batchTTL": 7200}

        # Step 1: Purchase stamp
        purchase_data = {"amount": 8000000000, "depth": 18, "label": "lifecycle-test"}
        purchase_response = client.post("/api/v1/stamps/", json=purchase_data)

        assert purchase_response.status_code == 201
        assert purchase_response.json()["batchID"] == batch_id

        # Step 2: Get stamp details
        mock_get_stamps.return_value = [initial_stamp_data]
        details_response = client.get(f"/api/v1/stamps/{batch_id}")

        assert details_response.status_code == 200
        details = details_response.json()
        assert details["batchID"] == batch_id
        assert details["local"] is True

        # Step 3: Extend stamp
        extend_data = {"amount": 8000000000}
        extend_response = client.patch(f"/api/v1/stamps/{batch_id}/extend", json=extend_data)

        assert extend_response.status_code == 200

        # Step 4: Get updated stamp details
        mock_get_stamps.return_value = [extended_stamp_data]
        updated_details_response = client.get(f"/api/v1/stamps/{batch_id}")

        assert updated_details_response.status_code == 200
        updated_details = updated_details_response.json()
        assert updated_details["batchID"] == batch_id

    @patch('app.services.swarm_api.get_all_stamps_processed')
    def test_data_consistency_across_endpoints(self, mock_get_stamps):
        """Test that stamp data is consistent across list and detail endpoints."""

        stamp_data = {
            "batchID": "consistency_test",
            "amount": "1500000000",
            "owner": "0xabcdef1234567890",
            "immutableFlag": True,
            "depth": 19,
            "bucketDepth": 16,
            "batchTTL": 5400,
            "utilization": 75,
            "usable": True,
            "label": "consistency-test",
            "expectedExpiration": "2024-12-01-16-30",
            "local": True
        }

        mock_get_stamps.return_value = [stamp_data]

        # Get from list endpoint
        list_response = client.get("/api/v1/stamps/")
        assert list_response.status_code == 200

        stamps_list = list_response.json()["stamps"]
        assert len(stamps_list) == 1

        stamp_from_list = stamps_list[0]

        # Get from details endpoint
        details_response = client.get(f"/api/v1/stamps/{stamp_data['batchID']}")
        assert details_response.status_code == 200

        stamp_from_details = details_response.json()

        # Compare key fields for consistency
        key_fields = ["batchID", "amount", "immutableFlag", "depth", "local", "expectedExpiration"]

        for field in key_fields:
            assert stamp_from_list[field] == stamp_from_details[field], f"Mismatch in field: {field}"


class TestSecurityAndValidation:
    """Security and input validation tests."""

    def test_sql_injection_attempts_in_stamp_id(self):
        """Test SQL injection attempts in stamp ID parameter."""
        malicious_ids = [
            "'; DROP TABLE stamps; --",
            "1' OR '1'='1",
            "test'; SELECT * FROM users; --",
            "<script>alert('xss')</script>",
            "../../../etc/passwd",
        ]

        for malicious_id in malicious_ids:
            response = client.get(f"/api/v1/stamps/{malicious_id}")
            # Should return 404 or handle gracefully, never execute malicious code
            assert response.status_code in [404, 422], f"Security issue with ID: {malicious_id}"

    def test_large_payload_handling(self):
        """Test handling of extremely large request payloads."""
        large_purchase_data = {
            "amount": 8000000000,
            "depth": 17,
            "label": "x" * 10000  # Very large label
        }

        response = client.post("/api/v1/stamps/", json=large_purchase_data)
        # Should either accept or reject gracefully, not crash
        assert response.status_code in [201, 422, 413]  # 413 = Payload Too Large

    def test_malformed_json_handling(self):
        """Test handling of malformed JSON in requests."""
        malformed_requests = [
            '{"amount": 8000000000, "depth": 17,}',  # Trailing comma
            '{"amount": 8000000000 "depth": 17}',    # Missing comma
            '{"amount": "not_a_number", "depth": 17}',  # Wrong type
            '{amount: 8000000000, depth: 17}',       # Unquoted keys
        ]

        for malformed in malformed_requests:
            response = client.post(
                "/api/v1/stamps/",
                data=malformed,
                headers={"Content-Type": "application/json"}
            )
            assert response.status_code == 422, f"Should reject malformed JSON: {malformed}"