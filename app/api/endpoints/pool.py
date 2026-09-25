# app/api/endpoints/pool.py
"""
API endpoints for the Stamp Pool feature.

Provides endpoints for:
- Getting pool status
- Acquiring/releasing stamps from the pool
- Manual pool maintenance
"""
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from typing import Dict, List, Optional, Literal
from datetime import datetime, timezone
import logging

from app.core.config import settings
from app.services.stamp_pool import stamp_pool_manager, PoolStampStatus
from app.services.stamp_ownership import stamp_ownership_manager
from app.services.metrics import pool_acquires_total
from app.services.pool_allowance import pool_allowance_tracker
from app.core.client_ip import get_client_ip
from app.services.signed_auth import POOL_CHECK_PREFIX, authorize_signed_request
from app.api.models.stamp import SIZE_PRESETS

router = APIRouter()
logger = logging.getLogger(__name__)


# === Response Models ===

class PoolStatusResponse(BaseModel):
    """Response model for pool status endpoint."""
    enabled: bool = Field(..., description="Whether stamp pool feature is enabled")
    reserve_config: Dict[int, int] = Field(..., description="Target reserve levels by depth")
    current_levels: Dict[int, int] = Field(..., description="Current stamp counts by depth")
    available_stamps: Dict[int, List[str]] = Field(..., description="Available batch IDs by depth")
    total_stamps: int = Field(..., description="Total available stamps in pool")
    low_reserve_warning: bool = Field(..., description="True if any depth is below threshold")
    last_check: Optional[str] = Field(None, description="Last maintenance check timestamp (ISO format)")
    next_check: Optional[str] = Field(None, description="Next scheduled check timestamp (ISO format)")
    errors: List[str] = Field(default_factory=list, description="Recent errors")

    class Config:
        json_schema_extra = {
            "example": {
                "enabled": True,
                "reserve_config": {17: 1, 20: 1},
                "current_levels": {17: 1, 20: 0},
                "available_stamps": {17: ["abc123..."]},
                "total_stamps": 1,
                "low_reserve_warning": True,
                "last_check": "2026-01-21T10:00:00Z",
                "next_check": "2026-01-21T10:15:00Z",
                "errors": []
            }
        }


class PoolStampInfo(BaseModel):
    """Information about a stamp from the pool."""
    batch_id: str = Field(..., description="The batch ID of the stamp")
    depth: int = Field(..., description="Stamp depth (17=small, 20=medium, 22=large)")
    size_name: str = Field(..., description="Human-friendly size name")
    created_at: str = Field(..., description="When stamp was added to pool (ISO format)")
    ttl_at_creation: int = Field(..., description="TTL when added to pool (seconds)")


class AcquireStampRequest(BaseModel):
    """Request to acquire a stamp from the pool."""
    size: Optional[Literal["small", "medium", "large"]] = Field(
        None,
        description=("Preferred stamp size. If it is not available, a free acquire may be served a "
                     "larger size (charged to that size's allowance); a paid acquire gets exactly the "
                     "size paid for, or 409.")
    )
    depth: Optional[int] = Field(
        None,
        description="Specific depth requested (overrides size): 17 (small), 20 (medium) or 22 (large)."
    )

    @field_validator("depth")
    @classmethod
    def _pool_depth(cls, v):
        # The pool only holds the preset sizes. Any other depth used to be
        # accepted, got its own daily-allowance bucket, and was then served
        # whatever larger batch was available (#351).
        if v is not None and v not in SIZE_PRESETS.values():
            raise ValueError("depth must be one of 17 (small), 20 (medium) or 22 (large)")
        return v

    def requested_depth(self) -> int:
        """The depth this request asks for: explicit depth, else size, else small.

        Shared by the handler and the x402 pricer so the price and the batch
        are derived from the same parse (#362).
        """
        if self.depth is not None:
            return self.depth
        if self.size is not None:
            return SIZE_PRESETS[self.size]
        return SIZE_PRESETS["small"]

    class Config:
        json_schema_extra = {
            "example": {
                "size": "small"
            }
        }


