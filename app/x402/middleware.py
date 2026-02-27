# app/x402/middleware.py
"""
FastAPI middleware for x402 post-response processing.

This module provides HTTP middleware that handles post-response work:
1. Settles payments via facilitator after successful responses
2. Adds X-PAYMENT-RESPONSE and rate limit headers

Pre-request payment verification is handled by the dependency
(app/x402/dependency.py) which runs after FastAPI validation,
ensuring malformed requests get 422 errors instead of 402.

Uses the official x402 Python SDK for payment handling.
"""
import json
import logging
from typing import Callable, Optional

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from x402.types import PaymentRequirements, PaymentPayload, SettleResponse
from x402.facilitator import FacilitatorClient, FacilitatorConfig
from x402.encoding import safe_base64_decode, safe_base64_encode

from app.core.config import settings
from app.x402.ratelimit import get_rate_limit_headers

logger = logging.getLogger(__name__)

# x402 protocol constants
X402_VERSION = 1
X_PAYMENT_HEADER = "X-PAYMENT"
X_PAYMENT_RESPONSE_HEADER = "X-PAYMENT-RESPONSE"
X_PAYMENT_MODE_HEADER = "X-Payment-Mode"

# USDC contract addresses by network
USDC_ADDRESSES = {
    "base": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "base-sepolia": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
}

# USDC token metadata for EIP-3009 signing
# The "name" and "version" fields are required for EIP-712 domain separator
# IMPORTANT: The name MUST match the on-chain DOMAIN_SEPARATOR exactly
# Circle's USDC uses "USDC" (not "USD Coin") in the EIP-712 domain
USDC_TOKEN_METADATA = {
    "base": {
        "name": "USDC",  # Matches on-chain EIP-712 domain
        "version": "2",
    },
    "base-sepolia": {
        "name": "USDC",  # Matches on-chain EIP-712 domain
        "version": "2",
    },
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

    # Get USDC token metadata for EIP-712 domain separator
    # This is required for clients to construct proper EIP-3009 signatures
    token_metadata = USDC_TOKEN_METADATA.get(network, USDC_TOKEN_METADATA["base-sepolia"])

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
        extra=token_metadata
    )


def create_402_response(
    payment_requirements: PaymentRequirements,
    error_message: str = "Payment required",
    free_tier_info: Optional[dict] = None
) -> JSONResponse:
    """
    Create an HTTP 402 Payment Required response.

    Args:
        payment_requirements: The payment requirements to include
        error_message: Error message for the response
        free_tier_info: Optional free tier information to include

    Returns:
        JSONResponse with 402 status and payment details
    """
    response_body = {
        "x402Version": X402_VERSION,
        "error": error_message,
        "accepts": [payment_requirements.model_dump(by_alias=True)]
    }

    # Include free tier info if available
    if free_tier_info and free_tier_info.get("available"):
        response_body["freeTier"] = free_tier_info

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
    x402 post-response middleware for FastAPI.

    Handles post-response processing only:
    - Settles payments via facilitator after successful responses
    - Adds X-PAYMENT-RESPONSE header with settlement proof
    - Adds rate limit headers for free-tier requests

    Pre-request payment verification is handled by the x402 dependency
    (app/x402/dependency.py), which stores state on request.state:
    - request.state.x402_mode: "free-tier" | "paid"
    - request.state.x402_payment: PaymentPayload (paid mode)
    - request.state.x402_requirements: PaymentRequirements (paid mode)
    - request.state.x402_rate_limit_stats: dict (free-tier mode)

    When X402_ENABLED=false, all requests pass through unchanged.
    """

    def __init__(self, app, facilitator_client: Optional[FacilitatorClient] = None):
        super().__init__(app)
        self._facilitator_client = facilitator_client

    @property
    def facilitator_client(self) -> FacilitatorClient:
        """Lazy initialization of facilitator client."""
        if self._facilitator_client is None:
            config: FacilitatorConfig = {"url": settings.X402_FACILITATOR_URL}
            self._facilitator_client = FacilitatorClient(config=config)
        return self._facilitator_client

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Response]
    ) -> Response:
        """
        Post-response processing for x402 payments.

        Flow:
        1. Pass request through to FastAPI (validation + dependency + endpoint)
        2. Check request.state for x402 mode set by the dependency
        3. For free-tier: add rate limit headers
        4. For paid: settle payment and add X-PAYMENT-RESPONSE header
        """
        # Skip if x402 is disabled
        if not settings.X402_ENABLED:
            return await call_next(request)

        # Let the request through — the dependency handles pre-request checks
        response = await call_next(request)

        # Check what the dependency decided
        x402_mode = getattr(request.state, 'x402_mode', None)

        if x402_mode == "free-tier":
            # Add rate limit headers for free-tier responses
            stats = getattr(request.state, 'x402_rate_limit_stats', {})

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

        if x402_mode == "paid" and 200 <= response.status_code < 300:
            # Settle payment and add response headers
            payment_payload = getattr(request.state, 'x402_payment', None)
            payment_requirements = getattr(request.state, 'x402_requirements', None)

            if payment_payload and payment_requirements:
                try:
                    logger.debug(f"x402: Calling facilitator settle at {settings.X402_FACILITATOR_URL}")
                    settle_response = await self.facilitator_client.settle(
                        payment=payment_payload,
                        payment_requirements=payment_requirements
                    )

                    tx_hash = getattr(settle_response, 'transaction_hash', 'unknown')
                    logger.info(f"x402: Payment settled successfully, tx_hash={tx_hash}")

                    # Add settlement response header
                    encoded_response = encode_payment_response(settle_response)

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
                    new_response.headers["X-Payment-Mode"] = "paid"
                    new_response.headers["X-Payment-Transaction"] = tx_hash

                    return new_response

                except Exception as e:
                    logger.error(f"x402: Payment settlement failed: {type(e).__name__}: {e}", exc_info=True)
                    return JSONResponse(
                        status_code=500,
                        content={
                            "error": "Payment settlement failed",
                            "detail": "Your request was processed but payment settlement failed. Please retry.",
                            "x402_status": "settlement_failed",
                            "message": "Please retry with a new payment or use X-Payment-Mode: free for free tier access"
                        },
                        headers={"X-Payment-Mode": "failed"}
                    )

        return response
