# app/x402/middleware.py
"""
FastAPI middleware for x402 payment verification.

This module provides HTTP middleware that:
1. Intercepts requests to protected endpoints
2. Checks if payment is required (X402_ENABLED)
3. Verifies X-PAYMENT header via facilitator
4. Settles payments via facilitator
5. Returns 402 Payment Required when needed

Uses the official x402 Python SDK for payment handling.
"""
import json
import logging
from typing import Callable, Optional, Tuple, List

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from x402.types import PaymentRequirements, PaymentPayload, VerifyResponse, SettleResponse
from x402.facilitator import FacilitatorClient
from x402.encoding import safe_base64_decode, safe_base64_encode

from app.core.config import settings
from app.x402.pricing import get_price_quote
from app.x402.ratelimit import check_rate_limit, get_rate_limit_headers

logger = logging.getLogger(__name__)

# x402 protocol constants
X402_VERSION = 1
X_PAYMENT_HEADER = "X-PAYMENT"
X_PAYMENT_RESPONSE_HEADER = "X-PAYMENT-RESPONSE"

# USDC contract addresses by network
USDC_ADDRESSES = {
    "base": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "base-sepolia": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
}

# Protected endpoints configuration
# These endpoints will require x402 payment when X402_ENABLED=true
PROTECTED_ENDPOINTS = [
    ("POST", "/api/v1/stamps/"),
    ("POST", "/api/v1/data/"),
    ("POST", "/api/v1/data/manifest"),
]


def is_protected_endpoint(method: str, path: str) -> bool:
    """Check if the request matches a protected endpoint."""
    for protected_method, protected_path in PROTECTED_ENDPOINTS:
        if method == protected_method and path.rstrip("/").startswith(protected_path.rstrip("/")):
            return True
    return False


def get_client_ip(request: Request) -> str:
    """Extract client IP from request, handling proxies."""
    # Check for forwarded headers first
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        # Take the first IP in the chain
        return forwarded_for.split(",")[0].strip()

    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()

    # Fall back to direct connection
    if request.client:
        return request.client.host

    return "unknown"


def create_payment_requirements(
    request: Request,
    price_usd: float,
    description: str = "Gateway operation"
) -> PaymentRequirements:
    """
    Create PaymentRequirements for x402 402 response.

    Args:
        request: The incoming request
        price_usd: Price in USD (will be converted to USDC smallest units)
        description: Description of the resource/operation

    Returns:
        PaymentRequirements object for the x402 response
    """
    network = settings.X402_NETWORK
    pay_to = settings.X402_PAY_TO_ADDRESS

    if not pay_to:
        logger.warning("X402_PAY_TO_ADDRESS not configured")
        pay_to = "0x0000000000000000000000000000000000000000"

    # USDC has 6 decimals, so $1.00 = 1,000,000 smallest units
    # Convert USD to USDC smallest units (string format for x402)
    amount_usdc = int(price_usd * 1_000_000)

    # Get USDC address for the configured network
    asset = USDC_ADDRESSES.get(network, USDC_ADDRESSES["base-sepolia"])

    # Build resource path
    resource = str(request.url)

    return PaymentRequirements(
        scheme="exact",
        network=network,
        max_amount_required=str(amount_usdc),
        resource=resource,
        description=description,
        mime_type="application/json",
        pay_to=pay_to,
        max_timeout_seconds=300,  # 5 minutes
        asset=asset,
        extra=None
    )


def create_402_response(
    payment_requirements: PaymentRequirements,
    error_message: str = "Payment required"
) -> JSONResponse:
    """
    Create an HTTP 402 Payment Required response.

    Args:
        payment_requirements: The payment requirements to include
        error_message: Error message for the response

    Returns:
        JSONResponse with 402 status and payment details
    """
    response_body = {
        "x402Version": X402_VERSION,
        "error": error_message,
        "accepts": [payment_requirements.model_dump(by_alias=True)]
    }

    return JSONResponse(
        status_code=402,
        content=response_body,
        headers={"Content-Type": "application/json"}
    )