class AcquireStampResponse(BaseModel):
    """Response when acquiring a stamp from the pool."""
    success: bool = Field(..., description="Whether a stamp was acquired")
    batch_id: Optional[str] = Field(None, description="The batch ID of the acquired stamp")
    depth: Optional[int] = Field(None, description="Depth of the acquired stamp")
    size_name: Optional[str] = Field(None, description="Human-friendly size name")
    message: str = Field(..., description="Status message")
    fallback_used: bool = Field(False, description="True if a larger stamp was provided")

    class Config:
        json_schema_extra = {
            "example": {
                "success": True,
                "batch_id": "abc123def456...",
                "depth": 17,
                "size_name": "small",
                "message": "Stamp acquired from pool",
                "fallback_used": False
            }
        }


class ManualCheckAcceptedResponse(BaseModel):
    """Acknowledgement that maintenance was scheduled.

    Deliberately carries no results. The check purchases stamps and can take
    well over a minute, and awaiting it was half of #292 — a caller could hold a
    worker for the duration. `GET /api/v1/pool/status` reports the outcome.
    """
    scheduled_at: str = Field(..., description="When the check was scheduled (ISO 8601)")
    message: str = Field(..., description="Where to look for the result")


# === Helper Functions ===

def depth_to_size_name(depth: int) -> str:
    """Convert depth to human-friendly size name."""
    for name, d in SIZE_PRESETS.items():
        if d == depth:
            return name
    return f"depth-{depth}"


# === Endpoints ===

@router.get(
    "/status",
    response_model=PoolStatusResponse,
    summary="Get Pool Status",
    description="Get current status of the stamp pool including reserve levels and available stamps."
)
async def get_pool_status():
    """Get current stamp pool status."""
    status = stamp_pool_manager.get_status()

    return PoolStatusResponse(
        enabled=status.enabled,
        reserve_config=status.reserve_config,
        current_levels=status.current_levels,
        available_stamps=status.available_stamps,
        total_stamps=status.total_stamps,
        low_reserve_warning=status.low_reserve_warning,
        last_check=status.last_check.isoformat() if status.last_check else None,
        next_check=status.next_check.isoformat() if status.next_check else None,
        errors=status.errors
    )


async def _refuse_allowance(http_request: Request, message: str, detail: dict) -> None:
    """Refuse an acquire whose free allowance is spent, saying what still works.

    The offer to pay is conditional. Where a settled payment does not buy a
    bypass — a testnet, without the explicit override — telling the caller to
    pay would send them to a path that takes their payment and still refuses
    them, which is worse than not offering it at all.
    """
    detail = dict(detail)
    if settings.paid_bypass_is_honoured():
        message += (
            "To continue now, pay with x402: send an X-PAYMENT header with "
            "this same request and you get a pooled stamp immediately, "
            "without drawing on the allowance."
        )
        detail["alternative"] = {
            "endpoint": "POST /api/v1/pool/acquire",
            "payment": "x402",
            "header": "X-PAYMENT",
            "note": "Paid acquires bypass the allowance and are immediate.",
        }
    else:
        message += (
            "To continue now, buy a stamp directly with POST /api/v1/stamps/ — "
            "that is not drawn from the pool, so this limit does not apply. "
            "It takes about a minute to become usable rather than seconds."
        )
        detail["alternative"] = {
            "endpoint": "POST /api/v1/stamps/",
            "note": "Direct purchase is not drawn from the pool, so the allowance does not apply.",
        }
    detail["message"] = message

    if settings.X402_ENABLED and settings.paid_bypass_is_honoured():
        # Paying bypasses the allowance here, so answer the way x402 clients
        # understand: 402 with the price, not a 429 they can only report as a
        # rate limit (#374). If the price cannot be worked out right now, the
        # 429 below still tells the caller what happened and what to do.
        try:
            from app.x402.dependency import _calculate_price_for_request
            from app.x402.middleware import X402_VERSION, create_payment_requirements
            quote = await _calculate_price_for_request(http_request)
            requirements = create_payment_requirements(
                http_request, quote["price_usd"], quote.get("description", "Pooled stamp"))
            payment_required = {
                "x402Version": X402_VERSION,
                "error": message,
                "accepts": [requirements.model_dump(by_alias=True)],
                **detail,
            }
        except Exception as e:
            logger.warning("Could not price the allowance refusal, answering 429: %s", e)
        else:
            raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=payment_required)

    raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=detail)


