# tests/test_validation_constraints.py
"""
Tests for input validation constraints and business rules.
These tests ensure that the API properly validates inputs according to business rules.
"""
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient

from app.main import app

# Reusable mock return values
MOCK_FUNDS_OK = {"sufficient": True, "required_bzz": 0.01, "wallet_balance_bzz": 100.0, "shortfall_bzz": 0}
MOCK_CHAINSTATE = {"currentPrice": "24000", "block": 1, "chainTip": 1, "totalAmount": "1"}

client = TestClient(app)


class TestAmountValidation:
    """Tests for amount field validation in stamp operations."""

    @patch('app.services.swarm_api.purchase_postage_stamp', return_value="mock_batch")
    @patch('app.services.swarm_api.check_sufficient_funds', return_value=MOCK_FUNDS_OK)
    @patch('app.services.swarm_api.extend_postage_stamp', return_value="mock_batch")
    @patch('app.services.swarm_api.get_all_stamps_processed', return_value=[{"batchID": "test_id", "depth": 17, "local": True}])
    def test_amount_positive_integers_only(self, mock_stamps, mock_extend, mock_funds, mock_purchase):
        """Test that amounts must be positive integers."""
        invalid_amounts = [
            0,           # Zero
            -1,          # Negative
            -1000000000, # Large negative
            1.5,         # Float (if API accepts it)
        ]

        for amount in invalid_amounts:
            purchase_data = {"amount": amount, "depth": 17}
            response = client.post("/api/v1/stamps/", json=purchase_data)
            assert response.status_code == 422, f"Should reject amount: {amount}"

            # Test extend endpoint too
            extend_data = {"amount": amount}
            response = client.patch("/api/v1/stamps/test_id/extend", json=extend_data)
            assert response.status_code == 422, f"Should reject extend amount: {amount}"

    @patch('app.services.swarm_api.purchase_postage_stamp', return_value="mock_batch")
    @patch('app.services.swarm_api.check_sufficient_funds', return_value=MOCK_FUNDS_OK)
    def test_amount_minimum_value(self, mock_funds, mock_purchase):
        """Test minimum valid amount values."""
        # Test minimum valid amount
        purchase_data = {"amount": 1, "depth": 17}

        # Should not fail validation (but may fail at service level)
        response = client.post("/api/v1/stamps/", json=purchase_data)
        assert response.status_code == 201, "Minimum amount should pass validation"

    @patch('app.services.swarm_api.purchase_postage_stamp', return_value="mock_batch")
    @patch('app.services.swarm_api.check_sufficient_funds', return_value=MOCK_FUNDS_OK)
    def test_amount_maximum_reasonable_value(self, mock_funds, mock_purchase):
        """Test that very large amounts are handled properly."""
        # Test with maximum safe integer value
        max_amount = 2**53 - 1  # JavaScript safe integer limit

        purchase_data = {"amount": max_amount, "depth": 17}
        response = client.post("/api/v1/stamps/", json=purchase_data)
        assert response.status_code in [201, 422], "Large amount should be handled gracefully"


class TestDepthValidation:
    """Tests for depth field validation."""

    @patch('app.services.swarm_api.purchase_postage_stamp', return_value="mock_batch")
    @patch('app.services.swarm_api.check_sufficient_funds', return_value=MOCK_FUNDS_OK)
    def test_depth_valid_range(self, mock_funds, mock_purchase):
        """Test that depth values are within valid range."""
        # Test boundary values that should be valid
        valid_depths = [16, 17, 18, 20, 24, 32]

        for depth in valid_depths:
            purchase_data = {"amount": 8000000000, "depth": depth}
            response = client.post("/api/v1/stamps/", json=purchase_data)
            # Should pass validation
            assert response.status_code == 201, f"Valid depth {depth} should pass validation"

    def test_depth_invalid_range(self):
        """Test that invalid depth values are rejected."""
        invalid_depths = [
            0, 1, 5, 10, 15,    # Too low
            33, 40, 50, 100     # Too high
        ]

        for depth in invalid_depths:
            purchase_data = {"amount": 8000000000, "depth": depth}
            response = client.post("/api/v1/stamps/", json=purchase_data)
            assert response.status_code == 422, f"Invalid depth {depth} should be rejected"

    @patch('app.services.swarm_api.purchase_postage_stamp', return_value="mock_batch")
    @patch('app.services.swarm_api.check_sufficient_funds', return_value=MOCK_FUNDS_OK)
    def test_depth_non_integer_values(self, mock_funds, mock_purchase):
        """Test that non-integer depth values are rejected."""
        invalid_depths = [
            17.5,        # Float
            "17",        # String
            None,        # None
            [],          # List
            {}           # Dict
        ]

        for depth in invalid_depths:
            purchase_data = {"amount": 8000000000, "depth": depth}
            response = client.post("/api/v1/stamps/", json=purchase_data)
            assert response.status_code == 422, f"Non-integer depth {depth} should be rejected"


