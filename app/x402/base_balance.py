# app/x402/base_balance.py
"""
Base Sepolia ETH balance monitoring for x402 gateway wallet.

This module monitors the gateway wallet's ETH balance on Base Sepolia
to ensure there's sufficient gas for the facilitator to execute USDC transfers.

Balance thresholds are configured via environment variables in app/core/config.py.
"""
import logging
import time
import requests
from typing import Dict, Any, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

# Conversion constant
WEI_PER_ETH = 10 ** 18

# Cache for balance checks (avoid hammering RPC endpoint)
_balance_cache: Dict[str, Any] = {
    "balance_wei": None,
    "timestamp": 0,
}
CACHE_TTL_SECONDS = 60  # Cache balance for 60 seconds


def wei_to_eth(wei: int) -> float:
    """Convert wei to ETH."""
    return wei / WEI_PER_ETH


def _get_eth_balance_from_rpc(address: str) -> int:
    """
    Fetch ETH balance from Base Sepolia RPC.

    Args:
        address: Ethereum address to check

    Returns:
        Balance in wei

    Raises:
        Exception: If RPC call fails
    """
    response = requests.post(
        settings.BASE_RPC_URL,
        json={
            "jsonrpc": "2.0",
            "method": "eth_getBalance",
            "params": [address, "latest"],
            "id": 1
        },
        timeout=10
    )
    response.raise_for_status()

    result = response.json()
    if "error" in result:
        raise Exception(f"RPC error: {result['error']}")

    if "result" not in result:
        raise Exception(f"Invalid RPC response: missing 'result' field")

    # Convert hex to int
    return int(result["result"], 16)


def _get_cached_balance() -> Optional[int]:
    """
    Get cached balance if still valid.

    Returns:
        Cached balance in wei, or None if cache expired/empty
    """
    if _balance_cache["balance_wei"] is None:
        return None

    age = time.time() - _balance_cache["timestamp"]
    if age > CACHE_TTL_SECONDS:
        return None

    return _balance_cache["balance_wei"]


def _update_cache(balance_wei: int) -> None:
    """Update the balance cache."""
    _balance_cache["balance_wei"] = balance_wei
    _balance_cache["timestamp"] = time.time()


def clear_balance_cache() -> None:
    """Clear the balance cache (useful for testing)."""
    _balance_cache["balance_wei"] = None
    _balance_cache["timestamp"] = 0


def check_base_eth_balance() -> Dict[str, Any]:
    """
    Check Base Sepolia ETH balance against configured thresholds.

    This function checks the gateway wallet's ETH balance and compares
    it against warning and critical thresholds. Results are cached
    for 60 seconds to avoid excessive RPC calls.

    Returns:
        Dict containing:
        - ok: bool - whether balance is above warning threshold
        - is_critical: bool - whether balance is below critical threshold
        - balance_wei: int - raw balance in wei
        - balance_eth: float - balance in ETH
        - threshold_eth: float - warning threshold in ETH
        - critical_eth: float - critical threshold in ETH
        - address: str - wallet address being monitored
        - warning: str or None - warning message if below threshold
    """
    address = settings.X402_PAY_TO_ADDRESS
    warn_threshold = settings.X402_BASE_ETH_WARN_THRESHOLD
    critical_threshold = settings.X402_BASE_ETH_CRITICAL_THRESHOLD

    # If no address configured, return error state
    if not address:
        logger.error("X402_PAY_TO_ADDRESS not configured - cannot check balance")
        return {
            "ok": False,
            "is_critical": True,
            "balance_wei": 0,
            "balance_eth": 0.0,
            "threshold_eth": warn_threshold,
            "critical_eth": critical_threshold,
            "address": None,
            "warning": "X402_PAY_TO_ADDRESS not configured"
        }

    try:
        # Try to use cached balance first
        balance_wei = _get_cached_balance()
        if balance_wei is None:
            balance_wei = _get_eth_balance_from_rpc(address)
            _update_cache(balance_wei)
            logger.debug(f"Fetched Base ETH balance: {wei_to_eth(balance_wei):.6f} ETH")

        balance_eth = wei_to_eth(balance_wei)

        # Determine status
        is_critical = balance_eth < critical_threshold
        ok = balance_eth >= warn_threshold

        # Build warning message
        warning = None
        if is_critical:
            warning = (
                f"Base wallet ETH critically low ({balance_eth:.6f} ETH). "
                f"Below critical threshold ({critical_threshold} ETH). "
                f"Cannot process x402 payments. Top up immediately!"
            )
            logger.error(f"Pre-flight check: {warning}")
        elif not ok:
            warning = (
                f"Base wallet ETH ({balance_eth:.6f} ETH) is below warning threshold "
                f"({warn_threshold} ETH). Top up your Base Sepolia wallet soon."
            )
            logger.warning(f"Pre-flight check: {warning}")

        return {
            "ok": ok,
            "is_critical": is_critical,
            "balance_wei": balance_wei,
            "balance_eth": balance_eth,
            "threshold_eth": warn_threshold,
            "critical_eth": critical_threshold,
            "address": address,
            "warning": warning
        }

    except Exception as e:
        logger.error(f"Failed to check Base ETH balance: {e}")
        return {
            "ok": False,
            "is_critical": True,  # Treat RPC errors as critical (can't verify)
            "balance_wei": 0,
            "balance_eth": 0.0,
            "threshold_eth": warn_threshold,
            "critical_eth": critical_threshold,
            "address": address,
            "warning": f"Failed to fetch Base ETH balance: {str(e)}"
        }
