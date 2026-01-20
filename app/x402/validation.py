# app/x402/validation.py
"""
Startup validation for x402 configuration.

When X402_ENABLED=true, validates that all required configuration is present
and correctly formatted before the application starts accepting requests.
"""
import re
import logging
from typing import List, Tuple

from app.core.config import settings

logger = logging.getLogger(__name__)

# Ethereum address pattern (0x followed by 40 hex characters)
ETH_ADDRESS_PATTERN = re.compile(r"^0x[a-fA-F0-9]{40}$")

# Valid x402 networks
VALID_NETWORKS = ["base", "base-sepolia"]


class X402ConfigurationError(Exception):
    """Raised when x402 configuration is invalid."""
    pass


def validate_eth_address(address: str, field_name: str) -> Tuple[bool, str]:
    """
    Validate an Ethereum address format.

    Returns:
        Tuple of (is_valid, error_message)
    """
    if not address:
        return False, f"{field_name} is required but not set"

    if not ETH_ADDRESS_PATTERN.match(address):
        return False, f"{field_name} is not a valid Ethereum address: {address}"

    # Check for obviously invalid addresses
    if address == "0x0000000000000000000000000000000000000000":
        return False, f"{field_name} is set to the zero address"

    return True, ""


def validate_x402_config() -> List[str]:
    """
    Validate x402 configuration when X402_ENABLED=true.

    Returns:
        List of error messages (empty if all valid)
    """
    errors = []

    # Validate X402_PAY_TO_ADDRESS
    is_valid, error = validate_eth_address(
        settings.X402_PAY_TO_ADDRESS or "",
        "X402_PAY_TO_ADDRESS"
    )
    if not is_valid:
        errors.append(error)

    # Validate X402_NETWORK
    if settings.X402_NETWORK not in VALID_NETWORKS:
        errors.append(
            f"X402_NETWORK must be one of {VALID_NETWORKS}, got: {settings.X402_NETWORK}"
        )

    # Validate X402_FACILITATOR_URL
    if not settings.X402_FACILITATOR_URL:
        errors.append("X402_FACILITATOR_URL is required but not set")
    elif not settings.X402_FACILITATOR_URL.startswith(("http://", "https://")):
        errors.append(
            f"X402_FACILITATOR_URL must be a valid URL: {settings.X402_FACILITATOR_URL}"
        )

    # Validate pricing settings
    if settings.X402_MIN_PRICE_USD <= 0:
        errors.append("X402_MIN_PRICE_USD must be positive")

    if settings.X402_BZZ_USD_RATE <= 0:
        errors.append("X402_BZZ_USD_RATE must be positive")

    # Validate rate limits
    if settings.X402_RATE_LIMIT_PER_IP <= 0:
        errors.append("X402_RATE_LIMIT_PER_IP must be positive")

    if settings.X402_FREE_TIER_ENABLED and settings.X402_FREE_TIER_RATE_LIMIT <= 0:
        errors.append("X402_FREE_TIER_RATE_LIMIT must be positive when free tier is enabled")

    return errors


def check_x402_startup() -> None:
    """
    Check x402 configuration at startup.

    If X402_ENABLED=true and configuration is invalid, raises X402ConfigurationError.
    If X402_ENABLED=false, logs info and returns.

    Raises:
        X402ConfigurationError: If x402 is enabled but configuration is invalid
    """
    if not settings.X402_ENABLED:
        logger.info("x402 is disabled (X402_ENABLED=false)")
        return

    logger.info("x402 is enabled - validating configuration...")

    errors = validate_x402_config()

    if errors:
        error_msg = "x402 configuration errors:\n" + "\n".join(f"  - {e}" for e in errors)
        logger.error(error_msg)
        raise X402ConfigurationError(error_msg)

    # Log successful configuration
    logger.info(f"x402 configuration valid:")
    logger.info(f"  - Network: {settings.X402_NETWORK}")
    logger.info(f"  - Pay-to address: {settings.X402_PAY_TO_ADDRESS}")
    logger.info(f"  - Facilitator: {settings.X402_FACILITATOR_URL}")
    logger.info(f"  - Free tier: {'enabled' if settings.X402_FREE_TIER_ENABLED else 'disabled'}")
