# tests/test_base_balance.py
"""
Unit tests for Base Sepolia ETH balance monitoring.
"""
import pytest
from unittest.mock import patch, MagicMock

from app.x402.base_balance import (
    wei_to_eth,
    check_base_eth_balance,
    clear_balance_cache,
    WEI_PER_ETH,
    CACHE_TTL_SECONDS,
)


class TestConversionFunctions:
    """Test unit conversion functions."""

    def test_wei_to_eth_zero(self):
        """Zero wei equals zero ETH."""
        assert wei_to_eth(0) == 0.0

    def test_wei_to_eth_one_eth(self):
        """One ETH worth of wei."""
        assert wei_to_eth(WEI_PER_ETH) == 1.0

    def test_wei_to_eth_fractional(self):
        """Fractional ETH values."""
        assert wei_to_eth(WEI_PER_ETH // 2) == 0.5

    def test_wei_to_eth_small_amount(self):
        """Small ETH amounts (typical gas amounts)."""
        # 0.001 ETH = 10^15 wei
        wei = 10 ** 15
        assert wei_to_eth(wei) == 0.001

    def test_wei_to_eth_large_amount(self):
        """Large ETH amounts."""
        # 100 ETH
        wei = 100 * WEI_PER_ETH
        assert wei_to_eth(wei) == 100.0


class TestCheckBaseEthBalance:
    """Test Base ETH balance checks."""

    def setup_method(self):
        """Clear cache before each test."""
        clear_balance_cache()

    @patch("app.x402.base_balance._get_eth_balance_from_rpc")
    @patch("app.x402.base_balance.settings")
    def test_balance_above_threshold(self, mock_settings, mock_get_balance):
        """Balance above warning threshold returns ok=True."""
        mock_settings.X402_PAY_TO_ADDRESS = "0x1234567890123456789012345678901234567890"
        mock_settings.X402_BASE_ETH_WARN_THRESHOLD = 0.005
        mock_settings.X402_BASE_ETH_CRITICAL_THRESHOLD = 0.001
        mock_get_balance.return_value = int(0.01 * WEI_PER_ETH)  # 0.01 ETH

        result = check_base_eth_balance()

        assert result["ok"] is True
        assert result["is_critical"] is False
        assert result["balance_eth"] == 0.01
        assert result["threshold_eth"] == 0.005
        assert result["warning"] is None

    @patch("app.x402.base_balance._get_eth_balance_from_rpc")
    @patch("app.x402.base_balance.settings")
    def test_balance_below_warning_threshold(self, mock_settings, mock_get_balance):
        """Balance below warning threshold returns ok=False with warning."""
        mock_settings.X402_PAY_TO_ADDRESS = "0x1234567890123456789012345678901234567890"
        mock_settings.X402_BASE_ETH_WARN_THRESHOLD = 0.005
        mock_settings.X402_BASE_ETH_CRITICAL_THRESHOLD = 0.001
        mock_get_balance.return_value = int(0.003 * WEI_PER_ETH)  # 0.003 ETH

        result = check_base_eth_balance()

        assert result["ok"] is False
        assert result["is_critical"] is False
        assert result["balance_eth"] == 0.003
        assert "below warning threshold" in result["warning"]
        assert "Top up" in result["warning"]

    @patch("app.x402.base_balance._get_eth_balance_from_rpc")
    @patch("app.x402.base_balance.settings")
    def test_balance_below_critical_threshold(self, mock_settings, mock_get_balance):
        """Balance below critical threshold returns is_critical=True."""
        mock_settings.X402_PAY_TO_ADDRESS = "0x1234567890123456789012345678901234567890"
        mock_settings.X402_BASE_ETH_WARN_THRESHOLD = 0.005
        mock_settings.X402_BASE_ETH_CRITICAL_THRESHOLD = 0.001
        mock_get_balance.return_value = int(0.0005 * WEI_PER_ETH)  # 0.0005 ETH

        result = check_base_eth_balance()

        assert result["ok"] is False
        assert result["is_critical"] is True
        assert result["balance_eth"] == 0.0005
        assert "critically low" in result["warning"]
        assert "immediately" in result["warning"]

    @patch("app.x402.base_balance._get_eth_balance_from_rpc")
    @patch("app.x402.base_balance.settings")
    def test_balance_at_warning_threshold(self, mock_settings, mock_get_balance):
        """Balance exactly at warning threshold returns ok=True."""
        mock_settings.X402_PAY_TO_ADDRESS = "0x1234567890123456789012345678901234567890"
        mock_settings.X402_BASE_ETH_WARN_THRESHOLD = 0.005
        mock_settings.X402_BASE_ETH_CRITICAL_THRESHOLD = 0.001
        mock_get_balance.return_value = int(0.005 * WEI_PER_ETH)  # Exactly at threshold

        result = check_base_eth_balance()

        assert result["ok"] is True
        assert result["is_critical"] is False

    @patch("app.x402.base_balance._get_eth_balance_from_rpc")
    @patch("app.x402.base_balance.settings")
    def test_balance_at_critical_threshold(self, mock_settings, mock_get_balance):
        """Balance exactly at critical threshold returns is_critical=False."""
        mock_settings.X402_PAY_TO_ADDRESS = "0x1234567890123456789012345678901234567890"
        mock_settings.X402_BASE_ETH_WARN_THRESHOLD = 0.005
        mock_settings.X402_BASE_ETH_CRITICAL_THRESHOLD = 0.001
        mock_get_balance.return_value = int(0.001 * WEI_PER_ETH)  # Exactly at critical

        result = check_base_eth_balance()

        assert result["ok"] is False  # Below warning
        assert result["is_critical"] is False  # But not below critical

    @patch("app.x402.base_balance._get_eth_balance_from_rpc")
    @patch("app.x402.base_balance.settings")
    def test_rpc_error_returns_critical(self, mock_settings, mock_get_balance):
        """RPC error returns is_critical=True (can't verify balance)."""
        mock_settings.X402_PAY_TO_ADDRESS = "0x1234567890123456789012345678901234567890"
        mock_settings.X402_BASE_ETH_WARN_THRESHOLD = 0.005
        mock_settings.X402_BASE_ETH_CRITICAL_THRESHOLD = 0.001
        mock_get_balance.side_effect = Exception("Connection refused")

        result = check_base_eth_balance()

        assert result["ok"] is False
        assert result["is_critical"] is True
        assert result["balance_eth"] == 0.0
        assert "Failed to fetch" in result["warning"]
        assert "Connection refused" in result["warning"]

    @patch("app.x402.base_balance.settings")
    def test_no_address_configured(self, mock_settings):
        """Missing PAY_TO_ADDRESS returns error state."""
        mock_settings.X402_PAY_TO_ADDRESS = None
        mock_settings.X402_BASE_ETH_WARN_THRESHOLD = 0.005
        mock_settings.X402_BASE_ETH_CRITICAL_THRESHOLD = 0.001

        result = check_base_eth_balance()

        assert result["ok"] is False
        assert result["is_critical"] is True
        assert result["address"] is None
        assert "not configured" in result["warning"]

    @patch("app.x402.base_balance.settings")
    def test_empty_address_configured(self, mock_settings):
        """Empty PAY_TO_ADDRESS returns error state."""
        mock_settings.X402_PAY_TO_ADDRESS = ""
        mock_settings.X402_BASE_ETH_WARN_THRESHOLD = 0.005
        mock_settings.X402_BASE_ETH_CRITICAL_THRESHOLD = 0.001

        result = check_base_eth_balance()

        assert result["ok"] is False
        assert result["is_critical"] is True


class TestCaching:
    """Test balance caching behavior."""

    def setup_method(self):
        """Clear cache before each test."""
        clear_balance_cache()

    @patch("app.x402.base_balance._get_eth_balance_from_rpc")
    @patch("app.x402.base_balance.settings")
    def test_cache_is_used(self, mock_settings, mock_get_balance):
        """Second call uses cached value."""
        mock_settings.X402_PAY_TO_ADDRESS = "0x1234567890123456789012345678901234567890"
        mock_settings.X402_BASE_ETH_WARN_THRESHOLD = 0.005
        mock_settings.X402_BASE_ETH_CRITICAL_THRESHOLD = 0.001
        mock_get_balance.return_value = int(0.01 * WEI_PER_ETH)

        # First call - should fetch from RPC
        result1 = check_base_eth_balance()
        # Second call - should use cache
        result2 = check_base_eth_balance()

        # RPC should only be called once
        assert mock_get_balance.call_count == 1
        assert result1["balance_eth"] == result2["balance_eth"]

    @patch("app.x402.base_balance.time")
    @patch("app.x402.base_balance._get_eth_balance_from_rpc")
    @patch("app.x402.base_balance.settings")
    def test_cache_expires(self, mock_settings, mock_get_balance, mock_time):
        """Cache expires after TTL."""
        mock_settings.X402_PAY_TO_ADDRESS = "0x1234567890123456789012345678901234567890"
        mock_settings.X402_BASE_ETH_WARN_THRESHOLD = 0.005
        mock_settings.X402_BASE_ETH_CRITICAL_THRESHOLD = 0.001
        mock_get_balance.return_value = int(0.01 * WEI_PER_ETH)

        # First call at time 0
        mock_time.time.return_value = 0
        check_base_eth_balance()

        # Second call at time 30 (within TTL)
        mock_time.time.return_value = 30
        check_base_eth_balance()

        # Should still be just 1 RPC call
        assert mock_get_balance.call_count == 1

        # Third call at time 61 (after TTL)
        mock_time.time.return_value = CACHE_TTL_SECONDS + 1
        check_base_eth_balance()

        # Should now be 2 RPC calls
        assert mock_get_balance.call_count == 2

    @patch("app.x402.base_balance._get_eth_balance_from_rpc")
    @patch("app.x402.base_balance.settings")
    def test_clear_cache(self, mock_settings, mock_get_balance):
        """clear_balance_cache() forces new RPC call."""
        mock_settings.X402_PAY_TO_ADDRESS = "0x1234567890123456789012345678901234567890"
        mock_settings.X402_BASE_ETH_WARN_THRESHOLD = 0.005
        mock_settings.X402_BASE_ETH_CRITICAL_THRESHOLD = 0.001
        mock_get_balance.return_value = int(0.01 * WEI_PER_ETH)

        # First call
        check_base_eth_balance()
        assert mock_get_balance.call_count == 1

        # Clear cache
        clear_balance_cache()

        # Second call - should fetch again
        check_base_eth_balance()
        assert mock_get_balance.call_count == 2


class TestResponseStructure:
    """Test the structure of balance check responses."""

    def setup_method(self):
        """Clear cache before each test."""
        clear_balance_cache()

    @patch("app.x402.base_balance._get_eth_balance_from_rpc")
    @patch("app.x402.base_balance.settings")
    def test_response_contains_all_fields(self, mock_settings, mock_get_balance):
        """Response contains all expected fields."""
        mock_settings.X402_PAY_TO_ADDRESS = "0x1234567890123456789012345678901234567890"
        mock_settings.X402_BASE_ETH_WARN_THRESHOLD = 0.005
        mock_settings.X402_BASE_ETH_CRITICAL_THRESHOLD = 0.001
        mock_get_balance.return_value = int(0.01 * WEI_PER_ETH)

        result = check_base_eth_balance()

        # Check all required fields
        assert "ok" in result
        assert "is_critical" in result
        assert "balance_wei" in result
        assert "balance_eth" in result
        assert "threshold_eth" in result
        assert "critical_eth" in result
        assert "address" in result
        assert "warning" in result

    @patch("app.x402.base_balance._get_eth_balance_from_rpc")
    @patch("app.x402.base_balance.settings")
    def test_response_includes_address(self, mock_settings, mock_get_balance):
        """Response includes the monitored address."""
        address = "0xABCDEF1234567890ABCDEF1234567890ABCDEF12"
        mock_settings.X402_PAY_TO_ADDRESS = address
        mock_settings.X402_BASE_ETH_WARN_THRESHOLD = 0.005
        mock_settings.X402_BASE_ETH_CRITICAL_THRESHOLD = 0.001
        mock_get_balance.return_value = int(0.01 * WEI_PER_ETH)

        result = check_base_eth_balance()

        assert result["address"] == address

    @patch("app.x402.base_balance._get_eth_balance_from_rpc")
    @patch("app.x402.base_balance.settings")
    def test_balance_wei_is_integer(self, mock_settings, mock_get_balance):
        """balance_wei is returned as integer."""
        mock_settings.X402_PAY_TO_ADDRESS = "0x1234567890123456789012345678901234567890"
        mock_settings.X402_BASE_ETH_WARN_THRESHOLD = 0.005
        mock_settings.X402_BASE_ETH_CRITICAL_THRESHOLD = 0.001
        expected_wei = int(0.01 * WEI_PER_ETH)
        mock_get_balance.return_value = expected_wei

        result = check_base_eth_balance()

        assert isinstance(result["balance_wei"], int)
        assert result["balance_wei"] == expected_wei