@router.post(
    "/acquire",
    response_model=AcquireStampResponse,
    summary="Acquire Stamp from Pool",
    description=(
        "Acquire a stamp from the pool for immediate use. "
        "This is much faster than purchasing a new stamp (~5 seconds vs >1 minute). "
        "The stamp is released from the pool and becomes the caller's responsibility."
    )
)
async def acquire_stamp(
    request: AcquireStampRequest,
    http_request: Request
):
    """
    Acquire a stamp from the pool for immediate use (~5 seconds vs >1 minute).

    **x402 Payment** (when gateway has x402 enabled):
    This endpoint requires payment OR free tier access. Check `GET /health` for availability.
    - **Free tier**: Add header `X-Payment-Mode: free` (rate limited)
    - **Paid**: Include x402 payment header (higher rate limit)
    - Without either header, returns **HTTP 402** with payment instructions and free tier info

    **Quick start** (free tier):
    ```bash
    curl -X POST http://gateway/api/v1/pool/acquire \\
         -H "Content-Type: application/json" \\
         -H "X-Payment-Mode: free" \\
         -d '{"size": "small"}'
    ```

    The stamp is released from the pool and becomes the caller's responsibility.
    Use the returned `batch_id` as the `stamp_id` parameter for data uploads.
    """
    if not settings.STAMP_POOL_ENABLED:
        raise HTTPException(
            status_code=404,
            detail="Stamp pool feature is not enabled on this gateway. Use POST /api/v1/stamps/ to purchase stamps directly."
        )

    # Daily allowance for this origin. Checked before any batch is taken, and
    # only consumed once one has actually been handed over — a failed acquire
    # must not spend somebody's budget.
    #
    # Origin is attribution, not authentication: a browser cannot forge another
    # site's, but anything that is not a browser can claim any origin it likes.
    # The budget is what protects the pool; the origin only selects which budget.
    origin = http_request.headers.get("origin")
    # Resolve the size first: the budget is per size, because a depth-20 batch
    # costs eight times a depth-17 one and a shared count would let a caller
    # spend eight times its allowance by asking for a larger one.
    requested_depth = request.requested_depth()
    requested_size = depth_to_size_name(requested_depth)

    # A settled payment bypasses the allowance. The allowance bounds what the
    # operator GIVES AWAY; it has no business limiting what someone has paid for.
    #
    # This branch did not execute for a long time: the handler read
    # request.state.x402_payer and registered the batch to that wallet when
    # present, but the payment dependency was not attached to this router, so
    # the attribute was always None and every acquire fell to the "shared" path.
    x402_mode_pre = getattr(http_request.state, "x402_mode", None)
    settled = x402_mode_pre == "paid"

    # A settled payment only buys a bypass where the payment was worth
    # something. On a testnet the currency is free from a faucet, so honouring
    # it would hand anyone an unlimited supply of batches the operator paid real
    # BZZ for — strictly worse than the allowance it replaces. The payment is
    # still settled and the batch still registered to the payer; only the
    # bypass is withheld, so the caller keeps its normal daily allowance.
    paid = settled and settings.paid_bypass_is_honoured()
    if settled and not paid:
        logger.warning(
            "Pool acquire settled on %s, which is a test network: allowance "
            "still applies. Set X402_ALLOW_TESTNET_PAID_BYPASS to override.",
            settings.X402_NETWORK,
        )

    # Pick the batch first, so the allowance is charged for the size actually
    # handed out rather than the size asked for (#351).
    stamp = stamp_pool_manager.get_available_stamp(requested_depth)
    fallback_used = False

    # If no exact match, a larger batch may stand in, but not for a paying
    # caller: the payment was priced for the requested size, and a larger batch
    # costs the operator up to 32x more (#362). Deliberately keyed on `settled`
    # (any settled payment, testnet included) rather than `paid` (payments that
    # also bypass the allowance): a payment is priced for one size either way.
    if not stamp and not settled:
        stamp = stamp_pool_manager.get_available_stamp_any_size(requested_depth)
        if stamp:
            fallback_used = True

    charged_size = depth_to_size_name(stamp.depth) if stamp else requested_size

    # An empty pool is answered before any allowance: offering to take payment
    # (402) or to wait for tomorrow (429) for a size that is not in stock would
    # only send the caller down a path that fails again.
    if not stamp:
        size_name = depth_to_size_name(requested_depth)
        pool_acquires_total.labels(size=size_name, status="error").inc()
        message = f"No stamp available for depth {requested_depth} (size: {size_name}). Pool is exhausted."
        if settled:
            message = (f"No {size_name} stamp is available right now. Paid acquires are served only at "
                       f"the size that was paid for, so no larger stamp was substituted and nothing was charged.")
        raise HTTPException(
            status_code=409,
            detail={
                "message": message,
                "suggestion": "Purchase a stamp directly via POST /api/v1/stamps/"
            }
        )

    client_address = get_client_ip(http_request)
    if paid:
        logger.info("Pool acquire paid via x402, bypassing the daily allowance")
    else:
        allowed_by_budget, budget = pool_allowance_tracker.check(origin, charged_size)
        address_ok, address_info = (True, None)
        if allowed_by_budget:
            address_ok, address_info = pool_allowance_tracker.check_address(
                origin, charged_size, client_address)

        if (not allowed_by_budget or not address_ok) and fallback_used:
            # The size asked for is out of stock and the allowance for the
            # larger size that would stand in is spent. Blaming the larger
            # size's allowance would be confusing (the caller never asked for
            # it); the accurate answer is that the requested size is
            # momentarily unavailable.
            whose = "this application" if not allowed_by_budget else "this client"
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "REQUESTED_SIZE_UNAVAILABLE",
                    "size": requested_size,
                    "message": (
                        f"No {requested_size} stamp is available right now; the pool is being "
                        f"refilled. A larger stamp was available, but today's {charged_size} "
                        f"allowance for {whose} is used up. Try again in a few minutes, "
                        f"or buy a stamp directly with POST /api/v1/stamps/."
                    ),
                },
            )

        if not allowed_by_budget:
            logger.info(
                "Pool allowance exhausted for origin %s (%s/%s today)",
                budget["origin"], budget["used"], budget["allowance"],
            )
            # Written to be shown to a person, not just logged: the caller is a
            # browser app whose user has never heard of a postage batch, so it
            # says what they can do rather than only what failed.
            await _refuse_allowance(
                http_request,
                message=(
                    f"The daily free allowance of {budget['allowance']} {charged_size} stamps for this "
                    f"application has been used up. It resets at {budget['resets_at']}. "
                ),
                detail={
                    "code": "DAILY_STAMP_ALLOWANCE_EXHAUSTED",
                    "size": charged_size,
                    "allowance": budget["allowance"],
                    "used": budget["used"],
                    "resets_at": budget["resets_at"],
                },
            )

        if not address_ok:
            await _refuse_allowance(
                http_request,
                message=(
                    f"This client has used its {address_info['address_allowance']} free "
                    f"{charged_size} stamps for today. It resets at {address_info['resets_at']}. "
                ),
                detail={
                    "code": "DAILY_STAMP_ALLOWANCE_PER_CLIENT_EXHAUSTED",
                    "size": charged_size,
                    "allowance": address_info["address_allowance"],
                    "resets_at": address_info["resets_at"],
                },
            )

    # Get client identifier for logging
    client_ip = http_request.client.host if http_request.client else "unknown"

    # Release the stamp
    released = stamp_pool_manager.release_stamp(stamp.batch_id, released_to=client_ip)

    if not released:
        size_name = depth_to_size_name(requested_depth)
        pool_acquires_total.labels(size=size_name, status="error").inc()
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Stamp was acquired by another request.",
                "suggestion": "Retry the request or purchase a stamp directly via POST /api/v1/stamps/"
            }
        )

    # Register stamp ownership
    x402_mode = getattr(http_request.state, 'x402_mode', None)
    x402_payer = getattr(http_request.state, 'x402_payer', None)
    if x402_mode == "paid" and x402_payer:
        stamp_ownership_manager.register_stamp(
            batch_id=released.batch_id,
            owner=x402_payer,
            mode="paid",
            source="pool_acquire"
        )
    else:
        stamp_ownership_manager.register_stamp(
            batch_id=released.batch_id,
            owner="shared",
            mode="free",
            source="pool_acquire"
        )

    # Consumed only now: the batch has been released to the caller, so the
    # allowance has genuinely been spent. A paid acquire consumes nothing — the
    # caller bought this batch rather than drawing on the free budget.
    if not paid:
        pool_allowance_tracker.consume(origin, charged_size, client_address)

    # Trigger immediate replenishment if pool is below target
    # This runs in the background and doesn't affect the response
    replenishment_triggered = stamp_pool_manager.trigger_replenishment_if_needed(released.depth)
    if replenishment_triggered:
        logger.info(f"Triggered immediate replenishment for depth {released.depth}")

    size_name = depth_to_size_name(released.depth)
    pool_acquires_total.labels(size=size_name, status="success").inc()

    message = f"Stamp acquired from pool (depth={released.depth}, size={size_name})"
    if fallback_used:
        message = f"Requested size not available. {message} (larger than requested)"

    return AcquireStampResponse(
        success=True,
        batch_id=released.batch_id,
        depth=released.depth,
        size_name=size_name,
        message=message,
        fallback_used=fallback_used
    )