def decode_payment_header(header_value: str) -> Optional[PaymentPayload]:
    """
    Decode the X-PAYMENT header into a PaymentPayload.

    Args:
        header_value: Base64-encoded payment payload

    Returns:
        PaymentPayload if successfully decoded, None otherwise
    """
    try:
        # Decode base64 - safe_base64_decode returns str, not bytes
        decoded_str = safe_base64_decode(header_value)
        if decoded_str is None:
            logger.warning("Failed to decode X-PAYMENT header: invalid base64")
            return None

        # Parse JSON
        payload_dict = json.loads(decoded_str)

        # Validate and create PaymentPayload
        return PaymentPayload.model_validate(payload_dict)

    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse X-PAYMENT header JSON: {e}")
        return None
    except Exception as e:
        logger.warning(f"Failed to decode X-PAYMENT header: {e}")
        return None


def encode_payment_response(settle_response: SettleResponse) -> str:
    """
    Encode a settlement response for the X-PAYMENT-RESPONSE header.

    Args:
        settle_response: The settlement response from the facilitator

    Returns:
        Base64-encoded JSON string
    """
    response_dict = settle_response.model_dump(by_alias=True)
    response_json = json.dumps(response_dict)
    return safe_base64_encode(response_json.encode("utf-8"))


class X402Middleware(BaseHTTPMiddleware):
    """
    x402 payment verification middleware for FastAPI.

    When X402_ENABLED=true, this middleware:
    - Checks if the endpoint requires payment
    - Verifies payment signatures on protected endpoints
    - Returns HTTP 402 with payment requirements if no valid payment
    - Settles payments via the configured facilitator

    When X402_ENABLED=false, all requests pass through unchanged.
    """

    def __init__(self, app, facilitator_client: Optional[FacilitatorClient] = None):
        super().__init__(app)
        self._facilitator_client = facilitator_client

    @property
    def facilitator_client(self) -> FacilitatorClient:
        """Lazy initialization of facilitator client."""
        if self._facilitator_client is None:
            self._facilitator_client = FacilitatorClient(
                base_url=settings.X402_FACILITATOR_URL
            )
        return self._facilitator_client

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Response]
    ) -> Response:
        """
        Process the request through x402 payment verification.

        Flow:
        1. Check if x402 is enabled
        2. Check if endpoint is protected
        3. If no X-PAYMENT header, return 402 with payment requirements
        4. If X-PAYMENT header present, verify with facilitator
        5. If valid, settle payment and process request
        6. Add X-PAYMENT-RESPONSE header to successful response
        """
        # Skip if x402 is disabled
        if not settings.X402_ENABLED:
            return await call_next(request)

        # Skip if not a protected endpoint
        if not is_protected_endpoint(request.method, request.url.path):
            return await call_next(request)

        client_ip = get_client_ip(request)
        logger.info(f"x402: Processing protected request from {client_ip}: {request.method} {request.url.path}")

        # Get the X-PAYMENT header
        payment_header = request.headers.get(X_PAYMENT_HEADER)

        # Calculate price for this operation
        try:
            price_quote = self._calculate_price_for_request(request)
            price_usd = price_quote["price_usd"]
            description = price_quote.get("description", "Gateway operation")
        except Exception as e:
            logger.error(f"x402: Failed to calculate price: {e}")
            return JSONResponse(
                status_code=503,
                content={"error": "Service temporarily unavailable", "detail": str(e)}
            )

        # Create payment requirements
        payment_requirements = create_payment_requirements(
            request=request,
            price_usd=price_usd,
            description=description
        )

        # If no payment header, check for free tier access
        if not payment_header:
            # Check if free tier is enabled
            if settings.X402_FREE_TIER_ENABLED:
                # Check free tier rate limit
                is_allowed, reason, stats = check_rate_limit(client_ip, is_free_tier=True)

                if is_allowed:
                    logger.info(f"x402: Free tier access granted for {client_ip} ({stats['requests_made']}/{stats['limit']} requests)")
                    # Process request with rate limit headers
                    response = await call_next(request)

                    # Add rate limit headers to response
                    body = b""
                    async for chunk in response.body_iterator:
                        body += chunk

                    new_response = Response(
                        content=body,
                        status_code=response.status_code,
                        headers=dict(response.headers),
                        media_type=response.media_type
                    )
                    for header, value in get_rate_limit_headers(stats).items():
                        new_response.headers[header] = value
                    new_response.headers["X-Payment-Mode"] = "free-tier"

                    return new_response
                else:
                    # Free tier rate limit exceeded - return 429
                    logger.warning(f"x402: Free tier rate limit exceeded for {client_ip}: {reason}")
                    return JSONResponse(
                        status_code=429,
                        content={
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

            # Free tier disabled - return 402
            logger.info(f"x402: No X-PAYMENT header, returning 402 for ${price_usd}")
            return create_402_response(
                payment_requirements=payment_requirements,
                error_message="X-PAYMENT header is required"
            )

        # Decode payment header
        payment_payload = decode_payment_header(payment_header)
        if payment_payload is None:
            logger.warning(f"x402: Invalid X-PAYMENT header from {client_ip}")
            return create_402_response(
                payment_requirements=payment_requirements,
                error_message="Invalid X-PAYMENT header format"
            )

        # Verify payment with facilitator
        try:
            verify_response = self.facilitator_client.verify(
                payment=payment_payload,
                payment_requirements=payment_requirements
            )
        except Exception as e:
            logger.error(f"x402: Facilitator verification failed: {e}")
            return JSONResponse(
                status_code=502,
                content={"error": "Payment verification failed", "detail": str(e)}
            )

        if not verify_response.is_valid:
            logger.warning(f"x402: Payment verification failed: {verify_response.invalid_reason}")
            return create_402_response(
                payment_requirements=payment_requirements,
                error_message=f"Payment verification failed: {verify_response.invalid_reason or 'Unknown reason'}"
            )

        logger.info(f"x402: Payment verified for payer {verify_response.payer}")

        # Process the request
        response = await call_next(request)

        # If request succeeded, settle the payment
        if 200 <= response.status_code < 300:
            try:
                settle_response = self.facilitator_client.settle(
                    payment=payment_payload,
                    payment_requirements=payment_requirements
                )

                logger.info(f"x402: Payment settled successfully")

                # Add settlement response header
                encoded_response = encode_payment_response(settle_response)

                # Create new response with header added
                # Note: We need to read the body and create a new response
                body = b""
                async for chunk in response.body_iterator:
                    body += chunk

                new_response = Response(
                    content=body,
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    media_type=response.media_type
                )
                new_response.headers[X_PAYMENT_RESPONSE_HEADER] = encoded_response

                return new_response

            except Exception as e:
                logger.error(f"x402: Payment settlement failed: {e}")
                # Request already succeeded, so return success but log the error
                # In production, you might want to handle this differently
                return response

        return response

    def _calculate_price_for_request(self, request: Request) -> dict:
        """
        Calculate price based on the request type.

        Returns:
            Dict with price_usd and description
        """
        path = request.url.path

        if "/stamps/" in path:
            # Stamp purchase - use default pricing for now
            # In full implementation, would parse request body for duration/depth
            quote = get_price_quote(
                operation="stamp_purchase",
                duration_hours=24,
                depth=17
            )
            return {
                "price_usd": quote["price_usd"],
                "description": "Postage stamp purchase (24h, depth 17)"
            }

        elif "/data/" in path:
            # Data upload - use default pricing for now
            # In full implementation, would use Content-Length header
            content_length = request.headers.get("Content-Length", "0")
            size_bytes = int(content_length) if content_length.isdigit() else 1024

            quote = get_price_quote(
                operation="upload",
                size_bytes=size_bytes,
                duration_hours=24
            )
            return {
                "price_usd": quote["price_usd"],
                "description": f"Data upload ({size_bytes} bytes, 24h)"
            }

        # Default pricing
        return {
            "price_usd": settings.X402_MIN_PRICE_USD,
            "description": "Gateway operation"
        }