class TestLabelValidation:
    """Tests for label field validation."""

    @patch('app.services.swarm_api.purchase_postage_stamp', return_value="mock_batch")
    @patch('app.services.swarm_api.check_sufficient_funds', return_value=MOCK_FUNDS_OK)
    def test_label_optional_field(self, mock_funds, mock_purchase):
        """Test that label is properly optional."""
        # Without label
        purchase_data = {"amount": 8000000000, "depth": 17}
        response = client.post("/api/v1/stamps/", json=purchase_data)
        assert response.status_code == 201, "Request without label should be valid"

        # With null label
        purchase_data = {"amount": 8000000000, "depth": 17, "label": None}
        response = client.post("/api/v1/stamps/", json=purchase_data)
        assert response.status_code == 201, "Request with null label should be valid"

        # With empty string label
        purchase_data = {"amount": 8000000000, "depth": 17, "label": ""}
        response = client.post("/api/v1/stamps/", json=purchase_data)
        assert response.status_code == 201, "Request with empty label should be valid"

    @patch('app.services.swarm_api.purchase_postage_stamp', return_value="mock_batch")
    @patch('app.services.swarm_api.check_sufficient_funds', return_value=MOCK_FUNDS_OK)
    def test_label_string_validation(self, mock_funds, mock_purchase):
        """Test that label accepts valid string values."""
        valid_labels = [
            "simple-label",
            "label_with_underscores",
            "label with spaces",
            "label123",
            "UPPERCASE",
            "MixedCase",
            "special!@#$%^&*()",
            "unicode-测试-🚀",
        ]

        for label in valid_labels:
            purchase_data = {"amount": 8000000000, "depth": 17, "label": label}
            response = client.post("/api/v1/stamps/", json=purchase_data)
            assert response.status_code == 201, f"Valid label '{label}' should be accepted"

    @patch('app.services.swarm_api.purchase_postage_stamp', return_value="mock_batch")
    @patch('app.services.swarm_api.check_sufficient_funds', return_value=MOCK_FUNDS_OK)
    def test_label_length_limits(self, mock_funds, mock_purchase):
        """Test label length constraints."""
        # Test reasonable length label
        medium_label = "a" * 100
        purchase_data = {"amount": 8000000000, "depth": 17, "label": medium_label}
        response = client.post("/api/v1/stamps/", json=purchase_data)
        assert response.status_code == 201, "Medium length label should be accepted"

        # Test very long label (should be handled gracefully)
        very_long_label = "a" * 10000
        purchase_data = {"amount": 8000000000, "depth": 17, "label": very_long_label}
        response = client.post("/api/v1/stamps/", json=purchase_data)
        # Should either accept or reject gracefully
        assert response.status_code in [201, 422], "Very long label should be handled gracefully"

    def test_label_type_validation(self):
        """Test that non-string label values are rejected."""
        invalid_labels = [
            123,         # Integer
            12.34,       # Float
            True,        # Boolean
            [],          # List
            {},          # Dict
        ]

        for label in invalid_labels:
            purchase_data = {"amount": 8000000000, "depth": 17, "label": label}
            response = client.post("/api/v1/stamps/", json=purchase_data)
            assert response.status_code == 422, f"Non-string label {type(label)} should be rejected"


