# tests/test_x402_access.py
"""
Unit tests for x402 access control (whitelist/blacklist).
"""
import pytest
from unittest.mock import patch

from app.x402.access import (
    parse_ip_list,
    ip_matches_list,
    is_ip_blacklisted,
    is_ip_whitelisted,
    check_access,
    get_access_control_status,
)


class TestParseIPList:
    """Test IP list parsing."""

    def test_parse_empty_string(self):
        """Empty string returns empty set."""
        assert parse_ip_list("") == set()
        assert parse_ip_list(None) == set()
        assert parse_ip_list("   ") == set()

    def test_parse_single_ip(self):
        """Parse single IP address."""
        result = parse_ip_list("192.168.1.1")
        assert "192.168.1.1" in result
        assert len(result) == 1

    def test_parse_multiple_ips(self):
        """Parse comma-separated IPs."""
        result = parse_ip_list("192.168.1.1, 10.0.0.1, 172.16.0.1")
        assert "192.168.1.1" in result
        assert "10.0.0.1" in result
        assert "172.16.0.1" in result
        assert len(result) == 3

    def test_parse_cidr_notation(self):
        """Parse CIDR notation."""
        result = parse_ip_list("192.168.0.0/24")
        assert "192.168.0.0/24" in result

    def test_parse_mixed_ips_and_cidrs(self):
        """Parse mixed IPs and CIDR ranges."""
        result = parse_ip_list("192.168.1.1, 10.0.0.0/8, 172.16.0.1")
        assert "192.168.1.1" in result
        assert "10.0.0.0/8" in result
        assert "172.16.0.1" in result
        assert len(result) == 3

    def test_parse_with_whitespace(self):
        """Handle whitespace in list."""
        result = parse_ip_list("  192.168.1.1  ,  10.0.0.1  ")
        assert "192.168.1.1" in result
        assert "10.0.0.1" in result

    def test_parse_invalid_ip_ignored(self):
        """Invalid IPs are skipped."""
        result = parse_ip_list("192.168.1.1, invalid, 10.0.0.1")
        assert "192.168.1.1" in result
        assert "10.0.0.1" in result
        assert len(result) == 2

    def test_parse_ipv6(self):
        """Parse IPv6 addresses."""
        result = parse_ip_list("::1, 2001:db8::1")
        assert "::1" in result
        assert "2001:db8::1" in result

    def test_parse_ipv6_cidr(self):
        """Parse IPv6 CIDR notation."""
        result = parse_ip_list("2001:db8::/32")
        assert "2001:db8::/32" in result


class TestIPMatchesList:
    """Test IP matching against lists."""

    def test_exact_match(self):
        """Exact IP match."""
        ip_list = {"192.168.1.1", "10.0.0.1"}
        assert ip_matches_list("192.168.1.1", ip_list) is True
        assert ip_matches_list("10.0.0.1", ip_list) is True
        assert ip_matches_list("192.168.1.2", ip_list) is False

    def test_cidr_match(self):
        """CIDR range matching."""
        ip_list = {"192.168.0.0/24"}
        assert ip_matches_list("192.168.0.1", ip_list) is True
        assert ip_matches_list("192.168.0.254", ip_list) is True
        assert ip_matches_list("192.168.1.1", ip_list) is False

    def test_mixed_match(self):
        """Mixed exact and CIDR matching."""
        ip_list = {"192.168.1.1", "10.0.0.0/8"}
        assert ip_matches_list("192.168.1.1", ip_list) is True
        assert ip_matches_list("10.5.5.5", ip_list) is True
        assert ip_matches_list("192.168.2.1", ip_list) is False

    def test_empty_list(self):
        """Empty list never matches."""
        assert ip_matches_list("192.168.1.1", set()) is False

    def test_invalid_client_ip(self):
        """Invalid client IP returns False."""
        ip_list = {"192.168.1.1"}
        assert ip_matches_list("invalid", ip_list) is False
        assert ip_matches_list("", ip_list) is False

    def test_ipv6_match(self):
        """IPv6 address matching."""
        ip_list = {"::1", "2001:db8::/32"}
        assert ip_matches_list("::1", ip_list) is True
        assert ip_matches_list("2001:db8::1", ip_list) is True
        assert ip_matches_list("2001:db9::1", ip_list) is False


