# app/x402/pricing.py
"""
Price calculation for x402 payment responses.

This module calculates the final USD price for gateway operations:
1. Calculate BZZ cost (using existing swarm_api functions)
2. Convert BZZ to USD using configured exchange rate
3. Apply markup percentage
4. Ensure minimum price

Configuration is loaded from app/core/config.py:
- X402_BZZ_USD_RATE: Manual BZZ/USD exchange rate
- X402_MARKUP_PERCENT: Markup percentage to apply
- X402_MIN_PRICE_USD: Minimum price floor
"""
import logging
from typing import Dict, Any, Optional

from app.core.config import settings
from app.services.swarm_api import (
    get_chainstate,
    calculate_stamp_amount,
    calculate_stamp_total_cost,
)

logger = logging.getLogger(__name__)

# Conversion constants
PLUR_PER_BZZ = 10 ** 16  # 1 BZZ = 10^16 PLUR


def plur_to_bzz(plur: int) -> float:
    """Convert PLUR to BZZ."""
    return plur / PLUR_PER_BZZ


def bzz_to_usd(bzz: float, rate: Optional[float] = None) -> float:
    """
    Convert BZZ to USD using configured or provided exchange rate.

    Args:
        bzz: Amount in BZZ
        rate: Optional exchange rate (BZZ/USD). Uses config if not provided.

    Returns:
        Amount in USD
    """
    exchange_rate = rate if rate is not None else settings.X402_BZZ_USD_RATE
    return bzz * exchange_rate


def apply_markup(base_price: float, markup_percent: Optional[float] = None) -> float:
    """
    Apply markup percentage to base price.

    Args:
        base_price: Base price in USD
        markup_percent: Markup percentage. Uses config if not provided.

    Returns:
        Price with markup applied
    """
    markup = markup_percent if markup_percent is not None else settings.X402_MARKUP_PERCENT
    return base_price * (1 + markup / 100)


def apply_minimum_price(price: float, minimum: Optional[float] = None) -> float:
    """
    Ensure price meets minimum threshold.

    Args:
        price: Calculated price in USD
        minimum: Minimum price. Uses config if not provided.

    Returns:
        Price, at least the minimum value
    """
    min_price = minimum if minimum is not None else settings.X402_MIN_PRICE_USD
    return max(price, min_price)


def calculate_stamp_price_usd(
    duration_hours: int,
    depth: int = 17,
    include_breakdown: bool = True
) -> Dict[str, Any]:
    """
    Calculate the USD price for a stamp purchase.

    Formula:
    1. Get current price per chunk per block from chainstate
    2. Calculate amount needed for duration: amount = currentPrice * duration_blocks
    3. Calculate total BZZ cost: totalCost = amount * 2^depth
    4. Convert to USD: cost_usd = cost_bzz * exchange_rate
    5. Apply markup: final_usd = cost_usd * (1 + markup_percent/100)
    6. Enforce minimum price

    Args:
        duration_hours: Desired stamp duration in hours
        depth: Stamp depth (determines storage capacity, default 17)
        include_breakdown: Whether to include detailed breakdown

    Returns:
        Dict containing:
        - price_usd: float - final price in USD
        - price_bzz: float - base cost in BZZ (before markup)
        - exchange_rate: float - BZZ/USD rate used
        - markup_percent: float - markup applied
        - minimum_applied: bool - whether minimum price was applied
        - breakdown: dict - detailed cost breakdown (if include_breakdown=True)

    Raises:
        Exception: If unable to fetch chainstate from Bee node
    """
    # Get current price from chainstate
    chainstate = get_chainstate()
    current_price = int(chainstate.get("currentPrice", 0))

    if current_price <= 0:
        raise ValueError("Invalid current price from chainstate")

    # Calculate amount needed for duration (PLUR per chunk)
    amount_plur = calculate_stamp_amount(duration_hours, current_price)

    # Calculate total cost in PLUR (amount * 2^depth)
    total_cost_plur = calculate_stamp_total_cost(amount_plur, depth)

    # Convert to BZZ
    cost_bzz = plur_to_bzz(total_cost_plur)

    # Convert to USD
    exchange_rate = settings.X402_BZZ_USD_RATE
    cost_usd = bzz_to_usd(cost_bzz, exchange_rate)

    # Apply markup
    markup_percent = settings.X402_MARKUP_PERCENT
    price_with_markup = apply_markup(cost_usd, markup_percent)

    # Apply minimum price
    min_price = settings.X402_MIN_PRICE_USD
    final_price = apply_minimum_price(price_with_markup, min_price)
    minimum_applied = price_with_markup < min_price

    result = {
        "price_usd": round(final_price, 6),
        "price_bzz": round(cost_bzz, 8),
        "exchange_rate": exchange_rate,
        "markup_percent": markup_percent,
        "minimum_applied": minimum_applied,
    }

    if include_breakdown:
        result["breakdown"] = {
            "duration_hours": duration_hours,
            "depth": depth,
            "current_price_plur": current_price,
            "amount_plur_per_chunk": amount_plur,
            "total_cost_plur": total_cost_plur,
            "cost_bzz": round(cost_bzz, 8),
            "cost_usd_before_markup": round(cost_usd, 6),
            "cost_usd_with_markup": round(price_with_markup, 6),
            "minimum_price_usd": min_price,
            "final_price_usd": round(final_price, 6),
        }

    logger.info(
        f"Calculated stamp price: {duration_hours}h depth={depth} -> "
        f"{cost_bzz:.6f} BZZ = ${final_price:.4f} USD "
        f"(rate={exchange_rate}, markup={markup_percent}%)"
    )

    return result