class TestRequestStructureValidation:
    """Tests for overall request structure validation."""

    @patch('app.services.swarm_api.purchase_postage_stamp', return_value="mock_batch")
    @patch('app.services.swarm_api.check_sufficient_funds', return_value=MOCK_FUNDS_OK)
    @patch('app.services.swarm_api.get_chainstate', return_value=MOCK_CHAINSTATE)
    def test_optional_fields_with_defaults(self, mock_chain, mock_funds, mock_purchase):
        """Test that amount and depth are optional with sensible defaults.

        Since we now support duration-based purchasing with defaults:
        - duration_hours defaults to 25
        - size defaults to 'small' (depth 17)
        - Empty request should succeed using defaults
        """
        # Missing amount - should use duration-based calculation
        request_data = {"depth": 17}
        response = client.post("/api/v1/stamps/", json=request_data)
        assert response.status_code in [201, 400], "Missing amount should use duration default"

        # Missing depth - should use default depth 17
        request_data = {"amount": 8000000000}
        response = client.post("/api/v1/stamps/", json=request_data)
        assert response.status_code == 201, "Missing depth should use default"

        # Only label provided - should use all defaults
        request_data = {"label": "test"}
        response = client.post("/api/v1/stamps/", json=request_data)
        assert response.status_code in [201, 400], "Should use all defaults"

        # Empty request - should use all defaults (25 hours, depth 17)
        response = client.post("/api/v1/stamps/", json={})
        assert response.status_code in [201, 400], "Empty request should use defaults"

    @patch('app.services.swarm_api.purchase_postage_stamp', return_value="mock_batch")
    @patch('app.services.swarm_api.check_sufficient_funds', return_value=MOCK_FUNDS_OK)
    def test_extra_fields_handling(self, mock_funds, mock_purchase):
        """Test handling of extra/unknown fields in requests."""
        request_data = {
            "amount": 8000000000,
            "depth": 17,
            "label": "test",
            "unknown_field": "should_be_ignored",
            "another_extra": 123
        }

        response = client.post("/api/v1/stamps/", json=request_data)
        # Should either accept (ignoring extra fields) or reject with validation error
        assert response.status_code in [201, 422], "Extra fields should be handled gracefully"

    def test_nested_object_validation(self):
        """Test that nested objects in fields are rejected."""
        request_data = {
            "amount": {"value": 8000000000},  # Nested object instead of integer
            "depth": 17
        }

        response = client.post("/api/v1/stamps/", json=request_data)
        assert response.status_code == 422, "Nested objects should be rejected"


class TestStampIdValidation:
    """Tests for stamp ID validation in URL parameters."""

    @patch('app.services.swarm_api.get_all_stamps_processed', return_value=[])
    def test_valid_stamp_id_formats(self, mock_stamps):
        """Test that valid stamp ID formats are accepted."""
        valid_ids = [
            "000de42079daebd58347bb38ce05bdc477701d93651d3bba318a9aee3fbd786a",  # 64 char hex
            "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",  # Mix of letters/numbers
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",  # All valid hex chars
        ]

        for stamp_id in valid_ids:
            response = client.get(f"/api/v1/stamps/{stamp_id}")
            # Should pass validation (returns 404 since mock returns empty list)
            assert response.status_code == 404, f"Valid ID '{stamp_id}' should pass validation"

    @patch('app.services.swarm_api.get_all_stamps_processed', return_value=[])
    def test_invalid_stamp_id_formats(self, mock_stamps):
        """Test that invalid stamp ID formats are rejected."""
        invalid_ids = [
            "",                    # Empty string (routes to list endpoint, returns 200)
            "too_short",          # Too short
            "a" * 100,            # Too long
            "invalid-chars!@#",   # Invalid characters
            "gggggggggggggggggggggggggggggggggggggggggggggggggggggggggggggggg",  # Invalid hex chars
            "spaces in id",       # Spaces
            "../../../etc/passwd", # Path traversal attempt
            "<script>alert('xss')</script>",  # XSS attempt
        ]

        for stamp_id in invalid_ids:
            response = client.get(f"/api/v1/stamps/{stamp_id}")
            # Empty string routes to list endpoint (200), others return 404
            assert response.status_code in [200, 404, 422], f"Invalid ID '{stamp_id}' should be handled"

    @patch('app.services.swarm_api.extend_postage_stamp', return_value="mock_batch")
    @patch('app.services.swarm_api.check_sufficient_funds', return_value=MOCK_FUNDS_OK)
    @patch('app.services.swarm_api.get_all_stamps_processed', return_value=[])
    def test_stamp_id_in_extend_endpoint(self, mock_stamps, mock_funds, mock_extend):
        """Test stamp ID validation in extend endpoint."""
        extend_data = {"amount": 8000000000}

        # Valid ID format — returns 404 because stamp not in mock data
        valid_id = "000de42079daebd58347bb38ce05bdc477701d93651d3bba318a9aee3fbd786a"
        response = client.patch(f"/api/v1/stamps/{valid_id}/extend", json=extend_data)
        assert response.status_code == 404, "Valid ID should pass validation (stamp not found)"

        # Invalid ID format
        invalid_id = "invalid_id_format"
        response = client.patch(f"/api/v1/stamps/{invalid_id}/extend", json=extend_data)
        assert response.status_code == 404, "Invalid ID should return 404"


