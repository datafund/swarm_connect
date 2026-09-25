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
    # Bandwidth credit top-up is paid per-request. The chunk upload itself
    # (POST /api/v1/chunks/) is NOT listed here — it spends prepaid credit via a
    # bearer token rather than a per-request payment.
    ("POST", "/api/v1/chunks/credit"),
    # Pool acquire is priced here so a caller CAN pay for it, but the router
    # attaches settle_payment_if_offered rather than require_x402_payment:
    # payment is optional, and a caller presenting none falls through to the
    # daily per-origin allowance instead of receiving a 402.
    ("POST", "/api/v1/pool/acquire"),
]


def is_protected_endpoint(method: str, path: str) -> bool:
    """Check if the request matches a protected endpoint."""
    for protected_method, protected_path in PROTECTED_ENDPOINTS:
        if method == protected_method and path.rstrip("/").startswith(protected_path.rstrip("/")):
            return True
    return False


# Single implementation in app/core/client_ip. Re-exported here because
# callers and tests import it from this module. Both copies used to take the
# FIRST X-Forwarded-For entry, which is the one furthest from us and is
# caller-controlled if any proxy appends rather than replaces.
from app.core.client_ip import get_client_ip  # noqa: F401,E402


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


async def _spec_shaped_402(response: Response) -> Response:
    """Put an x402 Payment Required body at the top level, as x402 v1 specifies.

    The dependency raises HTTPException(402, detail=body), which FastAPI
    serialises as {"detail": body}. Standard x402 clients read x402Version,
    accepts and error from the top level and cannot pay a nested body (#372).
    The same body is kept under "detail" as well, so clients written against
    the old shape keep working; that copy is deprecated.
    """
    raw = b""
    async for chunk in response.body_iterator:
        raw += chunk
    try:
        detail = json.loads(raw).get("detail")
    except Exception:
        detail = None
    if isinstance(detail, dict) and "x402Version" in detail and "accepts" in detail:
        out = JSONResponse(status_code=402, content={**detail, "detail": detail})
    else:
        out = Response(content=raw, status_code=402, media_type=response.media_type)
    # Keep every original header (repeated ones included) except those that
    # describe the old body.
    out.raw_headers = [
        (k, v) for k, v in response.raw_headers if k.lower() not in (b"content-length", b"content-type")
    ] + [(k, v) for k, v in out.raw_headers if k.lower() in (b"content-length", b"content-type")]
    return out


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

    Also returns x402 Payment Required bodies at the top level, as x402 v1
    specifies (see _spec_shaped_402).

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
        try:
            response = await call_next(request)
        except Exception:
            if getattr(request.state, "x402_mode", None) != "paid":
                raise
            return self._paid_request_crashed(request)

        # Check what the dependency decided
        x402_mode = getattr(request.state, 'x402_mode', None)

        if x402_mode is None and response.status_code == 402:
            return await _spec_shaped_402(response)

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

        if x402_mode == "paid":
            return await self._finish_paid(request, response)

        return response

    async def _finish_paid(self, request: Request, response: Response) -> Response:
        """Settle what the handler did not, and never deliver an unpaid result.

        Paid handlers settle through settle_payment() before their irreversible
        step (settlement.py), so normally this only attaches the settlement
        headers. It settles here only for a paid route that has no settle point
        of its own, and then checks the result: SettleResponse reports failure
        with success=False rather than an exception (#355).
        """
        from app.x402.settlement import replay_guard
        from app.x402.audit import log_payment_failed, log_payment_settled

        settlement = getattr(request.state, "x402_settlement", None)
        ok = 200 <= response.status_code < 300
        payer = getattr(request.state, "x402_payer", None)
        client_ip = get_client_ip(request)

        if settlement is None and not ok:
            # Nothing delivered and nothing collected: let the same signed
            # authorization be retried.
            replay_guard.release(getattr(request.state, "x402_auth_key", None))
            if response.status_code == 402:
                return await _spec_shaped_402(response)
            return response

        if settlement is None:
            payment_payload = getattr(request.state, "x402_payment", None)
            payment_requirements = getattr(request.state, "x402_requirements", None)
            try:
                settlement = await self.facilitator_client.settle(
                    payment=payment_payload,
                    payment_requirements=payment_requirements,
                )
                failure = None if settlement.success else (settlement.error_reason or "unknown")
            except Exception as e:
                logger.error(f"x402: Payment settlement failed: {type(e).__name__}: {e}", exc_info=True)
                failure = f"{type(e).__name__}"
            if failure is not None:
                logger.error(f"x402: settlement failed after delivery on {request.url.path}: {failure}")
                log_payment_settled(client_ip=client_ip, payer=payer, transaction_hash=None,
                                    network=settings.X402_NETWORK, success=False, error_reason=failure)
                return JSONResponse(
                    status_code=402,
                    content={
                        "code": "PAYMENT_SETTLEMENT_FAILED",
                        "message": f"The payment could not be settled ({failure}).",
                        "x402_status": "settlement_failed",
                    },
                    headers={"X-Payment-Mode": "failed"},
                )
            log_payment_settled(client_ip=client_ip, payer=payer,
                                transaction_hash=getattr(settlement, "transaction", None),
                                network=settings.X402_NETWORK, success=True)

        tx_hash = getattr(settlement, "transaction", None) or "unknown"
        if not ok:
            # Paid, but the step after settlement failed. Record it for a
            # refund, and give the caller the transaction to cite.
            logger.error(f"x402: payment {tx_hash} settled but {request.url.path} "
                         f"returned {response.status_code}; refund needed")
            log_payment_failed(client_ip=client_ip,
                               reason=f"HTTP {response.status_code} after settlement; tx={tx_hash}",
                               stage="delivery_after_settlement", wallet_address=payer)

        body = b""
        async for chunk in response.body_iterator:
            body += chunk
        new_response = Response(
            content=body,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
        )
        new_response.headers[X_PAYMENT_RESPONSE_HEADER] = encode_payment_response(settlement)
        new_response.headers["X-Payment-Mode"] = "paid"
        new_response.headers["X-Payment-Transaction"] = tx_hash
        if not ok:
            new_response.headers["X-Payment-Status"] = "settled_not_delivered"
        return new_response

    def _paid_request_crashed(self, request: Request) -> Response:
        """A paid request raised instead of responding.

        Before settlement nothing was collected: release the authorization so it
        can be retried. After settlement the payer has paid for nothing: record
        it for a refund and return the transaction to cite.
        """
        from app.x402.settlement import replay_guard
        from app.x402.audit import log_payment_failed

        settlement = getattr(request.state, "x402_settlement", None)
        if settlement is None:
            replay_guard.release(getattr(request.state, "x402_auth_key", None))
            logger.error(f"x402: paid request to {request.url.path} failed before settlement", exc_info=True)
            return JSONResponse(status_code=500, content={"detail": "Internal server error. You were not charged."})

        tx_hash = getattr(settlement, "transaction", None) or "unknown"
        logger.error(f"x402: payment {tx_hash} settled but {request.url.path} raised; refund needed", exc_info=True)
        log_payment_failed(client_ip=get_client_ip(request),
                           reason=f"exception after settlement; tx={tx_hash}",
                           stage="delivery_after_settlement",
                           wallet_address=getattr(request.state, "x402_payer", None))
        return JSONResponse(
            status_code=500,
            content={
                "code": "DELIVERY_FAILED_AFTER_PAYMENT",
                "message": ("The payment was collected but the request failed before completing. "
                            "Contact the operator with this transaction for a refund."),
                "transaction": tx_hash,
                "x402_status": "settled_not_delivered",
            },
            headers={"X-Payment-Transaction": tx_hash, "X-Payment-Status": "settled_not_delivered"},
        )
