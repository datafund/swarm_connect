# tests/test_x402_validation.py
"""
Unit tests for x402 startup validation.
"""
import pytest
from unittest.mock import patch, MagicMock

from app.x402.validation import (
    validate_eth_address,
    validate_x402_config,
    check_x402_startup,
    X402ConfigurationError,
    ETH_ADDRESS_PATTERN,
    VALID_NETWORKS,
)


class TestValidateEthAddress:
    """Test Ethereum address validation."""

    def test_valid_address_lowercase(self):
        """Valid lowercase address passes."""
        is_valid, error = validate_eth_address(
            "0x1234567890abcdef1234567890abcdef12345678",
            "TEST_ADDRESS"
        )
        assert is_valid is True
        assert error == ""

    def test_valid_address_uppercase(self):
        """Valid uppercase address passes."""
        is_valid, error = validate_eth_address(
            "0xABCDEF1234567890ABCDEF1234567890ABCDEF12",
            "TEST_ADDRESS"
        )
        assert is_valid is True
        assert error == ""

    def test_valid_address_mixed_case(self):
        """Valid mixed-case address passes (checksum format)."""
        is_valid, error = validate_eth_address(
            "0xAbCdEf1234567890abcdef1234567890AbCdEf12",
            "TEST_ADDRESS"
        )
        assert is_valid is True
        assert error == ""

    def test_empty_address_fails(self):
        """Empty address fails validation."""
        is_valid, error = validate_eth_address("", "TEST_ADDRESS")
        assert is_valid is False
        assert "required but not set" in error

    def test_none_address_fails(self):
        """None address fails validation."""
        is_valid, error = validate_eth_address(None, "TEST_ADDRESS")
        assert is_valid is False
        assert "required but not set" in error

    def test_missing_0x_prefix_fails(self):
        """Address without 0x prefix fails."""
        is_valid, error = validate_eth_address(
            "1234567890abcdef1234567890abcdef12345678",
            "TEST_ADDRESS"
        )
        assert is_valid is False
        assert "not a valid Ethereum address" in error

    def test_too_short_fails(self):
        """Address too short fails."""
        is_valid, error = validate_eth_address(
            "0x12345678",
            "TEST_ADDRESS"
        )
        assert is_valid is False
        assert "not a valid Ethereum address" in error

    def test_too_long_fails(self):
        """Address too long fails."""
        is_valid, error = validate_eth_address(
            "0x1234567890abcdef1234567890abcdef1234567890",
            "TEST_ADDRESS"
        )
        assert is_valid is False
        assert "not a valid Ethereum address" in error

    def test_invalid_characters_fails(self):
        """Address with invalid characters fails."""
        is_valid, error = validate_eth_address(
            "0xGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGG",
            "TEST_ADDRESS"
        )
        assert is_valid is False
        assert "not a valid Ethereum address" in error

    def test_zero_address_fails(self):
        """Zero address (0x0...0) fails validation."""
        is_valid, error = validate_eth_address(
            "0x0000000000000000000000000000000000000000",
            "TEST_ADDRESS"
        )
        assert is_valid is False
        assert "zero address" in error

    def test_field_name_in_error(self):
        """Field name is included in error message."""
        is_valid, error = validate_eth_address("", "MY_CUSTOM_FIELD")
        assert is_valid is False
        assert "MY_CUSTOM_FIELD" in error


