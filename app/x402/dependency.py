# app/x402/dependency.py
"""
FastAPI dependency for x402 payment verification.

This dependency runs AFTER FastAPI's request validation (Pydantic models,
path parameters, query parameters), ensuring that malformed requests
get proper 422 validation errors instead of generic 402 Payment Required.

The middleware (middleware.py) handles post-response processing only:
settlement and response headers.
"""
import json
import logging
from typing import Optional

from fastapi import HTTPException, Request

from x402.types import PaymentPayload
from x402.facilitator import FacilitatorClient, FacilitatorConfig

from app.core.config import settings
from app.services.metrics import x402_payments_total
from app.x402.pricing import get_price_quote
from app.x402.ratelimit import check_rate_limit, get_rate_limit_headers, get_free_tier_stats
from app.x402.base_balance import check_base_eth_balance
from app.x402.middleware import (
    is_protected_endpoint,
    get_client_ip,
    create_payment_requirements,
    decode_payment_header,
    X_PAYMENT_HEADER,
    X_PAYMENT_MODE_HEADER,
    X402_VERSION,
)

logger = logging.getLogger(__name__)

# Lazy-initialized facilitator client
_facilitator_client: Optional[FacilitatorClient] = None


def _get_facilitator_client() -> FacilitatorClient:
    """Get or create the facilitator client singleton."""
    global _facilitator_client
    if _facilitator_client is None:
        config: FacilitatorConfig = {"url": settings.X402_FACILITATOR_URL}
        _facilitator_client = FacilitatorClient(config=config)
    return _facilitator_client


async def _calculate_price_for_request(request: Request) -> dict:
    """
    Calculate price based on the request type.

    Returns:
        Dict with price_usd and description
    """
    path = request.url.path

    if "/stamps/" in path:
        quote = await get_price_quote(
            operation="stamp_purchase",
            duration_hours=24,
            depth=17
        )
        return {
            "price_usd": quote["price_usd"],
            "description": "Postage stamp purchase (24h, depth 17)"
        }

    elif "/data/" in path:
        content_length = request.headers.get("Content-Length", "0")
        size_bytes = int(content_length) if content_length.isdigit() else 1024

        quote = await get_price_quote(
            operation="upload",
            size_bytes=size_bytes,
            duration_hours=24
        )
        return {
            "price_usd": quote["price_usd"],
            "description": f"Data upload ({size_bytes} bytes, 24h)"
        }

    return {
        "price_usd": settings.X402_MIN_PRICE_USD,
        "description": "Gateway operation"
    }