@router.get(
    "/available",
    response_model=List[PoolStampInfo],
    summary="List Available Stamps",
    description="List all stamps currently available in the pool."
)
async def list_available_stamps():
    """List all available stamps in the pool."""
    if not settings.STAMP_POOL_ENABLED:
        raise HTTPException(
            status_code=404,
            detail="Stamp pool feature is not enabled on this gateway. Use POST /api/v1/stamps/ to purchase stamps directly."
        )

    status = stamp_pool_manager.get_status()
    stamps = []

    # Get stamp details
    for depth, batch_ids in status.available_stamps.items():
        for batch_id in batch_ids:
            stamp = stamp_pool_manager._pool.get(batch_id)
            if stamp:
                stamps.append(PoolStampInfo(
                    batch_id=stamp.batch_id,
                    depth=stamp.depth,
                    size_name=depth_to_size_name(stamp.depth),
                    created_at=stamp.created_at.isoformat(),
                    ttl_at_creation=stamp.ttl_at_creation
                ))

    return stamps


@router.post(
    "/check",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ManualCheckAcceptedResponse,
    summary="Trigger Pool Maintenance (operator only)",
    description=(
        "Schedule a pool maintenance check: sync existing stamps, purchase new ones "
        "if the reserve is short, and top up low-TTL stamps. Normally runs "
        "automatically in the background; this is the manual trigger.\n\n"
        "**This spends BZZ**, so it requires a signature from an address in "
        "POOL_ADMIN_ADDRESSES. Sign `swarm-connect-pool-check:<unix_ts>` with "
        "EIP-191 personal_sign and send it as X-Debug-Signature with "
        "X-Debug-Timestamp. Returns 404 when no allow-list is configured.\n\n"
        "Returns 202 immediately; poll `GET /api/v1/pool/status` for the outcome."
    ),
)
async def trigger_pool_check(
    background_tasks: BackgroundTasks,
    x_debug_timestamp: Optional[str] = Header(None, alias="X-Debug-Timestamp"),
    x_debug_signature: Optional[str] = Header(None, alias="X-Debug-Signature"),
):
    """Schedule pool maintenance. Operator-only, because it spends money.

    This endpoint was unauthenticated (#292). It calls check_and_replenish(),
    which buys postage batches with the gateway's own funds, so anyone able to
    resolve the hostname could spend them — against a production wallet holding
    real BZZ. It was also awaited in full, and a purchase takes about sixteen
    seconds, so repeated calls were a cheap way to tie up workers.

    Both halves are closed here: a signature from POOL_ADMIN_ADDRESSES over a
    prefix distinct from the diagnostics one, and a 202 that schedules the work
    instead of holding the connection for it.
    """
    if not settings.STAMP_POOL_ENABLED:
        raise HTTPException(
            status_code=404,
            detail="Stamp pool feature is not enabled on this gateway. Use POST /api/v1/stamps/ to purchase stamps directly."
        )

    # Ordered so an unconfigured gateway answers 404 for both reasons alike, and
    # an unauthorised caller learns nothing about pool state.
    signer = authorize_signed_request(
        prefix=POOL_CHECK_PREFIX,
        allowed=settings.get_pool_admin_addresses(),
        timestamp=x_debug_timestamp,
        signature=x_debug_signature,
        operation="pool maintenance (spends BZZ)",
    )

    background_tasks.add_task(stamp_pool_manager.check_and_replenish)
    logger.info("Pool maintenance scheduled by %s", signer)

    return ManualCheckAcceptedResponse(
        scheduled_at=datetime.now(timezone.utc).isoformat(),
        message=(
            "Pool maintenance scheduled. Poll GET /api/v1/pool/status for the "
            "result; `last_check` advances and `errors` reports any failure."
        ),
    )