class TestValidateX402Config:
    """Test x402 configuration validation."""

    @patch("app.x402.validation.settings")
    def test_valid_config_returns_no_errors(self, mock_settings):
        """Valid configuration returns empty error list."""
        mock_settings.X402_PAY_TO_ADDRESS = "0x1234567890abcdef1234567890abcdef12345678"
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_FACILITATOR_URL = "https://x402.org/facilitator"
        mock_settings.X402_MIN_PRICE_USD = 0.01
        mock_settings.X402_BZZ_USD_RATE = 0.50
        mock_settings.X402_RATE_LIMIT_PER_IP = 10
        mock_settings.X402_FREE_TIER_ENABLED = False
        mock_settings.X402_FREE_TIER_RATE_LIMIT = 5

        errors = validate_x402_config()
        assert errors == []

    @patch("app.x402.validation.settings")
    def test_missing_pay_to_address(self, mock_settings):
        """Missing PAY_TO_ADDRESS is caught."""
        mock_settings.X402_PAY_TO_ADDRESS = None
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_FACILITATOR_URL = "https://x402.org/facilitator"
        mock_settings.X402_MIN_PRICE_USD = 0.01
        mock_settings.X402_BZZ_USD_RATE = 0.50
        mock_settings.X402_RATE_LIMIT_PER_IP = 10
        mock_settings.X402_FREE_TIER_ENABLED = False
        mock_settings.X402_FREE_TIER_RATE_LIMIT = 5

        errors = validate_x402_config()
        assert len(errors) == 1
        assert "X402_PAY_TO_ADDRESS" in errors[0]

    @patch("app.x402.validation.settings")
    def test_invalid_network(self, mock_settings):
        """Invalid network is caught."""
        mock_settings.X402_PAY_TO_ADDRESS = "0x1234567890abcdef1234567890abcdef12345678"
        mock_settings.X402_NETWORK = "invalid-network"
        mock_settings.X402_FACILITATOR_URL = "https://x402.org/facilitator"
        mock_settings.X402_MIN_PRICE_USD = 0.01
        mock_settings.X402_BZZ_USD_RATE = 0.50
        mock_settings.X402_RATE_LIMIT_PER_IP = 10
        mock_settings.X402_FREE_TIER_ENABLED = False
        mock_settings.X402_FREE_TIER_RATE_LIMIT = 5

        errors = validate_x402_config()
        assert len(errors) == 1
        assert "X402_NETWORK" in errors[0]
        assert "invalid-network" in errors[0]

    @patch("app.x402.validation.settings")
    def test_missing_facilitator_url(self, mock_settings):
        """Missing facilitator URL is caught."""
        mock_settings.X402_PAY_TO_ADDRESS = "0x1234567890abcdef1234567890abcdef12345678"
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_FACILITATOR_URL = ""
        mock_settings.X402_MIN_PRICE_USD = 0.01
        mock_settings.X402_BZZ_USD_RATE = 0.50
        mock_settings.X402_RATE_LIMIT_PER_IP = 10
        mock_settings.X402_FREE_TIER_ENABLED = False
        mock_settings.X402_FREE_TIER_RATE_LIMIT = 5

        errors = validate_x402_config()
        assert len(errors) == 1
        assert "X402_FACILITATOR_URL" in errors[0]

    @patch("app.x402.validation.settings")
    def test_invalid_facilitator_url(self, mock_settings):
        """Invalid facilitator URL (not http/https) is caught."""
        mock_settings.X402_PAY_TO_ADDRESS = "0x1234567890abcdef1234567890abcdef12345678"
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_FACILITATOR_URL = "ftp://invalid.com"
        mock_settings.X402_MIN_PRICE_USD = 0.01
        mock_settings.X402_BZZ_USD_RATE = 0.50
        mock_settings.X402_RATE_LIMIT_PER_IP = 10
        mock_settings.X402_FREE_TIER_ENABLED = False
        mock_settings.X402_FREE_TIER_RATE_LIMIT = 5

        errors = validate_x402_config()
        assert len(errors) == 1
        assert "X402_FACILITATOR_URL" in errors[0]
        assert "valid URL" in errors[0]

    @patch("app.x402.validation.settings")
    def test_zero_min_price(self, mock_settings):
        """Zero min price is caught."""
        mock_settings.X402_PAY_TO_ADDRESS = "0x1234567890abcdef1234567890abcdef12345678"
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_FACILITATOR_URL = "https://x402.org/facilitator"
        mock_settings.X402_MIN_PRICE_USD = 0
        mock_settings.X402_BZZ_USD_RATE = 0.50
        mock_settings.X402_RATE_LIMIT_PER_IP = 10
        mock_settings.X402_FREE_TIER_ENABLED = False
        mock_settings.X402_FREE_TIER_RATE_LIMIT = 5

        errors = validate_x402_config()
        assert len(errors) == 1
        assert "X402_MIN_PRICE_USD" in errors[0]

    @patch("app.x402.validation.settings")
    def test_negative_bzz_rate(self, mock_settings):
        """Negative BZZ rate is caught."""
        mock_settings.X402_PAY_TO_ADDRESS = "0x1234567890abcdef1234567890abcdef12345678"
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_FACILITATOR_URL = "https://x402.org/facilitator"
        mock_settings.X402_MIN_PRICE_USD = 0.01
        mock_settings.X402_BZZ_USD_RATE = -1
        mock_settings.X402_RATE_LIMIT_PER_IP = 10
        mock_settings.X402_FREE_TIER_ENABLED = False
        mock_settings.X402_FREE_TIER_RATE_LIMIT = 5

        errors = validate_x402_config()
        assert len(errors) == 1
        assert "X402_BZZ_USD_RATE" in errors[0]

    @patch("app.x402.validation.settings")
    def test_zero_rate_limit(self, mock_settings):
        """Zero rate limit is caught."""
        mock_settings.X402_PAY_TO_ADDRESS = "0x1234567890abcdef1234567890abcdef12345678"
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_FACILITATOR_URL = "https://x402.org/facilitator"
        mock_settings.X402_MIN_PRICE_USD = 0.01
        mock_settings.X402_BZZ_USD_RATE = 0.50
        mock_settings.X402_RATE_LIMIT_PER_IP = 0
        mock_settings.X402_FREE_TIER_ENABLED = False
        mock_settings.X402_FREE_TIER_RATE_LIMIT = 5

        errors = validate_x402_config()
        assert len(errors) == 1
        assert "X402_RATE_LIMIT_PER_IP" in errors[0]

    @patch("app.x402.validation.settings")
    def test_free_tier_enabled_with_zero_limit(self, mock_settings):
        """Free tier enabled with zero rate limit is caught."""
        mock_settings.X402_PAY_TO_ADDRESS = "0x1234567890abcdef1234567890abcdef12345678"
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_FACILITATOR_URL = "https://x402.org/facilitator"
        mock_settings.X402_MIN_PRICE_USD = 0.01
        mock_settings.X402_BZZ_USD_RATE = 0.50
        mock_settings.X402_RATE_LIMIT_PER_IP = 10
        mock_settings.X402_FREE_TIER_ENABLED = True
        mock_settings.X402_FREE_TIER_RATE_LIMIT = 0

        errors = validate_x402_config()
        assert len(errors) == 1
        assert "X402_FREE_TIER_RATE_LIMIT" in errors[0]

    @patch("app.x402.validation.settings")
    def test_free_tier_disabled_zero_limit_ok(self, mock_settings):
        """Free tier disabled with zero rate limit is OK."""
        mock_settings.X402_PAY_TO_ADDRESS = "0x1234567890abcdef1234567890abcdef12345678"
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_FACILITATOR_URL = "https://x402.org/facilitator"
        mock_settings.X402_MIN_PRICE_USD = 0.01
        mock_settings.X402_BZZ_USD_RATE = 0.50
        mock_settings.X402_RATE_LIMIT_PER_IP = 10
        mock_settings.X402_FREE_TIER_ENABLED = False
        mock_settings.X402_FREE_TIER_RATE_LIMIT = 0

        errors = validate_x402_config()
        assert errors == []

    @patch("app.x402.validation.settings")
    def test_multiple_errors_collected(self, mock_settings):
        """Multiple config errors are all collected."""
        mock_settings.X402_PAY_TO_ADDRESS = None  # Error 1
        mock_settings.X402_NETWORK = "invalid"    # Error 2
        mock_settings.X402_FACILITATOR_URL = ""   # Error 3
        mock_settings.X402_MIN_PRICE_USD = 0      # Error 4
        mock_settings.X402_BZZ_USD_RATE = 0       # Error 5
        mock_settings.X402_RATE_LIMIT_PER_IP = 0  # Error 6
        mock_settings.X402_FREE_TIER_ENABLED = True
        mock_settings.X402_FREE_TIER_RATE_LIMIT = 0  # Error 7

        errors = validate_x402_config()
        assert len(errors) == 7


