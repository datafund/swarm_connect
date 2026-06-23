# app/api/endpoints/chunks.py
import logging
import re
from typing import Optional

import httpx
from fastapi import APIRouter, Header, HTTPException, Query, Request, status

from app.api.models.chunk import ChunkUploadResponse, CreditTopUpResponse
from app.core.config import settings
from app.services.bandwidth_credit import bandwidth_credit_manager
from app.services.bandwidth_free_tier import free_tier_tracker
from app.services.swarm_api import upload_chunk_to_swarm
from app.x402.audit import AuditEventType, log_audit_event
from app.x402.middleware import get_client_ip

logger = logging.getLogger(__name__)
router = APIRouter()

# A marshaled postage stamp is 113 bytes = 226 hex characters:
#   batchID[0:32] + index[32:40] + timestamp[40:48] + signature[48:113]
MARSHALED_STAMP_HEX_LEN = 226
_HEX_RE = re.compile(r"^[A-Fa-f0-9]+$")

# Header the client uses to present its prepaid bandwidth credit token.
CREDIT_TOKEN_HEADER = "X-Bandwidth-Credit-Token"

# 1 MB = 10^6 bytes (consistent with the per-GB bandwidth pricing).
BYTES_PER_MB = 1_000_000


def _validate_marshaled_stamp(stamp: str) -> str:
    """Validate and normalize the client-supplied marshaled stamp.

    Accepts an optional 0x prefix. Raises HTTP 400 if the value is not
    well-formed hex of the expected length. Returns the bare hex string.
    """
    s = stamp[2:] if stamp[:2].lower() == "0x" else stamp
    if not _HEX_RE.match(s) or len(s) != MARSHALED_STAMP_HEX_LEN:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "INVALID_STAMP",
                "message": (
                    "Swarm-Postage-Stamp must be a hex-encoded 113-byte marshaled stamp "
                    f"({MARSHALED_STAMP_HEX_LEN} hex characters)."
                ),
            },
        )
    return s


def _topup_info() -> dict:
    """Payment guidance returned in 402 responses for the credit flow."""
    return {
        "topup_endpoint": "POST /api/v1/chunks/credit",
        "min_topup_mb": settings.BANDWIDTH_CREDIT_MIN_TOPUP_MB,
        "price_usd_per_gb": settings.X402_BANDWIDTH_USD_PER_GB,
        "credit_token_header": CREDIT_TOKEN_HEADER,
    }


@router.post(
    "/credit",
    response_model=CreditTopUpResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Top up prepaid bandwidth credit (x402)",
)
async def top_up_credit(
    request: Request,
    mb: int = Query(..., ge=1, description="Amount of bandwidth credit to add, in MB (1 MB = 10^6 bytes)."),
) -> CreditTopUpResponse:
    """
    Add prepaid bandwidth credit with a single x402 payment.

    The payment is priced from the requested `mb` at `X402_BANDWIDTH_USD_PER_GB`. The
    credit is bound to the verified x402 payer wallet, and the response returns a bearer
    token to present (via the `X-Bandwidth-Credit-Token` header) on chunk uploads. One
    top-up funds many uploads, so per-chunk requests never hit the minimum-price floor.
    """
    if not settings.CHUNK_UPLOAD_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Chunk upload is not enabled on this gateway.")

    if not settings.X402_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "BILLING_DISABLED",
                "message": "Bandwidth credit top-up requires x402 billing, which is disabled on this gateway.",
            },
        )

    min_mb = settings.BANDWIDTH_CREDIT_MIN_TOPUP_MB
    if mb < min_mb:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "TOPUP_TOO_SMALL",
                "message": f"Minimum top-up is {min_mb} MB.",
                "min_topup_mb": min_mb,
            },
        )

    # The x402 dependency must have settled a paid request for this to be a real top-up.
    x402_mode = getattr(request.state, "x402_mode", None)
    payer = getattr(request.state, "x402_payer", None)
    if x402_mode != "paid" or not payer:
        raise HTTPException(
            status_code=402,
            detail={
                "code": "PAYMENT_REQUIRED",
                "message": "Bandwidth credit top-up requires a paid x402 payment (free tier cannot fund credit).",
            },
        )

    credited_bytes = mb * BYTES_PER_MB
    new_balance = bandwidth_credit_manager.credit(payer, credited_bytes)
    token = bandwidth_credit_manager.issue_token(payer)

    log_audit_event(
        event_type=AuditEventType.CREDIT_TOPPED_UP,
        data={"credited_bytes": credited_bytes, "balance_bytes": new_balance, "mb": mb},
        wallet_address=payer,
    )

    return CreditTopUpResponse(
        address=payer,
        token=token,
        credited_bytes=credited_bytes,
        balance_bytes=new_balance,
    )