class TestIsIPBlacklisted:
    """Test blacklist checking."""

    @patch("app.x402.access.settings")
    def test_blacklisted_ip(self, mock_settings):
        """Blacklisted IP returns True."""
        mock_settings.X402_BLACKLIST_IPS = "192.168.1.100, 10.0.0.0/8"

        assert is_ip_blacklisted("192.168.1.100") is True
        assert is_ip_blacklisted("10.5.5.5") is True

    @patch("app.x402.access.settings")
    def test_not_blacklisted_ip(self, mock_settings):
        """Non-blacklisted IP returns False."""
        mock_settings.X402_BLACKLIST_IPS = "192.168.1.100"

        assert is_ip_blacklisted("192.168.1.101") is False
        assert is_ip_blacklisted("10.0.0.1") is False

    @patch("app.x402.access.settings")
    def test_empty_blacklist(self, mock_settings):
        """Empty blacklist never matches."""
        mock_settings.X402_BLACKLIST_IPS = ""

        assert is_ip_blacklisted("192.168.1.1") is False

    @patch("app.x402.access.settings")
    def test_none_blacklist(self, mock_settings):
        """None blacklist never matches."""
        mock_settings.X402_BLACKLIST_IPS = None

        assert is_ip_blacklisted("192.168.1.1") is False


class TestIsIPWhitelisted:
    """Test whitelist checking."""

    @patch("app.x402.access.settings")
    def test_whitelisted_ip(self, mock_settings):
        """Whitelisted IP returns True."""
        mock_settings.X402_WHITELIST_IPS = "192.168.1.50, 172.16.0.0/12"

        assert is_ip_whitelisted("192.168.1.50") is True
        assert is_ip_whitelisted("172.20.0.1") is True

    @patch("app.x402.access.settings")
    def test_not_whitelisted_ip(self, mock_settings):
        """Non-whitelisted IP returns False."""
        mock_settings.X402_WHITELIST_IPS = "192.168.1.50"

        assert is_ip_whitelisted("192.168.1.51") is False

    @patch("app.x402.access.settings")
    def test_empty_whitelist(self, mock_settings):
        """Empty whitelist never matches."""
        mock_settings.X402_WHITELIST_IPS = ""

        assert is_ip_whitelisted("192.168.1.1") is False


class TestCheckAccess:
    """Test the main check_access function."""

    @patch("app.x402.access.settings")
    def test_blacklisted_returns_blocked(self, mock_settings):
        """Blacklisted IP returns 'blocked' status."""
        mock_settings.X402_BLACKLIST_IPS = "192.168.1.100"
        mock_settings.X402_WHITELIST_IPS = ""

        status, reason = check_access("192.168.1.100")
        assert status == "blocked"
        assert reason == "IP address is blocked"

    @patch("app.x402.access.settings")
    def test_whitelisted_returns_free(self, mock_settings):
        """Whitelisted IP returns 'free' status."""
        mock_settings.X402_BLACKLIST_IPS = ""
        mock_settings.X402_WHITELIST_IPS = "192.168.1.50"

        status, reason = check_access("192.168.1.50")
        assert status == "free"
        assert reason is None

    @patch("app.x402.access.settings")
    def test_normal_returns_pay(self, mock_settings):
        """Normal IP returns 'pay' status."""
        mock_settings.X402_BLACKLIST_IPS = ""
        mock_settings.X402_WHITELIST_IPS = ""

        status, reason = check_access("192.168.1.1")
        assert status == "pay"
        assert reason is None

    @patch("app.x402.access.settings")
    def test_blacklist_takes_precedence_over_whitelist(self, mock_settings):
        """If IP is in both lists, blacklist takes precedence."""
        mock_settings.X402_BLACKLIST_IPS = "192.168.1.100"
        mock_settings.X402_WHITELIST_IPS = "192.168.1.100"

        status, reason = check_access("192.168.1.100")
        assert status == "blocked"

    @patch("app.x402.access.settings")
    def test_cidr_blacklist(self, mock_settings):
        """CIDR range blacklisting."""
        mock_settings.X402_BLACKLIST_IPS = "192.168.0.0/16"
        mock_settings.X402_WHITELIST_IPS = ""

        status, _ = check_access("192.168.50.100")
        assert status == "blocked"

        status, _ = check_access("10.0.0.1")
        assert status == "pay"

    @patch("app.x402.access.settings")
    def test_cidr_whitelist(self, mock_settings):
        """CIDR range whitelisting."""
        mock_settings.X402_BLACKLIST_IPS = ""
        mock_settings.X402_WHITELIST_IPS = "10.0.0.0/8"

        status, _ = check_access("10.50.100.200")
        assert status == "free"

        status, _ = check_access("192.168.1.1")
        assert status == "pay"

    @patch("app.x402.access.settings")
    def test_wallet_address_parameter_accepted(self, mock_settings):
        """Wallet address parameter is accepted (future use)."""
        mock_settings.X402_BLACKLIST_IPS = ""
        mock_settings.X402_WHITELIST_IPS = ""

        # Should not raise error
        status, reason = check_access("192.168.1.1", wallet_address="0x1234")
        assert status == "pay"