def calculate_upload_price_usd(
    size_bytes: int,
    duration_hours: int = 24,
    include_breakdown: bool = True
) -> Dict[str, Any]:
    """
    Calculate the USD price for a data upload.

    This calculates the price for uploading data of a given size.
    The price is based on the stamp cost required to store the data.

    For uploads, we use a default depth based on the data size.
    Depth determines capacity: 2^depth chunks of 4096 bytes each.
    - depth 17 = 512 MB capacity
    - depth 20 = 4 GB capacity
    - depth 24 = 64 GB capacity

    Args:
        size_bytes: Size of data to upload in bytes
        duration_hours: How long to store the data (default 24 hours)
        include_breakdown: Whether to include detailed breakdown

    Returns:
        Dict containing price calculation details
    """
    # Calculate appropriate depth based on size
    # Each chunk is 4096 bytes, depth gives 2^depth chunks
    chunk_size = 4096
    chunks_needed = (size_bytes + chunk_size - 1) // chunk_size  # Ceiling division

    # Find minimum depth to fit the data
    # depth 17 = 2^17 = 131,072 chunks = 512 MB
    # We add some buffer for overhead
    min_depth = 17
    max_depth = 32

    depth = min_depth
    while depth < max_depth:
        capacity_chunks = 2 ** depth
        if capacity_chunks >= chunks_needed * 1.1:  # 10% buffer
            break
        depth += 1

    # Calculate stamp price for this depth and duration
    stamp_price = calculate_stamp_price_usd(
        duration_hours=duration_hours,
        depth=depth,
        include_breakdown=include_breakdown
    )

    result = {
        "price_usd": stamp_price["price_usd"],
        "price_bzz": stamp_price["price_bzz"],
        "exchange_rate": stamp_price["exchange_rate"],
        "markup_percent": stamp_price["markup_percent"],
        "minimum_applied": stamp_price["minimum_applied"],
    }

    if include_breakdown:
        result["breakdown"] = {
            "size_bytes": size_bytes,
            "chunks_needed": chunks_needed,
            "depth_used": depth,
            "capacity_chunks": 2 ** depth,
            "duration_hours": duration_hours,
            "stamp_breakdown": stamp_price.get("breakdown", {}),
        }

    logger.info(
        f"Calculated upload price: {size_bytes} bytes for {duration_hours}h -> "
        f"depth={depth}, ${stamp_price['price_usd']:.4f} USD"
    )

    return result


def get_price_quote(
    operation: str,
    **kwargs
) -> Dict[str, Any]:
    """
    Get a price quote for an x402 payment.

    This is the main entry point for generating x402 PaymentRequired responses.

    Args:
        operation: Type of operation ("stamp_purchase", "upload")
        **kwargs: Operation-specific parameters

    Returns:
        Dict containing:
        - price_usd: Final price
        - currency: "USDC"
        - network: Network identifier from config
        - pay_to: Payment address from config
        - expires_at: Quote expiration (optional)
        - details: Operation-specific details

    Raises:
        ValueError: If operation type is unknown
    """
    if operation == "stamp_purchase":
        duration_hours = kwargs.get("duration_hours", 24)
        depth = kwargs.get("depth", 17)
        price_info = calculate_stamp_price_usd(duration_hours, depth)
    elif operation == "upload":
        size_bytes = kwargs.get("size_bytes", 0)
        duration_hours = kwargs.get("duration_hours", 24)
        price_info = calculate_upload_price_usd(size_bytes, duration_hours)
    else:
        raise ValueError(f"Unknown operation type: {operation}")

    # Check if x402 is properly configured
    pay_to = settings.X402_PAY_TO_ADDRESS
    if not pay_to:
        logger.warning("X402_PAY_TO_ADDRESS not configured - using placeholder")
        pay_to = "0x0000000000000000000000000000000000000000"

    return {
        "price_usd": price_info["price_usd"],
        "currency": "USDC",
        "network": settings.X402_NETWORK,
        "pay_to": pay_to,
        "details": price_info,
    }