@router.post(
    "/",
    response_model=ChunkUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Forward a pre-stamped chunk to Swarm",
)
async def upload_chunk(
    request: Request,
    deferred: bool = Query(
        default=False,
        description="Deferred upload (local first, async sync). Default false = direct upload.",
    ),
    swarm_postage_stamp: Optional[str] = Header(
        default=None,
        alias="Swarm-Postage-Stamp",
        description="Hex-encoded 113-byte marshaled postage stamp signed by the chunk's batch owner.",
    ),
    credit_token: Optional[str] = Header(
        default=None,
        alias=CREDIT_TOKEN_HEADER,
        description="Bearer bandwidth-credit token from POST /api/v1/chunks/credit (required when billing is on).",
    ),
) -> ChunkUploadResponse:
    """
    Forward a single client-supplied **pre-stamped** chunk to the Swarm network.

    The client owns the postage batch and stamps the chunk locally; the gateway is a
    thin forwarder. Send the raw chunk as the request body (`application/octet-stream`)
    and the marshaled stamp in the `Swarm-Postage-Stamp` header. The gateway does NOT
    verify the stamp signature — the Bee node does.

    When x402 billing is enabled, the upload spends prepaid bandwidth credit: present the
    bearer token from `POST /api/v1/chunks/credit` in the `X-Bandwidth-Credit-Token`
    header. The chunk's byte length is debited from the token's balance.
    """
    # Feature toggle: behave as if the route does not exist when disabled.
    if not settings.CHUNK_UPLOAD_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Chunk upload is not enabled on this gateway.",
        )

    if not swarm_postage_stamp:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "MISSING_STAMP",
                "message": "The Swarm-Postage-Stamp header is required for chunk uploads.",
            },
        )

    stamp = _validate_marshaled_stamp(swarm_postage_stamp)

    chunk_bytes = await request.body()
    if not chunk_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "EMPTY_CHUNK", "message": "Request body (the chunk) must not be empty."},
        )

    max_bytes = settings.CHUNK_UPLOAD_MAX_BYTES_PER_REQUEST
    if len(chunk_bytes) > max_bytes:
        raise HTTPException(
            status_code=413,  # Content Too Large
            detail={
                "code": "CHUNK_TOO_LARGE",
                "message": f"Chunk exceeds the maximum size of {max_bytes} bytes.",
                "max_bytes": max_bytes,
            },
        )

    chunk_len = len(chunk_bytes)

    # --- Billing (when x402 is enabled) ---
    # The chunk upload carries no per-request x402 payment. Two paths:
    #   free  (X-Payment-Mode: free) -> debit a per-IP daily byte quota
    #   paid  (default)              -> debit prepaid credit via the bearer token
    billing_address: Optional[str] = None  # paid (credit) path
    free_ip: Optional[str] = None          # free-tier path
    bytes_charged: Optional[int] = None
    credit_balance: Optional[int] = None

    if settings.X402_ENABLED:
        payment_mode = request.headers.get("X-Payment-Mode", "").lower()

        if payment_mode == "free":
            if not settings.CHUNK_UPLOAD_FREE_TIER_ENABLED:
                raise HTTPException(
                    status_code=402,
                    detail={
                        "code": "FREE_TIER_DISABLED",
                        "message": "Free-tier chunk uploads are disabled. Top up bandwidth credit instead.",
                        "payment_info": _topup_info(),
                    },
                )
            free_ip = get_client_ip(request)
            daily_limit = settings.CHUNK_UPLOAD_FREE_TIER_MB_PER_DAY * BYTES_PER_MB
            allowed, remaining = free_tier_tracker.try_consume(free_ip, chunk_len, daily_limit)
            if not allowed:
                raise HTTPException(
                    status_code=429,
                    detail={
                        "code": "FREE_QUOTA_EXCEEDED",
                        "message": (
                            f"Free-tier daily quota exhausted ({remaining} bytes left, need {chunk_len}). "
                            "Top up bandwidth credit for higher limits."
                        ),
                        "remaining_bytes": remaining,
                        "payment_info": _topup_info(),
                    },
                )
            bytes_charged = chunk_len
        else:
            if not credit_token:
                raise HTTPException(
                    status_code=402,
                    detail={
                        "code": "CREDIT_REQUIRED",
                        "message": "A bandwidth credit token is required. Top up first, or use X-Payment-Mode: free.",
                        "payment_info": _topup_info(),
                    },
                )
            billing_address = bandwidth_credit_manager.resolve_token(credit_token)
            if not billing_address:
                raise HTTPException(
                    status_code=402,
                    detail={
                        "code": "INVALID_CREDIT_TOKEN",
                        "message": "The bandwidth credit token is unknown. Top up to obtain a valid token.",
                        "payment_info": _topup_info(),
                    },
                )
            ok, remaining = bandwidth_credit_manager.debit(billing_address, chunk_len)
            if not ok:
                raise HTTPException(
                    status_code=402,
                    detail={
                        "code": "INSUFFICIENT_CREDIT",
                        "message": f"Insufficient bandwidth credit ({remaining} bytes left, need {chunk_len}).",
                        "balance_bytes": remaining,
                        "payment_info": _topup_info(),
                    },
                )
            bytes_charged = chunk_len
            credit_balance = remaining

    # --- Forward the chunk to Bee. Refund the debit if forwarding fails. ---
    try:
        reference = await upload_chunk_to_swarm(chunk_bytes, stamp, deferred=deferred)
    except (httpx.HTTPError, ValueError) as e:
        # Refund: the upload never landed, so the client shouldn't be charged.
        if billing_address is not None:
            bandwidth_credit_manager.credit(billing_address, chunk_len)
        if free_ip is not None:
            free_tier_tracker.refund(free_ip, chunk_len)
        if isinstance(e, httpx.HTTPError):
            logger.error(f"Failed to forward chunk to Swarm API: {e}")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Could not forward the chunk. The Bee node may be unavailable or the stamp invalid.",
            )
        logger.error(f"Invalid response from Swarm API during chunk upload: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Invalid response from Swarm API. Please try again.",
        )

    if billing_address is not None:
        log_audit_event(
            event_type=AuditEventType.CREDIT_DEBITED,
            data={"bytes": bytes_charged, "balance_bytes": credit_balance, "reference": reference},
            wallet_address=billing_address,
        )

    return ChunkUploadResponse(
        reference=reference,
        bytes_charged=bytes_charged,
        credit_balance_bytes=credit_balance,
    )