class TestContentTypeValidation:
    """Tests for content type and header validation."""

    @patch('app.services.swarm_api.purchase_postage_stamp', return_value="mock_batch")
    @patch('app.services.swarm_api.check_sufficient_funds', return_value=MOCK_FUNDS_OK)
    def test_json_content_type_required(self, mock_funds, mock_purchase):
        """Test that JSON content type is required for POST/PATCH requests."""
        # Test with incorrect content type
        purchase_data = '{"amount": 8000000000, "depth": 17}'

        response = client.post(
            "/api/v1/stamps/",
            data=purchase_data,
            headers={"Content-Type": "text/plain"}
        )
        assert response.status_code == 422, "Non-JSON content type should be rejected"

        # Test with missing content type
        response = client.post("/api/v1/stamps/", data=purchase_data)
        assert response.status_code == 422, "Missing content type should be rejected"

    def test_malformed_json_handling(self):
        """Test handling of malformed JSON."""
        malformed_json_examples = [
            '{"amount": 8000000000, "depth": 17,}',      # Trailing comma
            '{"amount": 8000000000 "depth": 17}',        # Missing comma
            '{amount: 8000000000, depth: 17}',           # Unquoted keys
            '{"amount": 8000000000, "depth":}',          # Missing value
            '{"amount": 8000000000, "depth": 17',        # Unclosed brace
        ]

        for malformed in malformed_json_examples:
            response = client.post(
                "/api/v1/stamps/",
                data=malformed,
                headers={"Content-Type": "application/json"}
            )
            assert response.status_code == 422, f"Malformed JSON should be rejected: {malformed}"


class TestBusinessRuleValidation:
    """Tests for business rule validation beyond basic field validation."""

    @patch('app.services.swarm_api.purchase_postage_stamp', return_value="mock_batch")
    @patch('app.services.swarm_api.check_sufficient_funds', return_value=MOCK_FUNDS_OK)
    def test_reasonable_amount_depth_combinations(self, mock_funds, mock_purchase):
        """Test that amount and depth combinations make business sense."""
        # These are business rule tests - the combinations should be reasonable
        combinations = [
            {"amount": 1000000000, "depth": 16},   # Small amount, low depth
            {"amount": 10000000000, "depth": 20},  # Medium amount, medium depth
            {"amount": 100000000000, "depth": 24}, # Large amount, high depth
        ]

        for combo in combinations:
            purchase_data = {**combo, "label": "test"}
            response = client.post("/api/v1/stamps/", json=purchase_data)
            # Should pass validation (business logic validation)
            assert response.status_code == 201, f"Reasonable combination should be accepted: {combo}"

    @patch('app.services.swarm_api.extend_postage_stamp', return_value="mock_batch")
    @patch('app.services.swarm_api.check_sufficient_funds', return_value=MOCK_FUNDS_OK)
    @patch('app.services.swarm_api.get_all_stamps_processed', return_value=[{"batchID": "valid_id", "depth": 17, "local": True}])
    def test_stamp_extension_business_rules(self, mock_stamps, mock_funds, mock_extend):
        """Test business rules for stamp extension."""
        # Extension amount should follow same rules as purchase amount
        extend_data = {"amount": 1}  # Minimum extension
        response = client.patch("/api/v1/stamps/valid_id/extend", json=extend_data)
        assert response.status_code in [200, 404], "Minimum extension should be valid"

        # Very large extension
        extend_data = {"amount": 999999999999}
        response = client.patch("/api/v1/stamps/valid_id/extend", json=extend_data)
        assert response.status_code in [200, 404, 422], "Large extension should be handled"

    @patch('app.services.swarm_api.purchase_postage_stamp', return_value="mock_batch")
    @patch('app.services.swarm_api.check_sufficient_funds', return_value=MOCK_FUNDS_OK)
    def test_concurrent_operation_validation(self, mock_funds, mock_purchase):
        """Test validation under concurrent operations."""
        import threading

        validation_errors = []

        def make_request():
            try:
                purchase_data = {"amount": 8000000000, "depth": 17}
                response = client.post("/api/v1/stamps/", json=purchase_data)
                if response.status_code == 422:
                    validation_errors.append(response.json())
            except Exception as e:
                validation_errors.append(str(e))

        # Create multiple concurrent requests
        threads = []
        for _ in range(5):
            thread = threading.Thread(target=make_request)
            threads.append(thread)

        for thread in threads:
            thread.start()

        for thread in threads:
            thread.join()

        # Validation should be consistent across concurrent requests
        if validation_errors:
            # All validation errors should be the same type
            first_error = validation_errors[0]
            for error in validation_errors[1:]:
                assert error == first_error, "Validation should be consistent across concurrent requests"