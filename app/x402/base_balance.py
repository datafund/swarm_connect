# app/x402/base_balance.py
"""
Base ETH balance of the x402 pay-to wallet, reported as information only (#663).

With x402 `exact` (EIP-3009) the facilitator submits and pays for the USDC
transfer, and the pay-to wallet only receives. It needs no ETH: 0 ETH is a
normal state, for example a new Safe. The balance is read for /health and the
gateway_base_eth_balance gauge, and never degrades /health, fires an alert or
blocks a request. There are no thresholds.
"""
import logging
import time
from typing import Dict, Any, Optional

from app.core.config import settings
from app.services.bee_errors import error_cause
from app.services.http_client import exc_text, get_client

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


async def _get_eth_balance_from_rpc(address: str) -> int:
    """
    Fetch the ETH balance from the Base RPC (BASE_RPC_URL).

    Args:
        address: Ethereum address to check

    Returns:
        Balance in wei

    Raises:
        Exception: If RPC call fails
    """
    client = get_client()
    response = await client.post(
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


async def check_base_eth_balance() -> Dict[str, Any]:
    """
    Read the pay-to wallet's Base ETH balance, as information.

    Results are cached for 60 seconds to avoid excessive RPC calls.

    Returns:
        Dict containing:
        - ok: bool - always True: the balance never makes the gateway unhealthy
        - is_critical: bool - always False, for the same reason
        - informational: bool - always True
        - fetched: bool - whether the balance was actually read
        - balance_wei: int - raw balance in wei (0 when not read)
        - balance_eth: float - balance in ETH (0.0 when not read)
        - threshold_eth, critical_eth: None - there are no thresholds (#663);
          kept so existing readers of /health keep parsing
        - address: str - wallet address being read
        - warning: str or None - why the balance could not be read, if it could not
    """
    address = settings.X402_PAY_TO_ADDRESS
    result: Dict[str, Any] = {
        "ok": True,
        "is_critical": False,
        "informational": True,
        "fetched": False,
        "balance_wei": 0,
        "balance_eth": 0.0,
        "threshold_eth": None,
        "critical_eth": None,
        "address": address or None,
        "warning": None,
    }

    if not address:
        # validate_x402_config refuses to start without one when x402 is on.
        logger.debug("X402_PAY_TO_ADDRESS not configured - cannot read the Base ETH balance")
        result["warning"] = "X402_PAY_TO_ADDRESS not configured"
        return result

    try:
        # Try to use cached balance first
        balance_wei = _get_cached_balance()
        if balance_wei is None:
            balance_wei = await _get_eth_balance_from_rpc(address)
            _update_cache(balance_wei)
            logger.debug(f"Fetched Base ETH balance: {wei_to_eth(balance_wei):.6f} ETH")

        result["fetched"] = True
        result["balance_wei"] = balance_wei
        result["balance_eth"] = wei_to_eth(balance_wei)
        return result

    except Exception as e:
        logger.error(f"Failed to check Base ETH balance: {exc_text(e)}")
        # str(e) names BASE_RPC_URL, which can carry an API key (#549);
        # the full text is logged above.
        result["warning"] = (f"Failed to fetch Base ETH balance "
                             f"({error_cause(e)}); see the gateway log for details.")
        return result