class TestCheckX402Startup:
    """Test startup validation function."""

    @patch("app.x402.validation.settings")
    def test_x402_disabled_returns_immediately(self, mock_settings):
        """When X402_ENABLED=false, validation returns without checking."""
        mock_settings.X402_ENABLED = False
        # Don't set any other config - should not matter

        # Should not raise
        check_x402_startup()

    @patch("app.x402.validation.settings")
    def test_x402_enabled_valid_config_succeeds(self, mock_settings):
        """When X402_ENABLED=true with valid config, succeeds."""
        mock_settings.X402_ENABLED = True
        mock_settings.X402_PAY_TO_ADDRESS = "0x1234567890abcdef1234567890abcdef12345678"
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_FACILITATOR_URL = "https://x402.org/facilitator"
        mock_settings.X402_MIN_PRICE_USD = 0.01
        mock_settings.X402_BZZ_USD_RATE = 0.50
        mock_settings.X402_RATE_LIMIT_PER_IP = 10
        mock_settings.X402_FREE_TIER_ENABLED = False
        mock_settings.X402_FREE_TIER_RATE_LIMIT = 5

        # Should not raise
        check_x402_startup()

    @patch("app.x402.validation.settings")
    def test_x402_enabled_invalid_config_raises(self, mock_settings):
        """When X402_ENABLED=true with invalid config, raises X402ConfigurationError."""
        mock_settings.X402_ENABLED = True
        mock_settings.X402_PAY_TO_ADDRESS = None  # Invalid
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_FACILITATOR_URL = "https://x402.org/facilitator"
        mock_settings.X402_MIN_PRICE_USD = 0.01
        mock_settings.X402_BZZ_USD_RATE = 0.50
        mock_settings.X402_RATE_LIMIT_PER_IP = 10
        mock_settings.X402_FREE_TIER_ENABLED = False
        mock_settings.X402_FREE_TIER_RATE_LIMIT = 5

        with pytest.raises(X402ConfigurationError) as exc_info:
            check_x402_startup()

        assert "X402_PAY_TO_ADDRESS" in str(exc_info.value)

    @patch("app.x402.validation.settings")
    def test_x402_enabled_multiple_errors_in_exception(self, mock_settings):
        """Multiple config errors are all included in exception message."""
        mock_settings.X402_ENABLED = True
        mock_settings.X402_PAY_TO_ADDRESS = None  # Error 1
        mock_settings.X402_NETWORK = "invalid"    # Error 2
        mock_settings.X402_FACILITATOR_URL = "https://x402.org/facilitator"
        mock_settings.X402_MIN_PRICE_USD = 0.01
        mock_settings.X402_BZZ_USD_RATE = 0.50
        mock_settings.X402_RATE_LIMIT_PER_IP = 10
        mock_settings.X402_FREE_TIER_ENABLED = False
        mock_settings.X402_FREE_TIER_RATE_LIMIT = 5

        with pytest.raises(X402ConfigurationError) as exc_info:
            check_x402_startup()

        error_msg = str(exc_info.value)
        assert "X402_PAY_TO_ADDRESS" in error_msg
        assert "X402_NETWORK" in error_msg


class TestValidNetworks:
    """Test network validation constants."""

    def test_base_sepolia_is_valid(self):
        """base-sepolia is a valid network."""
        assert "base-sepolia" in VALID_NETWORKS

    def test_base_is_valid(self):
        """base (mainnet) is a valid network."""
        assert "base" in VALID_NETWORKS


class TestEthAddressPattern:
    """Test the ETH address regex pattern."""

    def test_pattern_matches_valid_address(self):
        """Pattern matches valid 42-char address."""
        assert ETH_ADDRESS_PATTERN.match("0x1234567890abcdef1234567890abcdef12345678")

    def test_pattern_rejects_no_prefix(self):
        """Pattern rejects address without 0x."""
        assert not ETH_ADDRESS_PATTERN.match("1234567890abcdef1234567890abcdef12345678")

    def test_pattern_rejects_wrong_length(self):
        """Pattern rejects wrong length."""
        assert not ETH_ADDRESS_PATTERN.match("0x123456")
        assert not ETH_ADDRESS_PATTERN.match("0x1234567890abcdef1234567890abcdef1234567890")
