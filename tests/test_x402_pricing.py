# tests/test_x402_pricing.py
"""
Unit tests for x402 pricing service.
"""
import pytest
from unittest.mock import patch, MagicMock

from app.x402.pricing import (
    plur_to_bzz,
    bzz_to_usd,
    apply_markup,
    apply_minimum_price,
    calculate_stamp_price_usd,
    calculate_upload_price_usd,
    get_price_quote,
    PLUR_PER_BZZ,
)


class TestConversionFunctions:
    """Test unit conversion and helper functions."""

    def test_plur_to_bzz_zero(self):
        """Zero PLUR equals zero BZZ."""
        assert plur_to_bzz(0) == 0.0

    def test_plur_to_bzz_one_bzz(self):
        """One BZZ worth of PLUR."""
        assert plur_to_bzz(PLUR_PER_BZZ) == 1.0

    def test_plur_to_bzz_fractional(self):
        """Fractional BZZ values."""
        assert plur_to_bzz(PLUR_PER_BZZ // 2) == 0.5

    def test_bzz_to_usd_with_rate(self):
        """Convert BZZ to USD with explicit rate."""
        assert bzz_to_usd(1.0, rate=0.50) == 0.50
        assert bzz_to_usd(2.0, rate=0.50) == 1.00
        assert bzz_to_usd(0.5, rate=1.00) == 0.50

    @patch("app.x402.pricing.settings")
    def test_bzz_to_usd_default_rate(self, mock_settings):
        """Convert BZZ to USD using config rate."""
        mock_settings.X402_BZZ_USD_RATE = 0.75
        assert bzz_to_usd(2.0) == 1.50

    def test_apply_markup_explicit(self):
        """Apply explicit markup percentage."""
        assert apply_markup(1.00, markup_percent=50) == 1.50
        assert apply_markup(1.00, markup_percent=100) == 2.00
        assert apply_markup(1.00, markup_percent=0) == 1.00

    @patch("app.x402.pricing.settings")
    def test_apply_markup_default(self, mock_settings):
        """Apply default markup from config."""
        mock_settings.X402_MARKUP_PERCENT = 25.0
        assert apply_markup(1.00) == 1.25

    def test_apply_minimum_price_explicit(self):
        """Apply explicit minimum price."""
        assert apply_minimum_price(0.005, minimum=0.01) == 0.01
        assert apply_minimum_price(0.05, minimum=0.01) == 0.05

    @patch("app.x402.pricing.settings")
    def test_apply_minimum_price_default(self, mock_settings):
        """Apply default minimum from config."""
        mock_settings.X402_MIN_PRICE_USD = 0.02
        assert apply_minimum_price(0.005) == 0.02
        assert apply_minimum_price(0.10) == 0.10


class TestCalculateStampPriceUSD:
    """Test stamp price calculations."""

    @patch("app.x402.pricing.settings")
    @patch("app.x402.pricing.get_chainstate")
    def test_basic_stamp_price(self, mock_chainstate, mock_settings):
        """Calculate basic stamp price."""
        # Configure mocks
        mock_settings.X402_BZZ_USD_RATE = 0.50
        mock_settings.X402_MARKUP_PERCENT = 50.0
        mock_settings.X402_MIN_PRICE_USD = 0.01

        # Current price of 1000 PLUR per chunk per block
        mock_chainstate.return_value = {"currentPrice": "1000"}

        result = calculate_stamp_price_usd(
            duration_hours=24,
            depth=17,
            include_breakdown=True
        )

        # Verify structure
        assert "price_usd" in result
        assert "price_bzz" in result
        assert "exchange_rate" in result
        assert "markup_percent" in result
        assert "minimum_applied" in result
        assert "breakdown" in result

        # Verify values are reasonable
        assert result["price_usd"] >= 0.01  # At least minimum price
        assert result["exchange_rate"] == 0.50
        assert result["markup_percent"] == 50.0

    @patch("app.x402.pricing.settings")
    @patch("app.x402.pricing.get_chainstate")
    def test_minimum_price_applied(self, mock_chainstate, mock_settings):
        """Verify minimum price is applied for small requests."""
        mock_settings.X402_BZZ_USD_RATE = 0.50
        mock_settings.X402_MARKUP_PERCENT = 0.0
        mock_settings.X402_MIN_PRICE_USD = 1.00

        # Very low price to trigger minimum
        mock_chainstate.return_value = {"currentPrice": "1"}

        result = calculate_stamp_price_usd(
            duration_hours=1,
            depth=17,
            include_breakdown=True
        )

        assert result["price_usd"] == 1.00
        assert result["minimum_applied"] is True

    @patch("app.x402.pricing.settings")
    @patch("app.x402.pricing.get_chainstate")
    def test_markup_applied(self, mock_chainstate, mock_settings):
        """Verify markup is correctly applied."""
        mock_settings.X402_BZZ_USD_RATE = 1.00  # 1:1 for easy calculation
        mock_settings.X402_MARKUP_PERCENT = 100.0  # Double the price
        mock_settings.X402_MIN_PRICE_USD = 0.0  # No minimum

        mock_chainstate.return_value = {"currentPrice": "1000000"}

        result = calculate_stamp_price_usd(
            duration_hours=24,
            depth=17,
            include_breakdown=True
        )

        # With 100% markup, price should be double the base cost
        base_cost = result["breakdown"]["cost_usd_before_markup"]
        final_price = result["price_usd"]

        # Allow for rounding
        assert abs(final_price - base_cost * 2) < 0.01

    @patch("app.x402.pricing.get_chainstate")
    def test_invalid_chainstate_price(self, mock_chainstate):
        """Raise error for invalid chainstate price."""
        mock_chainstate.return_value = {"currentPrice": "0"}

        with pytest.raises(ValueError, match="Invalid current price"):
            calculate_stamp_price_usd(duration_hours=24, depth=17)

    @patch("app.x402.pricing.settings")
    @patch("app.x402.pricing.get_chainstate")
    def test_breakdown_structure(self, mock_chainstate, mock_settings):
        """Verify breakdown contains all expected fields."""
        mock_settings.X402_BZZ_USD_RATE = 0.50
        mock_settings.X402_MARKUP_PERCENT = 50.0
        mock_settings.X402_MIN_PRICE_USD = 0.01

        mock_chainstate.return_value = {"currentPrice": "1000"}

        result = calculate_stamp_price_usd(
            duration_hours=24,
            depth=17,
            include_breakdown=True
        )

        breakdown = result["breakdown"]
        assert "duration_hours" in breakdown
        assert "depth" in breakdown
        assert "current_price_plur" in breakdown
        assert "amount_plur_per_chunk" in breakdown
        assert "total_cost_plur" in breakdown
        assert "cost_bzz" in breakdown
        assert "cost_usd_before_markup" in breakdown
        assert "cost_usd_with_markup" in breakdown
        assert "minimum_price_usd" in breakdown
        assert "final_price_usd" in breakdown

    @patch("app.x402.pricing.settings")
    @patch("app.x402.pricing.get_chainstate")
    def test_no_breakdown_option(self, mock_chainstate, mock_settings):
        """Verify breakdown can be excluded."""
        mock_settings.X402_BZZ_USD_RATE = 0.50
        mock_settings.X402_MARKUP_PERCENT = 50.0
        mock_settings.X402_MIN_PRICE_USD = 0.01

        mock_chainstate.return_value = {"currentPrice": "1000"}

        result = calculate_stamp_price_usd(
            duration_hours=24,
            depth=17,
            include_breakdown=False
        )

        assert "breakdown" not in result


class TestCalculateUploadPriceUSD:
    """Test upload price calculations."""

    @patch("app.x402.pricing.settings")
    @patch("app.x402.pricing.get_chainstate")
    def test_small_upload(self, mock_chainstate, mock_settings):
        """Calculate price for small upload (fits in minimum depth)."""
        mock_settings.X402_BZZ_USD_RATE = 0.50
        mock_settings.X402_MARKUP_PERCENT = 50.0
        mock_settings.X402_MIN_PRICE_USD = 0.01

        mock_chainstate.return_value = {"currentPrice": "1000"}

        # 1 KB upload
        result = calculate_upload_price_usd(
            size_bytes=1024,
            duration_hours=24,
            include_breakdown=True
        )

        assert "price_usd" in result
        assert "breakdown" in result
        assert result["breakdown"]["depth_used"] == 17  # Minimum depth

    @patch("app.x402.pricing.settings")
    @patch("app.x402.pricing.get_chainstate")
    def test_large_upload_increases_depth(self, mock_chainstate, mock_settings):
        """Large uploads should use higher depth."""
        mock_settings.X402_BZZ_USD_RATE = 0.50
        mock_settings.X402_MARKUP_PERCENT = 50.0
        mock_settings.X402_MIN_PRICE_USD = 0.01

        mock_chainstate.return_value = {"currentPrice": "1000"}

        # 1 GB upload - should need depth > 17
        result = calculate_upload_price_usd(
            size_bytes=1024 * 1024 * 1024,  # 1 GB
            duration_hours=24,
            include_breakdown=True
        )

        # 1 GB needs more than 2^17 chunks
        assert result["breakdown"]["depth_used"] > 17

    @patch("app.x402.pricing.settings")
    @patch("app.x402.pricing.get_chainstate")
    def test_upload_breakdown_structure(self, mock_chainstate, mock_settings):
        """Verify upload breakdown contains expected fields."""
        mock_settings.X402_BZZ_USD_RATE = 0.50
        mock_settings.X402_MARKUP_PERCENT = 50.0
        mock_settings.X402_MIN_PRICE_USD = 0.01

        mock_chainstate.return_value = {"currentPrice": "1000"}

        result = calculate_upload_price_usd(
            size_bytes=1024,
            duration_hours=24,
            include_breakdown=True
        )

        breakdown = result["breakdown"]
        assert "size_bytes" in breakdown
        assert "chunks_needed" in breakdown
        assert "depth_used" in breakdown
        assert "capacity_chunks" in breakdown
        assert "duration_hours" in breakdown
        assert "stamp_breakdown" in breakdown


class TestGetPriceQuote:
    """Test price quote generation."""

    @patch("app.x402.pricing.settings")
    @patch("app.x402.pricing.get_chainstate")
    def test_stamp_purchase_quote(self, mock_chainstate, mock_settings):
        """Generate quote for stamp purchase."""
        mock_settings.X402_BZZ_USD_RATE = 0.50
        mock_settings.X402_MARKUP_PERCENT = 50.0
        mock_settings.X402_MIN_PRICE_USD = 0.01
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_PAY_TO_ADDRESS = "0x1234567890abcdef"

        mock_chainstate.return_value = {"currentPrice": "1000"}

        result = get_price_quote(
            operation="stamp_purchase",
            duration_hours=24,
            depth=17
        )

        assert "price_usd" in result
        assert result["currency"] == "USDC"
        assert result["network"] == "base-sepolia"
        assert result["pay_to"] == "0x1234567890abcdef"
        assert "details" in result

    @patch("app.x402.pricing.settings")
    @patch("app.x402.pricing.get_chainstate")
    def test_upload_quote(self, mock_chainstate, mock_settings):
        """Generate quote for upload."""
        mock_settings.X402_BZZ_USD_RATE = 0.50
        mock_settings.X402_MARKUP_PERCENT = 50.0
        mock_settings.X402_MIN_PRICE_USD = 0.01
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_PAY_TO_ADDRESS = "0x1234567890abcdef"

        mock_chainstate.return_value = {"currentPrice": "1000"}

        result = get_price_quote(
            operation="upload",
            size_bytes=1024,
            duration_hours=24
        )

        assert "price_usd" in result
        assert result["currency"] == "USDC"
        assert result["network"] == "base-sepolia"

    @patch("app.x402.pricing.settings")
    @patch("app.x402.pricing.get_chainstate")
    def test_missing_pay_to_address(self, mock_chainstate, mock_settings):
        """Handle missing pay_to address gracefully."""
        mock_settings.X402_BZZ_USD_RATE = 0.50
        mock_settings.X402_MARKUP_PERCENT = 50.0
        mock_settings.X402_MIN_PRICE_USD = 0.01
        mock_settings.X402_NETWORK = "base-sepolia"
        mock_settings.X402_PAY_TO_ADDRESS = None

        mock_chainstate.return_value = {"currentPrice": "1000"}

        result = get_price_quote(
            operation="stamp_purchase",
            duration_hours=24
        )

        # Should use placeholder address
        assert result["pay_to"] == "0x0000000000000000000000000000000000000000"

    def test_unknown_operation(self):
        """Raise error for unknown operation type."""
        with pytest.raises(ValueError, match="Unknown operation type"):
            get_price_quote(operation="unknown_operation")


class TestPricingFormulas:
    """Test the pricing formula calculations."""

    @patch("app.x402.pricing.settings")
    @patch("app.x402.pricing.get_chainstate")
    def test_price_increases_with_duration(self, mock_chainstate, mock_settings):
        """Longer duration should increase price."""
        mock_settings.X402_BZZ_USD_RATE = 0.50
        mock_settings.X402_MARKUP_PERCENT = 50.0
        mock_settings.X402_MIN_PRICE_USD = 0.0

        mock_chainstate.return_value = {"currentPrice": "1000000"}

        price_24h = calculate_stamp_price_usd(duration_hours=24, depth=17)
        price_48h = calculate_stamp_price_usd(duration_hours=48, depth=17)

        assert price_48h["price_usd"] > price_24h["price_usd"]

    @patch("app.x402.pricing.settings")
    @patch("app.x402.pricing.get_chainstate")
    def test_price_increases_with_depth(self, mock_chainstate, mock_settings):
        """Higher depth (more storage) should increase price."""
        mock_settings.X402_BZZ_USD_RATE = 0.50
        mock_settings.X402_MARKUP_PERCENT = 50.0
        mock_settings.X402_MIN_PRICE_USD = 0.0

        mock_chainstate.return_value = {"currentPrice": "1000000"}

        price_d17 = calculate_stamp_price_usd(duration_hours=24, depth=17)
        price_d20 = calculate_stamp_price_usd(duration_hours=24, depth=20)

        # depth 20 has 8x the capacity of depth 17, so should cost more
        assert price_d20["price_usd"] > price_d17["price_usd"]

    @patch("app.x402.pricing.settings")
    @patch("app.x402.pricing.get_chainstate")
    def test_price_scales_with_exchange_rate(self, mock_chainstate, mock_settings):
        """Higher exchange rate should increase USD price."""
        mock_settings.X402_MARKUP_PERCENT = 0.0
        mock_settings.X402_MIN_PRICE_USD = 0.0

        mock_chainstate.return_value = {"currentPrice": "1000000"}

        # Price at $0.50/BZZ
        mock_settings.X402_BZZ_USD_RATE = 0.50
        price_low = calculate_stamp_price_usd(duration_hours=24, depth=17)

        # Price at $1.00/BZZ (2x rate)
        mock_settings.X402_BZZ_USD_RATE = 1.00
        price_high = calculate_stamp_price_usd(duration_hours=24, depth=17)

        # Price should approximately double
        ratio = price_high["price_usd"] / price_low["price_usd"]
        assert 1.9 < ratio < 2.1  # Allow small rounding variance