async def require_x402_payment(request: Request) -> None:
    """
    FastAPI dependency that enforces x402 payment on protected endpoints.

    Runs AFTER FastAPI routing and Pydantic validation, so invalid requests
    get 422 errors before payment is checked.

    Stores payment state on request.state for the middleware to use:
    - request.state.x402_mode: "free-tier" | "paid" | None
    - request.state.x402_payment: PaymentPayload (for paid mode)
    - request.state.x402_requirements: PaymentRequirements (for paid mode)
    """
    # Skip if x402 is disabled
    if not settings.X402_ENABLED:
        return

    # Skip if not a protected endpoint (e.g. GET requests on these routers)
    if not is_protected_endpoint(request.method, request.url.path):
        return

    # Check gateway ETH balance
    base_balance = await check_base_eth_balance()
    if base_balance.get("is_critical"):
        logger.error(
            f"x402: Gateway ETH critically low ({base_balance.get('balance_eth', 0):.6f} ETH). "
            f"Cannot process payments."
        )
        raise HTTPException(
            status_code=503,
            detail={
                "error": "Gateway temporarily unavailable",
                "detail": "Gateway wallet has insufficient ETH for gas. Please try again later.",
                "x402_status": "critical",
                "balance_eth": base_balance.get("balance_eth", 0),
            }
        )

    client_ip = get_client_ip(request)
    logger.info(f"x402: Processing protected request from {client_ip}: {request.method} {request.url.path}")

    # Calculate price for this operation
    try:
        price_quote = await _calculate_price_for_request(request)
        price_usd = price_quote["price_usd"]
        description = price_quote.get("description", "Gateway operation")
    except Exception as e:
        logger.error(f"x402: Failed to calculate price: {e}")
        raise HTTPException(
            status_code=503,
            detail={
                "error": "Service temporarily unavailable",
                "detail": "Failed to calculate price for this operation."
            }
        )

    # Create payment requirements
    payment_requirements = create_payment_requirements(
        request=request,
        price_usd=price_usd,
        description=description
    )

    # Get X-PAYMENT header and payment mode
    payment_header = request.headers.get(X_PAYMENT_HEADER)
    payment_mode = request.headers.get(X_PAYMENT_MODE_HEADER, "").lower()

    # If no payment header AND no free tier opt-in, return 402
    if not payment_header and payment_mode != "free":
        free_tier_info = get_free_tier_stats(client_ip) if settings.X402_FREE_TIER_ENABLED else None

        logger.info(f"x402: No payment header, returning 402 for ${price_usd}")
        x402_payments_total.labels(mode="rejected").inc()

        response_body = {
            "x402Version": X402_VERSION,
            "error": "Payment required. Use X-PAYMENT header for paid access or X-Payment-Mode: free for free tier.",
            "accepts": [payment_requirements.model_dump(by_alias=True)]
        }
        if free_tier_info and free_tier_info.get("available"):
            response_body["freeTier"] = free_tier_info

        raise HTTPException(status_code=402, detail=response_body)

    # Handle free tier opt-in
    if payment_mode == "free":
        if not settings.X402_FREE_TIER_ENABLED:
            logger.warning(f"x402: Free tier requested but disabled for {client_ip}")
            response_body = {
                "x402Version": X402_VERSION,
                "error": "Free tier is not available. Payment required.",
                "accepts": [payment_requirements.model_dump(by_alias=True)]
            }
            raise HTTPException(status_code=402, detail=response_body)

        # Check free tier rate limit
        is_allowed, reason, stats = check_rate_limit(client_ip, is_free_tier=True)

        if is_allowed:
            logger.info(f"x402: Free tier access granted for {client_ip} ({stats['requests_made']}/{stats['limit']} requests)")
            x402_payments_total.labels(mode="free").inc()
            request.state.x402_mode = "free-tier"
            request.state.x402_payer = None
            request.state.x402_rate_limit_stats = stats
            return
        else:
            # Free tier rate limit exceeded
            logger.warning(f"x402: Free tier rate limit exceeded for {client_ip}: {reason}")
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "Rate limit exceeded",
                    "detail": reason,
                    "message": "Free tier rate limit exceeded. Use x402 payment for higher limits.",
                    "payment_info": {
                        "price_usd": price_usd,
                        "network": settings.X402_NETWORK,
                        "pay_to": settings.X402_PAY_TO_ADDRESS,
                    }
                },
                headers=get_rate_limit_headers(stats)
            )

    # At this point, we have an X-PAYMENT header - verify payment
    payment_payload = decode_payment_header(payment_header)
    if payment_payload is None:
        logger.warning(f"x402: Invalid X-PAYMENT header from {client_ip}")
        response_body = {
            "x402Version": X402_VERSION,
            "error": "Invalid X-PAYMENT header format",
            "accepts": [payment_requirements.model_dump(by_alias=True)]
        }
        raise HTTPException(status_code=402, detail=response_body)

    # Verify payment with facilitator
    try:
        facilitator = _get_facilitator_client()
        logger.debug(f"x402: Calling facilitator verify at {settings.X402_FACILITATOR_URL}")
        verify_response = await facilitator.verify(
            payment=payment_payload,
            payment_requirements=payment_requirements
        )
        logger.debug(f"x402: Verify response: is_valid={verify_response.is_valid}, payer={getattr(verify_response, 'payer', 'unknown')}")
    except Exception as e:
        logger.error(f"x402: Facilitator verification failed: {type(e).__name__}: {e}", exc_info=True)
        raise HTTPException(
            status_code=502,
            detail={
                "error": "Payment verification failed",
                "detail": "Could not verify payment with facilitator. Please try again.",
            }
        )

    if not verify_response.is_valid:
        invalid_reason = getattr(verify_response, 'invalid_reason', None) or 'Unknown reason'
        logger.warning(f"x402: Payment verification failed: {invalid_reason}")
        response_body = {
            "x402Version": X402_VERSION,
            "error": f"Payment verification failed: {invalid_reason}",
            "accepts": [payment_requirements.model_dump(by_alias=True)]
        }
        raise HTTPException(status_code=402, detail=response_body)

    logger.info(f"x402: Payment verified for payer {verify_response.payer}")
    x402_payments_total.labels(mode="paid").inc()

    # Store payment info on request.state for middleware settlement
    request.state.x402_mode = "paid"
    request.state.x402_payer = getattr(verify_response, 'payer', None)
    request.state.x402_payment = payment_payload
    request.state.x402_requirements = payment_requirements
