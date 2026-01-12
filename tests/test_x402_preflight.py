# tests/test_x402_preflight.py
"""
Unit tests for x402 pre-flight balance checks.
"""
import pytest
from unittest.mock import patch, MagicMock

from app.x402.preflight import (
    plur_to_bzz,
    wei_to_xdai,
    check_xbzz_balance,
    check_xdai_balance,
    check_chequebook_balance,
    check_preflight_balances,
    PLUR_PER_BZZ,
    WEI_PER_XDAI,
)


class TestConversionFunctions:
    """Test unit conversion functions."""

    def test_plur_to_bzz_zero(self):
        """Zero PLUR equals zero BZZ."""
        assert plur_to_bzz(0) == 0.0

    def test_plur_to_bzz_one_bzz(self):
        """One BZZ worth of PLUR."""
        assert plur_to_bzz(PLUR_PER_BZZ) == 1.0

    def test_plur_to_bzz_fractional(self):
        """Fractional BZZ values."""
        assert plur_to_bzz(PLUR_PER_BZZ // 2) == 0.5

    def test_wei_to_xdai_zero(self):
        """Zero wei equals zero xDAI."""
        assert wei_to_xdai(0) == 0.0

    def test_wei_to_xdai_one_xdai(self):
        """One xDAI worth of wei."""
        assert wei_to_xdai(WEI_PER_XDAI) == 1.0

    def test_wei_to_xdai_fractional(self):
        """Fractional xDAI values."""
        assert wei_to_xdai(WEI_PER_XDAI // 2) == 0.5


class TestCheckXBZZBalance:
    """Test xBZZ balance checks."""

    @patch("app.x402.preflight.get_wallet_info")
    @patch("app.x402.preflight.settings")
    def test_xbzz_above_threshold(self, mock_settings, mock_get_wallet):
        """xBZZ balance above threshold returns ok=True."""
        mock_settings.X402_XBZZ_WARN_THRESHOLD = 10.0
        mock_get_wallet.return_value = {
            "walletAddress": "0x123",
            "bzzBalance": str(20 * PLUR_PER_BZZ)  # 20 BZZ
        }

        result = check_xbzz_balance()

        assert result["ok"] is True
        assert result["balance_bzz"] == 20.0
        assert result["threshold_bzz"] == 10.0
        assert result["warning"] is None

    @patch("app.x402.preflight.get_wallet_info")
    @patch("app.x402.preflight.settings")
    def test_xbzz_below_threshold(self, mock_settings, mock_get_wallet):
        """xBZZ balance below threshold returns ok=False with warning."""
        mock_settings.X402_XBZZ_WARN_THRESHOLD = 10.0
        mock_get_wallet.return_value = {
            "walletAddress": "0x123",
            "bzzBalance": str(5 * PLUR_PER_BZZ)  # 5 BZZ
        }

        result = check_xbzz_balance()

        assert result["ok"] is False
        assert result["balance_bzz"] == 5.0
        assert result["threshold_bzz"] == 10.0
        assert "below threshold" in result["warning"]

    @patch("app.x402.preflight.get_wallet_info")
    @patch("app.x402.preflight.settings")
    def test_xbzz_at_exact_threshold(self, mock_settings, mock_get_wallet):
        """xBZZ balance exactly at threshold returns ok=True."""
        mock_settings.X402_XBZZ_WARN_THRESHOLD = 10.0
        mock_get_wallet.return_value = {
            "walletAddress": "0x123",
            "bzzBalance": str(10 * PLUR_PER_BZZ)  # 10 BZZ
        }

        result = check_xbzz_balance()

        assert result["ok"] is True
        assert result["balance_bzz"] == 10.0

    @patch("app.x402.preflight.get_wallet_info")
    @patch("app.x402.preflight.settings")
    def test_xbzz_api_error(self, mock_settings, mock_get_wallet):
        """API error returns ok=False with error message."""
        mock_settings.X402_XBZZ_WARN_THRESHOLD = 10.0
        mock_get_wallet.side_effect = Exception("Connection refused")

        result = check_xbzz_balance()

        assert result["ok"] is False
        assert result["balance_bzz"] == 0.0
        assert "Failed to fetch" in result["warning"]


class TestCheckXDAIBalance:
    """Test xDAI balance checks."""

    @patch("app.x402.preflight.get_wallet_info")
    @patch("app.x402.preflight.settings")
    def test_xdai_above_threshold(self, mock_settings, mock_get_wallet):
        """xDAI balance above threshold returns ok=True."""
        mock_settings.X402_XDAI_WARN_THRESHOLD = 0.5
        mock_get_wallet.return_value = {
            "walletAddress": "0x123",
            "nativeTokenBalance": str(int(1.0 * WEI_PER_XDAI))  # 1 xDAI
        }

        result = check_xdai_balance()

        assert result["ok"] is True
        assert result["balance_xdai"] == 1.0
        assert result["threshold_xdai"] == 0.5
        assert result["warning"] is None

    @patch("app.x402.preflight.get_wallet_info")
    @patch("app.x402.preflight.settings")
    def test_xdai_below_threshold(self, mock_settings, mock_get_wallet):
        """xDAI balance below threshold returns ok=False with warning."""
        mock_settings.X402_XDAI_WARN_THRESHOLD = 0.5
        mock_get_wallet.return_value = {
            "walletAddress": "0x123",
            "nativeTokenBalance": str(int(0.1 * WEI_PER_XDAI))  # 0.1 xDAI
        }

        result = check_xdai_balance()

        assert result["ok"] is False
        assert result["balance_xdai"] == 0.1
        assert "below threshold" in result["warning"]
        assert "gas" in result["warning"].lower()

    @patch("app.x402.preflight.get_wallet_info")
    @patch("app.x402.preflight.settings")
    def test_xdai_missing_field(self, mock_settings, mock_get_wallet):
        """Missing nativeTokenBalance field defaults to 0."""
        mock_settings.X402_XDAI_WARN_THRESHOLD = 0.5
        mock_get_wallet.return_value = {
            "walletAddress": "0x123",
            # nativeTokenBalance missing
        }

        result = check_xdai_balance()

        assert result["ok"] is False
        assert result["balance_xdai"] == 0.0


class TestCheckChequebookBalance:
    """Test chequebook balance checks."""

    @patch("app.x402.preflight.get_chequebook_balance")
    @patch("app.x402.preflight.settings")
    def test_chequebook_above_threshold(self, mock_settings, mock_get_chequebook):
        """Chequebook balance above threshold returns ok=True."""
        mock_settings.X402_CHEQUEBOOK_WARN_THRESHOLD = 5.0
        mock_get_chequebook.return_value = {
            "availableBalance": str(10 * PLUR_PER_BZZ),  # 10 BZZ
            "totalBalance": str(15 * PLUR_PER_BZZ)  # 15 BZZ
        }

        result = check_chequebook_balance()

        assert result["ok"] is True
        assert result["available_balance_bzz"] == 10.0
        assert result["total_balance_bzz"] == 15.0
        assert result["threshold_bzz"] == 5.0
        assert result["warning"] is None

    @patch("app.x402.preflight.get_chequebook_balance")
    @patch("app.x402.preflight.settings")
    def test_chequebook_below_threshold(self, mock_settings, mock_get_chequebook):
        """Chequebook balance below threshold returns ok=False with warning."""
        mock_settings.X402_CHEQUEBOOK_WARN_THRESHOLD = 5.0
        mock_get_chequebook.return_value = {
            "availableBalance": str(2 * PLUR_PER_BZZ),  # 2 BZZ
            "totalBalance": str(5 * PLUR_PER_BZZ)  # 5 BZZ
        }

        result = check_chequebook_balance()

        assert result["ok"] is False
        assert result["available_balance_bzz"] == 2.0
        assert "below threshold" in result["warning"]
        assert "bandwidth" in result["warning"].lower()

    @patch("app.x402.preflight.get_chequebook_balance")
    @patch("app.x402.preflight.settings")
    def test_chequebook_api_error(self, mock_settings, mock_get_chequebook):
        """API error returns ok=False with error message."""
        mock_settings.X402_CHEQUEBOOK_WARN_THRESHOLD = 5.0
        mock_get_chequebook.side_effect = Exception("Connection refused")

        result = check_chequebook_balance()

        assert result["ok"] is False
        assert result["available_balance_bzz"] == 0.0
        assert "Failed to fetch" in result["warning"]


class TestCheckPreflightBalances:
    """Test combined pre-flight balance checks."""

    @patch("app.x402.preflight.check_chequebook_balance")
    @patch("app.x402.preflight.check_xdai_balance")
    @patch("app.x402.preflight.check_xbzz_balance")
    def test_all_checks_pass(self, mock_xbzz, mock_xdai, mock_chequebook):
        """All checks passing returns can_accept=True."""
        mock_xbzz.return_value = {
            "ok": True,
            "balance_plur": 20 * PLUR_PER_BZZ,
            "balance_bzz": 20.0,
            "threshold_bzz": 10.0,
            "warning": None
        }
        mock_xdai.return_value = {
            "ok": True,
            "balance_wei": int(1.0 * WEI_PER_XDAI),
            "balance_xdai": 1.0,
            "threshold_xdai": 0.5,
            "warning": None
        }
        mock_chequebook.return_value = {
            "ok": True,
            "available_balance_plur": 10 * PLUR_PER_BZZ,
            "available_balance_bzz": 10.0,
            "total_balance_plur": 15 * PLUR_PER_BZZ,
            "total_balance_bzz": 15.0,
            "threshold_bzz": 5.0,
            "warning": None
        }

        result = check_preflight_balances()

        assert result["can_accept"] is True
        assert result["xbzz_ok"] is True
        assert result["xdai_ok"] is True
        assert result["chequebook_ok"] is True
        assert len(result["warnings"]) == 0
        assert len(result["errors"]) == 0

    @patch("app.x402.preflight.check_chequebook_balance")
    @patch("app.x402.preflight.check_xdai_balance")
    @patch("app.x402.preflight.check_xbzz_balance")
    def test_low_balance_warnings(self, mock_xbzz, mock_xdai, mock_chequebook):
        """Low balances generate warnings but can still accept."""
        mock_xbzz.return_value = {
            "ok": False,
            "balance_plur": 5 * PLUR_PER_BZZ,
            "balance_bzz": 5.0,
            "threshold_bzz": 10.0,
            "warning": "xBZZ balance below threshold"
        }
        mock_xdai.return_value = {
            "ok": True,
            "balance_wei": int(1.0 * WEI_PER_XDAI),
            "balance_xdai": 1.0,
            "threshold_xdai": 0.5,
            "warning": None
        }
        mock_chequebook.return_value = {
            "ok": True,
            "available_balance_plur": 10 * PLUR_PER_BZZ,
            "available_balance_bzz": 10.0,
            "total_balance_plur": 15 * PLUR_PER_BZZ,
            "total_balance_bzz": 15.0,
            "threshold_bzz": 5.0,
            "warning": None
        }

        result = check_preflight_balances()

        # Can accept even with low balance (warning only)
        assert result["can_accept"] is True
        assert result["xbzz_ok"] is False
        assert len(result["warnings"]) == 1
        assert len(result["errors"]) == 0

    @patch("app.x402.preflight.check_chequebook_balance")
    @patch("app.x402.preflight.check_xdai_balance")
    @patch("app.x402.preflight.check_xbzz_balance")
    def test_api_error_blocks_acceptance(self, mock_xbzz, mock_xdai, mock_chequebook):
        """API errors block payment acceptance."""
        mock_xbzz.return_value = {
            "ok": False,
            "balance_plur": 0,
            "balance_bzz": 0.0,
            "threshold_bzz": 10.0,
            "warning": "Failed to fetch xBZZ balance: Connection refused"
        }
        mock_xdai.return_value = {
            "ok": True,
            "balance_wei": int(1.0 * WEI_PER_XDAI),
            "balance_xdai": 1.0,
            "threshold_xdai": 0.5,
            "warning": None
        }
        mock_chequebook.return_value = {
            "ok": True,
            "available_balance_plur": 10 * PLUR_PER_BZZ,
            "available_balance_bzz": 10.0,
            "total_balance_plur": 15 * PLUR_PER_BZZ,
            "total_balance_bzz": 15.0,
            "threshold_bzz": 5.0,
            "warning": None
        }

        result = check_preflight_balances()

        # Cannot accept when API is unreachable
        assert result["can_accept"] is False
        assert len(result["errors"]) == 1
        assert "unreachable" in result["errors"][0].lower()

    @patch("app.x402.preflight.check_chequebook_balance")
    @patch("app.x402.preflight.check_xdai_balance")
    @patch("app.x402.preflight.check_xbzz_balance")
    def test_balances_structure(self, mock_xbzz, mock_xdai, mock_chequebook):
        """Verify balances structure in response."""
        mock_xbzz.return_value = {
            "ok": True,
            "balance_plur": 20 * PLUR_PER_BZZ,
            "balance_bzz": 20.0,
            "threshold_bzz": 10.0,
            "warning": None
        }
        mock_xdai.return_value = {
            "ok": True,
            "balance_wei": int(1.0 * WEI_PER_XDAI),
            "balance_xdai": 1.0,
            "threshold_xdai": 0.5,
            "warning": None
        }
        mock_chequebook.return_value = {
            "ok": True,
            "available_balance_plur": 10 * PLUR_PER_BZZ,
            "available_balance_bzz": 10.0,
            "total_balance_plur": 15 * PLUR_PER_BZZ,
            "total_balance_bzz": 15.0,
            "threshold_bzz": 5.0,
            "warning": None
        }

        result = check_preflight_balances()

        # Verify balances structure
        assert "balances" in result
        assert "xbzz" in result["balances"]
        assert "xdai" in result["balances"]
        assert "chequebook" in result["balances"]

        assert result["balances"]["xbzz"]["balance_bzz"] == 20.0
        assert result["balances"]["xbzz"]["threshold_bzz"] == 10.0

        assert result["balances"]["xdai"]["balance_xdai"] == 1.0
        assert result["balances"]["xdai"]["threshold_xdai"] == 0.5

        assert result["balances"]["chequebook"]["available_bzz"] == 10.0
        assert result["balances"]["chequebook"]["total_bzz"] == 15.0
        assert result["balances"]["chequebook"]["threshold_bzz"] == 5.0

    @patch("app.x402.preflight.check_chequebook_balance")
    @patch("app.x402.preflight.check_xdai_balance")
    @patch("app.x402.preflight.check_xbzz_balance")
    def test_multiple_warnings(self, mock_xbzz, mock_xdai, mock_chequebook):
        """Multiple low balances generate multiple warnings."""
        mock_xbzz.return_value = {
            "ok": False,
            "balance_plur": 5 * PLUR_PER_BZZ,
            "balance_bzz": 5.0,
            "threshold_bzz": 10.0,
            "warning": "xBZZ below threshold"
        }
        mock_xdai.return_value = {
            "ok": False,
            "balance_wei": int(0.1 * WEI_PER_XDAI),
            "balance_xdai": 0.1,
            "threshold_xdai": 0.5,
            "warning": "xDAI below threshold"
        }
        mock_chequebook.return_value = {
            "ok": False,
            "available_balance_plur": 2 * PLUR_PER_BZZ,
            "available_balance_bzz": 2.0,
            "total_balance_plur": 5 * PLUR_PER_BZZ,
            "total_balance_bzz": 5.0,
            "threshold_bzz": 5.0,
            "warning": "Chequebook below threshold"
        }

        result = check_preflight_balances()

        # Can still accept (low balance = warning, not error)
        assert result["can_accept"] is True
        assert result["xbzz_ok"] is False
        assert result["xdai_ok"] is False
        assert result["chequebook_ok"] is False
        assert len(result["warnings"]) == 3
        assert len(result["errors"]) == 0