class TestGetAccessControlStatus:
    """Test access control status reporting."""

    @patch("app.x402.access.settings")
    def test_status_with_entries(self, mock_settings):
        """Status includes correct counts and entries."""
        mock_settings.X402_BLACKLIST_IPS = "192.168.1.100, 10.0.0.0/8"
        mock_settings.X402_WHITELIST_IPS = "172.16.0.1"

        status = get_access_control_status()

        assert status["blacklist_count"] == 2
        assert status["whitelist_count"] == 1
        assert "192.168.1.100" in status["blacklist_entries"]
        assert "10.0.0.0/8" in status["blacklist_entries"]
        assert "172.16.0.1" in status["whitelist_entries"]

    @patch("app.x402.access.settings")
    def test_status_empty_lists(self, mock_settings):
        """Status with empty lists."""
        mock_settings.X402_BLACKLIST_IPS = ""
        mock_settings.X402_WHITELIST_IPS = None

        status = get_access_control_status()

        assert status["blacklist_count"] == 0
        assert status["whitelist_count"] == 0
        assert status["blacklist_entries"] == []
        assert status["whitelist_entries"] == []


class TestEdgeCases:
    """Test edge cases and special scenarios."""

    @patch("app.x402.access.settings")
    def test_localhost_ipv4(self, mock_settings):
        """Localhost IPv4 can be whitelisted."""
        mock_settings.X402_BLACKLIST_IPS = ""
        mock_settings.X402_WHITELIST_IPS = "127.0.0.1"

        status, _ = check_access("127.0.0.1")
        assert status == "free"

    @patch("app.x402.access.settings")
    def test_localhost_ipv6(self, mock_settings):
        """Localhost IPv6 can be whitelisted."""
        mock_settings.X402_BLACKLIST_IPS = ""
        mock_settings.X402_WHITELIST_IPS = "::1"

        status, _ = check_access("::1")
        assert status == "free"

    @patch("app.x402.access.settings")
    def test_duplicate_entries_handled(self, mock_settings):
        """Duplicate entries in list are deduplicated."""
        mock_settings.X402_BLACKLIST_IPS = "192.168.1.1, 192.168.1.1, 192.168.1.1"
        mock_settings.X402_WHITELIST_IPS = ""

        status = get_access_control_status()
        assert status["blacklist_count"] == 1

    @patch("app.x402.access.settings")
    def test_trailing_comma_handled(self, mock_settings):
        """Trailing comma is handled gracefully."""
        mock_settings.X402_BLACKLIST_IPS = "192.168.1.1,"
        mock_settings.X402_WHITELIST_IPS = ""

        status = get_access_control_status()
        assert status["blacklist_count"] == 1
        assert "192.168.1.1" in status["blacklist_entries"]

    @patch("app.x402.access.settings")
    def test_leading_comma_handled(self, mock_settings):
        """Leading comma is handled gracefully."""
        mock_settings.X402_BLACKLIST_IPS = ",192.168.1.1"
        mock_settings.X402_WHITELIST_IPS = ""

        status = get_access_control_status()
        assert status["blacklist_count"] == 1
