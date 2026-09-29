# app/api/endpoints/stamps.py
from fastapi import APIRouter, HTTPException, Path, Query, Request, status, Body
from fastapi.responses import JSONResponse
from typing import Any, Optional, Union
import asyncio
import datetime
import httpx
import json
import logging
import secrets

from app.x402.settlement import settle_payment
from app.core.config import settings
from app.services import swarm_api
from app.services.swarm_api import expiry_from_amount, get_batch_expiry, plur_to_bzz
from app.services.stamp_ownership import stamp_ownership_manager
from app.services.stamp_tracker import record_purchase
from app.services.spend_budget import (
    GIVEAWAY_KEY, GLOBAL_KEY, spend_budget_tracker, spend_certainly_did_not_happen,
)
from app.core.client_ip import get_client_key
from app.services.metrics import (
    gateway_spend_uncertain_bzz_total,
    stamp_purchases_total,
    stamp_spend_refusals_total,
    stamp_spend_bzz_total,
)
from app.api.models.stamp import (
    StampDetails,
    StampPurchaseRequest,
    StampPurchaseResponse,
    StampExtensionRequest,
    StampExtensionResponse,
    StampListResponse,
    StampHealthCheckResponse,
    StampHealthStatus,
    StampHealthIssue
)

router = APIRouter()
logger = logging.getLogger(__name__)


class SpendReservation:
    """BZZ reserved against the spending limits for one purchase or extension.

    Charged when the request is admitted (#363): the limits used to be checked
    first and charged only after the Bee call returned, so concurrent requests
    all passed the check before any was charged. release_if_unspent() gives it
    back only when the spend certainly did not happen.
    """

    def __init__(self, operation: str, cost_bzz: float, caller: Optional[str], hold):
        self.operation = operation
        self.cost_bzz = cost_bzz
        self.caller = caller  # None: not charged to a caller's budget (paid)
        self.hold = hold

    def release_if_unspent(self, exc: BaseException) -> None:
        if spend_certainly_did_not_happen(exc):
            spend_budget_tracker.release_hold(self.hold)
        else:
            gateway_spend_uncertain_bzz_total.labels(operation=self.operation).inc(self.cost_bzz)
            logger.warning(
                "%s failed with %s; the outcome is uncertain, so its %.6f BZZ stays "
                "charged against the spending limits", self.operation, type(exc).__name__, self.cost_bzz,
            )

    def record(self) -> None:
        stamp_spend_bzz_total.labels(
            operation=self.operation, charged="budget" if self.caller is not None else "paid"
        ).inc(self.cost_bzz)


def _enforce_spend_limits(request: Request, cost_bzz: float, operation: str) -> SpendReservation:
    """Bound what one request, one caller in a day, and the gateway in a day may spend.

    Both stamp endpoints spend the gateway's BZZ for whoever asks. The limits
    answer different questions:

    - `X402_MAX_STAMP_BZZ` bounds a SINGLE request, so no one call can take a
      large share of the wallet however it is shaped.
    - `STAMP_DAILY_BZZ_PER_CALLER` bounds a caller over a day, so the first
      limit cannot simply be applied repeatedly.
    - `GATEWAY_DAILY_BZZ_FREE_CEILING` bounds all unpaid spending in a day,
      whatever the callers look like, leaving headroom for the pool.
    - `GATEWAY_DAILY_BZZ_CEILING` bounds the gateway's total over a day, paid
      or not.

    All applicable limits are reserved together, atomically. Returns the
    reservation; the caller must release_if_unspent() it on failure. Raises
    rather than returning a failure, because every caller of this must stop.
    """
    max_single = settings.X402_MAX_STAMP_BZZ
    if max_single > 0 and cost_bzz > max_single:
        logger.warning(
            "Refusing %s costing %.6f BZZ, above the per-request limit of %.6f",
            operation, cost_bzz, max_single,
        )
        stamp_spend_refusals_total.labels(operation=operation, limit="per_request").inc()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "STAMP_COST_EXCEEDS_LIMIT",
                "message": (
                    f"This {operation} would cost {cost_bzz:.6f} BZZ, above the "
                    f"per-request limit of {max_single:.6f} BZZ. Ask for a smaller "
                    f"depth or a shorter duration."
                ),
                "cost_bzz": round(cost_bzz, 6),
                "limit_bzz": max_single,
            },
        )

    caller = None
    # A settled payment is not drawn from the giveaway budgets — the caller has
    # funded it. Withheld on a test network for the same reason as the pool:
    # testnet currency is free from a faucet, so honouring it there would
    # replace a bounded giveaway with an unbounded one.
    paid = getattr(request.state, "x402_mode", None) == "paid"
    if paid and not settings.paid_bypass_is_honoured():
        logger.warning(
            "Payment for %s settled on %s, which is a test network: the daily "
            "spend budget still applies.", operation, settings.X402_NETWORK,
        )
    if not (paid and settings.paid_bypass_is_honoured()):
        caller = get_client_key(request)

    hold, refused, info = spend_budget_tracker.reserve_spend(cost_bzz, caller)
    if hold is not None:
        return SpendReservation(operation, cost_bzz, caller, hold)

    if refused in (GLOBAL_KEY, GIVEAWAY_KEY):
        which = "gateway_daily" if refused == GLOBAL_KEY else "gateway_free_daily"
        logger.error(
            "Gateway %s spend ceiling reached: %.6f of %.6f BZZ today, %s needs %.6f",
            "total" if refused == GLOBAL_KEY else "free", info["spent_bzz"],
            info["daily_budget_bzz"], operation, cost_bzz,
        )
        stamp_spend_refusals_total.labels(operation=operation, limit=which).inc()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "GATEWAY_DAILY_SPEND_CEILING",
                "message": (
                    ("The gateway has reached its daily spending limit" if refused == GLOBAL_KEY
                     else "Today's free spending on this gateway is used up; paid requests still work")
                    + f". It resets at {info['resets_at']}."
                ),
                "resets_at": info["resets_at"],
            },
        )

    logger.info(
        "Daily spend budget exhausted for %s: %.6f of %.6f BZZ used, request needs %.6f",
        caller, info["spent_bzz"], info["daily_budget_bzz"], cost_bzz,
    )
    stamp_spend_refusals_total.labels(operation=operation, limit="daily_budget").inc()
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail={
            "code": "DAILY_SPEND_BUDGET_EXHAUSTED",
            "message": (
                f"This {operation} would cost {cost_bzz:.6f} BZZ and only "
                f"{info['remaining_bzz']:.6f} BZZ remains of today's "
                f"{info['daily_budget_bzz']:.6f} BZZ allowance. It resets at "
                f"{info['resets_at']}. A smaller or shorter batch may still fit."
            ),
            "cost_bzz": info["request_cost_bzz"],
            "daily_budget_bzz": info["daily_budget_bzz"],
            "spent_bzz": info["spent_bzz"],
            "remaining_bzz": info["remaining_bzz"],
            "resets_at": info["resets_at"],
        },
    )


def _bee_error_detail(exc: httpx.HTTPError):
    """Extract (status_code, message) from a failed Bee request.

    Bee reports refusals as JSON like {"code": 400, "message": "insufficient
    amount for 24h minimum validity"}. That message names the problem exactly,
    so it is worth surfacing rather than replacing with a generic one.

    Returns (None, str(exc)) when there is no response to read — a timeout or a
    connection failure, which genuinely is the node being unreachable.
    """
    response = getattr(exc, "response", None)
    if response is None:
        return None, str(exc)
    message = None
    try:
        body = response.json()
        if isinstance(body, dict):
            message = body.get("message") or body.get("detail")
    except Exception:
        pass
    if not message:
        message = (response.text or "").strip()[:200] or str(exc)
    return response.status_code, message


def _is_owned_by(batch_id: str, wallet: str) -> bool:
    """Check if a stamp is owned by the given wallet address."""
    info = stamp_ownership_manager.get_stamp_info(batch_id)
    if not info:
        return False
    return info.get("mode") == "paid" and info.get("owner") == wallet


@router.get(
    "/",
    response_model=StampListResponse,
    summary="List Swarm Stamp Batches"
)
async def list_stamps(
    wallet: Optional[str] = Query(
        default=None,
        description="Filter to stamps accessible by this wallet address (owned + shared + untracked local). Requires x402 to be enabled."
    ),
    global_view: Optional[bool] = Query(
        default=None,
        alias="global",
        description="If true, return all stamps including non-local (old behavior)."
    ),
    exclusive: Optional[bool] = Query(
        default=None,
        description="When used with wallet, return only stamps purchased by this wallet (excludes shared and untracked stamps)."
    ),
) -> Any:
    """
    Retrieves a list of postage stamp batches from the Swarm network.

    **Default behavior**: Returns only **local** stamps (stamps owned by this Bee node).
    This is the practical default since only local stamps can be used for uploads.

    **Filtering options**:
    - `?global=true` — Return all stamps visible on the network (old behavior)
    - `?wallet=0xABC...` — Return stamps accessible by this wallet (owned + shared + untracked local).
      Only effective when x402 is enabled; ignored otherwise.
    - `?wallet=0xABC...&exclusive=true` — Return only stamps purchased by this wallet (excludes shared/free and untracked stamps).

    Returns:
        StampListResponse: Contains list of filtered stamps and total count

    Raises:
        HTTPException: 502 if Swarm API is unreachable, 500 for other errors
    """
    try:
        processed_stamps = await swarm_api.get_all_stamps_processed()

        # Convert to StampDetails objects for proper validation
        stamp_details = []
        for stamp_data in processed_stamps:
            try:
                stamp_detail = StampDetails(**stamp_data)
                stamp_details.append(stamp_detail)
            except Exception as e:
                logger.warning(f"Skipping invalid stamp data: {e}")
                continue

        # Apply filtering
        if global_view:
            # No filtering — return everything (old behavior)
            pass
        elif wallet and settings.X402_ENABLED:
            if exclusive:
                # Only stamps purchased by this wallet
                stamp_details = [
                    s for s in stamp_details
                    if _is_owned_by(s.batchID, wallet)
                ]
            else:
                # Stamps accessible to this wallet (owned + shared + untracked local)
                stamp_details = [
                    s for s in stamp_details
                    if s.accessMode == "shared"
                    or (s.accessMode is None and s.local)
                    or _is_owned_by(s.batchID, wallet)
                ]
        else:
            # Default: local stamps only
            stamp_details = [s for s in stamp_details if s.local]

        return StampListResponse(
            stamps=stamp_details,
            total_count=len(stamp_details)
        )

    except httpx.HTTPError as e:
        logger.error(f"Failed to retrieve stamps from Swarm API: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not fetch stamp data. The Bee node may be unavailable."
        )
    except Exception as e:
        logger.error(f"Unexpected error fetching stamps: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while fetching stamp data."
        )


@router.get(
    "/{stamp_id}/check",
    response_model=StampHealthCheckResponse,
    summary="Check Stamp Health for Uploads"
)
async def check_stamp_health(
    stamp_id: str = Path(..., description="The Batch ID of the Swarm stamp to check.", example="a1b2c3d4e5f6...", pattern=r"^[a-fA-F0-9]{64}$")
) -> Any:
    """
    Performs a comprehensive health check on a stamp to determine if it can be used for uploads.

    This endpoint checks for all potential issues that could prevent uploads:
    - **Errors** (blocking): Issues that will prevent uploads
    - **Warnings** (non-blocking): Issues to be aware of but won't block uploads

    **Error Codes**:
    - `NOT_FOUND`: Stamp doesn't exist on the connected node
    - `NOT_LOCAL`: Stamp exists but isn't owned by this Bee node
    - `EXPIRED`: Stamp TTL has reached 0
    - `NOT_USABLE`: Stamp is not yet usable (e.g., propagation delay after purchase)
    - `FULL`: Stamp is at 100% utilization

    **Warning Codes**:
    - `LOW_TTL`: Stamp expires in less than 1 hour
    - `NEARLY_FULL`: Stamp is 95%+ utilized
    - `HIGH_UTILIZATION`: Stamp is 80%+ utilized

    **Use Cases**:
    - Check if a recently purchased stamp is ready for use
    - Verify a stamp before starting a large batch upload
    - Diagnose why uploads are failing

    **Example Response**:
    ```json
    {
        "stamp_id": "abc123...",
        "can_upload": true,
        "errors": [],
        "warnings": [
            {
                "code": "HIGH_UTILIZATION",
                "message": "Stamp is 82% utilized.",
                "suggestion": "Monitor usage and consider purchasing additional stamps."
            }
        ],
        "status": {
            "exists": true,
            "local": true,
            "usable": true,
            "utilizationPercent": 82.5,
            "utilizationStatus": "warning",
            "batchTTL": 86400,
            "expectedExpiration": "2026-01-12-17-30"
        }
    }
    ```
    """
    try:
        health_check = await swarm_api.get_stamp_health_check(stamp_id)

        # Convert to response model
        errors = [StampHealthIssue(**e) for e in health_check.get("errors", [])]
        warnings = [StampHealthIssue(**w) for w in health_check.get("warnings", [])]
        status_data = health_check.get("status", {})

        return StampHealthCheckResponse(
            stamp_id=health_check.get("stamp_id", stamp_id),
            can_upload=health_check.get("can_upload", False),
            errors=errors,
            warnings=warnings,
            status=StampHealthStatus(
                exists=status_data.get("exists", False),
                local=status_data.get("local", False),
                usable=status_data.get("usable"),
                utilizationPercent=status_data.get("utilizationPercent"),
                utilizationStatus=status_data.get("utilizationStatus"),
                batchTTL=status_data.get("batchTTL"),
                expectedExpiration=status_data.get("expectedExpiration"),
                secondsSincePurchase=status_data.get("secondsSincePurchase"),
                estimatedReadyAt=status_data.get("estimatedReadyAt"),
                propagationStatus=status_data.get("propagationStatus")
            )
        )

    except httpx.HTTPError as e:
        logger.error(f"Failed to check stamp health from Swarm API: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not fetch stamp data. The Bee node may be unavailable."
        )
    except Exception as e:
        logger.error(f"Unexpected error during stamp health check: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred during stamp health check."
        )


@router.get(
    "/{stamp_id}",
    response_model=StampDetails,
    summary="Get Specific Swarm Stamp Batch Details"
)
async def get_stamp_details(
    stamp_id: str = Path(..., description="The Batch ID of the Swarm stamp to retrieve.", example="a1b2c3d4e5f6...", pattern=r"^[a-fA-F0-9]{64}$")
) -> Any:
    """
    Retrieves details for a specific Swarm postage stamp batch by its ID.

    It fetches all batches from the backend Swarm node, finds the matching batch,
    calculates the expected expiration time based on the current time and the batchTTL,
    and returns the relevant information.
    """
    try:
        all_stamps = await swarm_api.get_all_stamps_processed()
    except httpx.HTTPError as e:
        logger.error(f"Failed to retrieve data from upstream Swarm API: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not fetch data from the Bee node. The Bee node may be unavailable."
        )
    except Exception as e:
         logger.error(f"Unexpected error fetching stamps: {e}", exc_info=True)
         raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while fetching stamp data."
         )


    found_stamp = None
    for stamp in all_stamps:
        if stamp.get("batchID") == stamp_id:
            found_stamp = stamp
            break

    if not found_stamp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Stamp batch with ID '{stamp_id}' not found on the connected Swarm node."
        )

    try:
        # Use the enhanced data directly from get_all_stamps_processed()
        # which already includes calculated expiration, local data merging, etc.
        response_data = StampDetails(
            batchID=found_stamp.get("batchID"),
            amount=str(found_stamp.get("amount", "")),
            blockNumber=found_stamp.get("blockNumber"),
            owner=found_stamp.get("owner"),
            immutableFlag=found_stamp.get("immutableFlag"),
            depth=found_stamp.get("depth"),
            bucketDepth=found_stamp.get("bucketDepth"),
            batchTTL=found_stamp.get("batchTTL"),
            utilization=found_stamp.get("utilization"),
            utilizationPercent=found_stamp.get("utilizationPercent"),
            utilizationStatus=found_stamp.get("utilizationStatus"),
            utilizationWarning=found_stamp.get("utilizationWarning"),
            usable=found_stamp.get("usable"),
            label=found_stamp.get("label"),
            secondsSincePurchase=found_stamp.get("secondsSincePurchase"),
            estimatedReadyAt=found_stamp.get("estimatedReadyAt"),
            propagationStatus=found_stamp.get("propagationStatus"),
            accessMode=found_stamp.get("accessMode"),
            expectedExpiration=found_stamp.get("expectedExpiration"),
            local=found_stamp.get("local")
        )
        return response_data

    except KeyError as e:
        logger.error(f"Missing expected key '{e}' in Swarm API response for stamp {stamp_id}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Incomplete data received from Swarm API. Please try again."
        )
    except (ValueError, TypeError) as e:
        logger.error(f"Data type error processing Swarm API response for stamp {stamp_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Invalid data format received from Swarm API. Please try again."
        )
    except Exception as e:
         logger.error(f"Unexpected error processing stamp {stamp_id}: {e}", exc_info=True)
         raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while processing the stamp data."
         )


_PENDING_FIRST_WAIT_SECONDS = 10
# Background lookups in flight. The event loop keeps only weak references to
# tasks, so one nobody holds can be garbage-collected mid-search.
_PENDING_TASKS: set = set()
# Paid purchases between settlement and Bee's answer (STAMP_MAX_CONCURRENT_PAID_PURCHASES).
_paid_purchases_in_flight = 0


def _release_paid_slot(_=None) -> None:
    global _paid_purchases_in_flight
    _paid_purchases_in_flight = max(0, _paid_purchases_in_flight - 1)


async def drain_pending_purchases(grace_seconds: float) -> None:
    """Shutdown: let Bee requests and background halves finish, then stop them.

    Called before the shared HTTP client is closed. Whatever is still running
    after the grace period is cancelled and awaited, so each writes its refund
    record (naming a purchase still in flight at Bee) before the process exits.
    """
    if not _PENDING_TASKS:
        return
    logger.info(f"Waiting up to {grace_seconds}s for {len(_PENDING_TASKS)} pending purchase task(s)")
    _, still = await asyncio.wait(set(_PENDING_TASKS), timeout=grace_seconds)
    if still:
        logger.error(f"{len(still)} pending purchase task(s) still running at shutdown; cancelling")
        # Background halves first, so they record the shutdown while the Bee
        # request they await is still, for them, in flight.
        for t in sorted(still, key=lambda t: getattr(t, "_is_bee_request", False)):
            t.cancel()
        await asyncio.gather(*still, return_exceptions=True)


def _is_registered(batch_id: str) -> bool:
    return stamp_ownership_manager.get_stamp_info(batch_id) is not None


def _register_purchase(request: Request, batch_id: str, payer: Optional[str] = None,
                       only_if_unowned: bool = False) -> bool:
    """Register a purchased batch to its payer, or as shared for the free tier."""
    payer = payer or getattr(request.state, "x402_payer", None)
    if getattr(request.state, "x402_mode", None) == "paid" and payer:
        return stamp_ownership_manager.register_stamp(
            batch_id=batch_id, owner=payer, mode="paid", source="direct_purchase",
            only_if_unowned=only_if_unowned)
    return stamp_ownership_manager.register_stamp(
        batch_id=batch_id, owner="shared", mode="free", source="direct_purchase",
        only_if_unowned=only_if_unowned)


def _outcome_unknown(e: httpx.HTTPError) -> bool:
    """Bee (or a proxy in front of it) gave no answer about the purchase.

    A timeout or dropped connection, or a 502/504 from a proxy, says nothing
    about whether Bee bought the batch. Any other status is Bee's own answer.
    """
    if isinstance(e, httpx.TransportError):
        return True
    return isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (502, 504)


def _purchase_pending(request: Request, label: str, depth: int, amount: int,
                      start_block: Optional[int], purchase: Optional[asyncio.Task] = None,
                      taken: Optional[str] = None) -> JSONResponse:
    """202 for a paid purchase Bee did not confirm in time (#400).

    The payment has settled and the batch may exist. `purchase` is Bee's
    request, still running: the background half awaits it. Without one (it
    failed without an answer), the background half looks for the batch by its
    label. `taken`: the lookup found the batch already registered to someone
    else, which only needs recording. Either way, the batch is registered to
    the payer when it appears; with an Idempotency-Key, a retry then gets the
    201 instead of this 202.
    """
    from app.x402.audit import AuditEventType, log_audit_event, log_payment_failed
    # The audit trail records the address itself; spend limits key on the
    # grouped value (get_client_key), so this is not imported module-wide.
    from app.core.client_ip import get_client_ip
    payer = getattr(request.state, "x402_payer", None)
    tx = getattr(getattr(request.state, "x402_settlement", None), "transaction", None)
    try:
        stamp_purchases_total.labels(size="custom", status="pending").inc()
        # Everything needed to find the batch by hand, should the search below
        # be interrupted: it is otherwise only in the client's response.
        log_audit_event(event_type=AuditEventType.PURCHASE_PENDING, client_ip=get_client_ip(request),
                        wallet_address=payer,
                        data={"transaction_hash": tx, "label": label, "depth": depth, "amount": str(amount),
                              "start_block": start_block, "network": settings.X402_NETWORK})
    except Exception as e:
        logger.error(f"Could not record a pending purchase (label {label}, tx {tx}): {e}", exc_info=True)
    try:
        task = asyncio.get_running_loop().create_task(_finish_pending_purchase(
            request, label, depth, amount, start_block, payer, tx,
            getattr(request.state, "x402_idempotency_id", None), purchase, taken))
        _PENDING_TASKS.add(task)
        task.add_done_callback(_PENDING_TASKS.discard)
    except Exception as e:
        # Nobody will finish this purchase: record it for the operator, and
        # still observe Bee's request so its outcome is not silently dropped.
        logger.error(f"Could not start the pending purchase task (label {label}): {e}", exc_info=True)
        log_payment_failed(client_ip=get_client_ip(request),
                           reason=f"stamp purchase pending but not followed up ({type(e).__name__}); "
                                  f"a batch may exist on-chain; label={label}; tx={tx}",
                           stage="delivery_after_settlement", wallet_address=payer)
        if purchase is not None:
            purchase.add_done_callback(lambda t: t.cancelled() or t.exception())
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content={
        "code": "PURCHASE_PENDING",
        "message": ("Payment received, but the Bee node did not confirm the purchase in time. "
                    "The batch is registered to your wallet as soon as the node reports it."),
        "transaction": tx,
        "label": label,
        "depth": depth,
        "amount": str(amount),
        "lookup": (f"GET /api/v1/stamps/?wallet={payer} and look for this label. "
                   "Retrying with the same Idempotency-Key returns the batch once it is found."),
    })


async def _finish_pending_purchase(request: Request, label: str, depth: int, amount: int,
                                   start_block: Optional[int], payer: Optional[str],
                                   tx: Optional[str], idem, purchase: Optional[asyncio.Task] = None,
                                   taken: Optional[str] = None) -> None:
    """Background half of _purchase_pending: find, register, record.

    Every way this ends leaves an audit record: delivered (late), or a
    payment_failed for a refund, including an interruption (shutdown) or an
    error of its own.
    """
    from app.x402.audit import AuditEventType, log_audit_event, log_payment_failed
    # The audit trail records the address itself; spend limits key on the
    # grouped value (get_client_key), so this is not imported module-wide.
    from app.core.client_ip import get_client_ip
    from app.x402.idempotency import resolve_idempotent_result
    client_ip = get_client_ip(request)

    def refund_needed(why: str) -> None:
        log_payment_failed(client_ip=client_ip,
                           reason=f"stamp purchase after settlement: {why}; label={label}; tx={tx}",
                           stage="delivery_after_settlement", wallet_address=payer)
        # A retry with the key is told the final outcome, not "pending" for 24 h.
        resolve_idempotent_result(idem, status.HTTP_500_INTERNAL_SERVER_ERROR, json.dumps({
            "code": "DELIVERY_FAILED_AFTER_PAYMENT",
            "message": ("The payment was collected but the batch could not be found on the node. "
                        "Contact the operator with this transaction for a refund."),
            "transaction": tx,
            "x402_status": "settled_not_delivered",
        }).encode())

    batch_id = None
    in_flight = False
    try:
        if taken:
            refund_needed(f"found ({taken}) but already registered to someone else")
            return
        if purchase is not None:
            # Bee's own answer, from the request kept open for it. Shielded so
            # that cancelling this task alone does not cut Bee's request off.
            try:
                in_flight = True
                batch_id = await asyncio.shield(purchase)
                in_flight = False
            except httpx.HTTPError as e:
                if not _outcome_unknown(e):
                    refund_needed(f"refused by Bee ({_bee_error_detail(e)[1] or type(e).__name__})")
                    return
                logger.error(f"Bee gave no answer to a paid purchase ({type(e).__name__}); looking for the batch")
        if batch_id is None:
            # Waiting first also lets the middleware store the 202 for the
            # Idempotency-Key before this replaces it.
            await asyncio.sleep(_PENDING_FIRST_WAIT_SECONDS)
            batch_id = await swarm_api.find_purchased_batch(
                label, depth, amount, _is_registered, start_block,
                wait_seconds=settings.STAMP_PURCHASE_BACKGROUND_LOOKUP_SECONDS, interval=10)
        if batch_id is None:
            refund_needed("not found")
            return
        if not _register_purchase(request, batch_id, payer=payer, only_if_unowned=True):
            refund_needed(f"found ({batch_id}) but already registered to someone else")
            return
    except asyncio.CancelledError:
        if in_flight:
            # Bee's request is being cut off with the process: if its
            # transaction was sent, the batch exists on-chain, unlabelled.
            refund_needed("Bee purchase in flight at shutdown, a batch may exist on-chain unlabelled "
                          "(Bee's 'recovered'): check the node's transactions")
        else:
            refund_needed("lookup interrupted (shutdown)")
        raise
    except Exception as e:
        logger.error(f"Lost purchase lookup failed: {e}", exc_info=True)
        refund_needed(f"lookup failed ({type(e).__name__})")
        return

    # Registered: from here on, bookkeeping only; it cannot undo the outcome.
    try:
        record_purchase(batch_id)
        logger.info(f"Pending purchase delivered: {batch_id[:16]} registered to {payer}")
        log_audit_event(event_type=AuditEventType.PAYMENT_DELIVERED, client_ip=client_ip, wallet_address=payer,
                        data={"transaction_hash": tx, "method": "POST", "path": request.url.path,
                              "network": settings.X402_NETWORK, "resource": {"batchID": batch_id},
                              "late": True})
        body = StampPurchaseResponse(batchID=batch_id, message="Postage stamp purchased successfully")
        resolve_idempotent_result(idem, status.HTTP_201_CREATED, body.model_dump_json().encode())
    except Exception as e:
        logger.error(f"Lost purchase {batch_id[:16]} registered; bookkeeping after it failed: {e}", exc_info=True)


@router.post(
    "/",
    response_model=StampPurchaseResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Purchase a New Swarm Postage Stamp"
)
async def purchase_stamp(
    request: Request,
    stamp_request: StampPurchaseRequest
) -> Any:
    """
    Purchases a new postage stamp from the Swarm network.

    **x402 Payment** (when gateway has x402 enabled):
    This endpoint requires payment OR free tier access. Check `GET /health` for availability.
    - **Free tier**: Add header `X-Payment-Mode: free` (rate limited)
    - **Paid**: Include x402 payment header (higher rate limit)
    - Without either header, returns **HTTP 402** with payment instructions and free tier info

    **Tip**: For faster stamp acquisition, use `POST /api/v1/pool/acquire` instead (instant
    from pre-purchased pool vs ~1 minute for on-chain purchase).

    Creates a new postage stamp batch with the specified duration or amount and depth.
    If duration_hours is provided, amount is calculated based on current network price.
    If neither is provided, defaults to 25 hours duration.

    The endpoint checks wallet balance before purchase and returns a meaningful error
    if funds are insufficient.

    Args:
        stamp_request: Purchase request containing duration_hours or amount, depth, and optional label

    Returns:
        StampPurchaseResponse: Contains the new batch ID and success message

    Raises:
        HTTPException: 400 if insufficient funds, 402 if payment required, 502 if Swarm API is unreachable
    """
    try:
        # Get effective depth from size preset or explicit depth
        effective_depth = stamp_request.get_effective_depth()
        price_for_expiry = None

        # A paid purchase buys exactly the batch its price was computed for
        # (#361): the pricer parsed this same body, and recalculating here from
        # a second chainstate read could buy more than was paid for.
        priced = getattr(request.state, "x402_priced_batch", None)
        if priced and getattr(request.state, "x402_mode", None) == "paid" and priced["depth"] == effective_depth:
            amount = priced["amount"]
        elif stamp_request.amount is not None:
            # Legacy mode: use provided amount directly
            amount = stamp_request.amount
        else:
            # Calculate amount from duration (default 25 hours)
            duration_hours = stamp_request.duration_hours or 25
            chainstate = await swarm_api.get_chainstate()
            current_price = int(chainstate["currentPrice"])
            price_for_expiry = current_price
            amount = swarm_api.calculate_stamp_amount(
                duration_hours, current_price,
                minimum_validity_blocks=chainstate.get("minimumValidityBlocks"),
            )
            logger.info(f"Calculated amount {amount} for {duration_hours} hours at price {current_price}")

        # Calculate total cost and check funds
        total_cost = swarm_api.calculate_stamp_total_cost(amount, effective_depth)
        funds_check = await swarm_api.check_sufficient_funds(total_cost)

        # Bound the spend BEFORE the funds check, so the answer does not depend
        # on how much money happens to be left. Refusing a 243,074 BZZ request
        # for "insufficient funds" told the caller the wallet was the only limit,
        # which was true and is the defect this closes.
        cost_bzz = plur_to_bzz(total_cost)
        reservation = _enforce_spend_limits(request, cost_bzz, "stamp purchase")

        # Charged already; handed back if the purchase certainly did not
        # happen. A paid purchase that goes on in the background (202) keeps
        # its hold: its outcome is not known yet.
        try:
            if not funds_check["sufficient"]:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Insufficient funds to purchase stamp. "
                        f"Required: {funds_check['required_bzz']:.6f} BZZ, "
                        f"Available: {funds_check['wallet_balance_bzz']:.6f} BZZ, "
                        f"Shortfall: {funds_check['shortfall_bzz']:.6f} BZZ"
                    )
                )

            # A paid purchase sends Bee a label unique to it, so it can be found on
            # the node if Bee's answer is lost after the payment settled (#400):
            # the caller's label with a random suffix, or a generated one. A label
            # the caller chose alone could match someone else's batch.
            paid = getattr(request.state, "x402_mode", None) == "paid"
            label = stamp_request.label
            start_block = None
            if paid:
                suffix = secrets.token_hex(6)
                label = f"{label}-{suffix}" if label else f"paid-{suffix}"
                # Where the chain was when the purchase started: an older batch can
                # never be this one.
                try:
                    start_block = swarm_api.coerce_int((await swarm_api.get_chainstate()).get("block"), -1)
                    start_block = start_block if start_block >= 0 else None
                except Exception as e:
                    logger.warning(f"No chain block before a paid purchase: {e}")

            # Collect the payment immediately before the purchase: every check
            # above can refuse the request, and a refusal must not cost anything.
            #
            # Inside the try, so a settlement failure releases the reservation.
            # That is safe and intended: settle_payment raises HTTPException,
            # which spend_certainly_did_not_happen() treats as "spent nothing",
            # and a retry of the same authorization is only delivered if its own
            # settlement succeeds — a transfer that did go through has spent the
            # nonce, so it cannot.
            found_by_lookup = False
            try:
                if not paid:
                    await settle_payment(request)   # a no-op unless paid
                    batch_id = await swarm_api.purchase_postage_stamp(
                        amount=amount,
                        depth=effective_depth,
                        label=label
                    )
                else:
                    # A bounded number of paid purchases wait on Bee at once
                    # (checked before settlement, so a refusal costs nothing).
                    global _paid_purchases_in_flight
                    if _paid_purchases_in_flight >= max(1, settings.STAMP_MAX_CONCURRENT_PAID_PURCHASES):
                        raise HTTPException(
                            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            headers={"Retry-After": "30"},
                            detail={"code": "PURCHASE_CAPACITY",
                                    "message": ("Too many stamp purchases are waiting on the Bee node. "
                                                "You were not charged; retry shortly."),
                                    })
                    _paid_purchases_in_flight += 1
                    try:
                        await settle_payment(request)
                    except BaseException:
                        _release_paid_slot()
                        raise
                    # Paid: Bee's request runs as a task of its own with a long
                    # timeout, and is never cut off by ours. Bee names the batch only
                    # after the receipt, on that request's context; closed early, the
                    # batch comes back as "recovered", which cannot be told apart
                    # from anyone else's. Past our deadline the caller gets a 202,
                    # and the background half awaits this same task.
                    purchase = asyncio.get_running_loop().create_task(swarm_api.purchase_postage_stamp(
                        amount=amount, depth=effective_depth, label=label,
                        timeout=settings.STAMP_PURCHASE_BEE_TIMEOUT_SECONDS))
                    purchase._is_bee_request = True
                    _PENDING_TASKS.add(purchase)
                    purchase.add_done_callback(_PENDING_TASKS.discard)
                    purchase.add_done_callback(_release_paid_slot)
                    try:
                        batch_id = await asyncio.wait_for(asyncio.shield(purchase),
                                                          settings.SWARM_STAMP_PURCHASE_TIMEOUT_SECONDS)
                    except asyncio.TimeoutError:
                        return _purchase_pending(request, label, effective_depth, amount, start_block,
                                                 purchase=purchase)
                    except asyncio.CancelledError:
                        # This request was cut off; the purchase goes on. Finish it
                        # in the background so the batch still reaches the payer.
                        _purchase_pending(request, label, effective_depth, amount, start_block, purchase=purchase)
                        raise
            except httpx.HTTPError as e:
                # No answer about the purchase. It may still have happened. Unpaid,
                # the caller just retries; paid, look for the batch rather than keep
                # the money and report a failure.
                if getattr(request.state, "x402_settlement", None) is None or not _outcome_unknown(e):
                    raise
                logger.error(f"Bee gave no answer to a paid purchase ({type(e).__name__}); looking for the batch")
                try:
                    batch_id = await swarm_api.find_purchased_batch(
                        label, effective_depth, amount, _is_registered, start_block,
                        wait_seconds=settings.STAMP_PURCHASE_LOOKUP_SECONDS,
                    )
                except Exception as lookup_error:
                    logger.error(f"Lost purchase lookup failed: {lookup_error}")
                    batch_id = None
                if batch_id is None:
                    return _purchase_pending(request, label, effective_depth, amount, start_block)
                if not _register_purchase(request, batch_id, only_if_unowned=True):
                    return _purchase_pending(request, label, effective_depth, amount, start_block, taken=batch_id)
                found_by_lookup = True
        except BaseException as exc:
            reservation.release_if_unspent(exc)
            raise

        # Ownership first: once the batch id is known, nothing that can fail
        # may stand between the payer and the batch they paid for.
        if not found_by_lookup:
            _register_purchase(request, batch_id)

        try:
            reservation.record()

            # Record purchase time for propagation tracking
            record_purchase(batch_id)

            size_label = stamp_request.size or "custom"
            stamp_purchases_total.labels(size=size_label, status="success").inc()
        except Exception as e:
            # Bookkeeping only. The batch is bought and registered: return it.
            logger.error(f"Stamp {batch_id[:16]} bought, bookkeeping after it failed: {e}", exc_info=True)

        # Estimated from the amount funded at today's price (#383).
        if price_for_expiry is None:
            try:
                price_for_expiry = int((await swarm_api.get_chainstate())["currentPrice"])
            except Exception:
                price_for_expiry = None
        return StampPurchaseResponse(
            batchID=batch_id,
            message="Postage stamp purchased successfully",
            expires_at=expiry_from_amount(amount, price_for_expiry) if price_for_expiry else None,
        )

    except HTTPException:
        size_label = stamp_request.size or "custom"
        stamp_purchases_total.labels(size=size_label, status="error").inc()
        raise  # Re-raise HTTP exceptions as-is
    except httpx.HTTPError as e:
        stamp_purchases_total.labels(size=stamp_request.size or "custom", status="error").inc()
        logger.error(f"Failed to purchase stamp from Swarm API: {e}")
        # A 4xx from Bee is the caller's request being wrong (an amount below the
        # minimum validity, a bad depth), not the node being unavailable. Masking
        # it as 502 tells the caller to go and check node health for a problem
        # they can fix in their own request, and hides the reason entirely.
        bee_status_code, bee_message = _bee_error_detail(e)
        if bee_status_code is not None and 400 <= bee_status_code < 500:
            raise HTTPException(
                status_code=bee_status_code,
                detail=f"Bee rejected the stamp purchase: {bee_message}"
            ) from e
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not purchase stamp. The Bee node may be unavailable."
        )
    except ValueError as e:
        stamp_purchases_total.labels(size=stamp_request.size or "custom", status="error").inc()
        logger.error(f"Invalid response from Swarm API during stamp purchase: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Invalid response from Swarm API. Please try again."
        )
    except Exception as e:
        stamp_purchases_total.labels(size=stamp_request.size or "custom", status="error").inc()
        logger.error(f"Unexpected error during stamp purchase: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while purchasing the stamp."
        )


@router.patch(
    "/{stamp_id}/extend",
    response_model=StampExtensionResponse,
    summary="Extend an Existing Swarm Postage Stamp"
)
async def extend_stamp(
    request: Request,
    stamp_id: str = Path(..., description="The Batch ID of the stamp to extend.", example="a1b2c3d4e5f6...", pattern=r"^[a-fA-F0-9]{64}$"),
    extension_request: StampExtensionRequest = ...
) -> Any:
    """
    Extends an existing postage stamp by adding more funds to it.

    **Payment and ownership** (when x402 is enabled): priced like a purchase
    (x402 payment, or `X-Payment-Mode: free` within the free-tier rate limit).
    A batch registered to a payer can only be extended by a paid request from
    that payer; a shared batch can be extended by anyone; pool inventory and
    batches the gateway has no record of cannot be extended. A legacy `amount`
    must be worth at least 24 hours. With x402 disabled, only the minimum
    amount and the spend limits apply.

    This operation adds the specified duration or amount to the existing stamp,
    extending its validity period. If duration_hours is provided, amount is
    calculated based on current network price. If neither is provided, defaults
    to 25 hours.

    The endpoint checks wallet balance before extension and returns a meaningful
    error if funds are insufficient.

    Args:
        stamp_id: The batch ID of the stamp to extend
        extension_request: Extension request containing duration_hours or amount

    Returns:
        StampExtensionResponse: Contains the batch ID and success message

    Raises:
        HTTPException: 400 if insufficient funds, 404 if stamp not found, 502 if Swarm API unreachable
    """
    try:
        # First, get the stamp to verify it exists and get its depth
        all_stamps = await swarm_api.get_all_stamps_processed()
        found_stamp = None
        for stamp in all_stamps:
            if stamp.get("batchID") == stamp_id:
                found_stamp = stamp
                break

        if not found_stamp:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Stamp batch with ID '{stamp_id}' not found on the connected Swarm node."
            )

        stamp_depth = found_stamp.get("depth", 17)

        # Only the batch's owner may top it up (#350). The same rule as uploads:
        # a batch registered to a payer needs that payer, a shared batch may be
        # extended by anyone, and pool inventory or untracked batches may not be
        # extended through this route at all. Checked before any spend, and
        # before the payment is settled, so a refusal costs the caller nothing.
        if settings.X402_ENABLED:
            allowed, reason = stamp_ownership_manager.check_access(
                stamp_id,
                getattr(request.state, "x402_payer", None),
                getattr(request.state, "x402_mode", None),
            )
            if not allowed:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "code": "STAMP_OWNERSHIP_DENIED",
                        "message": f"Cannot extend this stamp: {reason}",
                        "stamp_id": stamp_id,
                    },
                )

        # Determine the amount to use
        chainstate = await swarm_api.get_chainstate()
        current_price = int(chainstate["currentPrice"])
        if current_price <= 0:
            # Bee reports 0 while it is still syncing chain state. The minimum
            # below would then be 0 and admit any amount.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="The Bee node has not reported a current stamp price yet. Try again shortly.",
            )
        if extension_request.amount is not None:
            # Legacy mode: use provided amount directly, but not below 24 hours'
            # worth. Every top-up is an on-chain transaction paid in gas and holds
            # Bee's single on-chain-operation lock, so a near-zero amount costs
            # the gateway far more than it adds and blocks everyone else's
            # purchases while it runs (#350).
            amount = extension_request.amount
            minimum = current_price * 24 * swarm_api.BLOCKS_PER_HOUR
            if amount < minimum:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "code": "EXTENSION_TOO_SMALL",
                        "message": (
                            f"An extension must add at least 24 hours: amount >= {minimum} "
                            f"PLUR per chunk at the current price. Use duration_hours instead."
                        ),
                        "minimum_amount": minimum,
                    },
                )
        else:
            # Calculate amount from duration (default 25 hours)
            duration_hours = extension_request.duration_hours or 25
            amount = swarm_api.calculate_stamp_amount(
                duration_hours, current_price,
                minimum_validity_blocks=chainstate.get("minimumValidityBlocks"),
            )
            logger.info(f"Calculated extension amount {amount} for {duration_hours} hours at price {current_price}")

        # Calculate total cost and check funds
        total_cost = swarm_api.calculate_stamp_total_cost(amount, stamp_depth)
        funds_check = await swarm_api.check_sufficient_funds(total_cost)

        # Paid or free-tier through the x402 dependency (#350), owner-checked
        # above, and still bounded per caller by the daily budget.
        cost_bzz = plur_to_bzz(total_cost)
        reservation = _enforce_spend_limits(request, cost_bzz, "stamp extension")

        try:
            if not funds_check["sufficient"]:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Insufficient funds to extend stamp. "
                        f"Required: {funds_check['required_bzz']:.6f} BZZ, "
                        f"Available: {funds_check['wallet_balance_bzz']:.6f} BZZ, "
                        f"Shortfall: {funds_check['shortfall_bzz']:.6f} BZZ"
                    )
                )

            # Same ordering as the purchase path: settle immediately before the
            # irreversible call, inside the try so a settlement failure gives the
            # reservation back.
            await settle_payment(request)

            batch_id = await swarm_api.extend_postage_stamp(
                stamp_id=stamp_id,
                amount=amount
            )
        except BaseException as exc:
            reservation.release_if_unspent(exc)
            raise
        reservation.record()

        return StampExtensionResponse(
            batchID=batch_id,
            message="Postage stamp extended successfully",
            expires_at=await get_batch_expiry(stamp_id),
        )

    except HTTPException:
        raise  # Re-raise HTTP exceptions as-is
    except httpx.HTTPError as e:
        logger.error(f"Failed to extend stamp {stamp_id} from Swarm API: {e}")
        # Same reasoning as the purchase path: a 4xx from Bee describes the
        # request, not the node's availability, and its message names the cause.
        bee_status_code, bee_message = _bee_error_detail(e)
        if bee_status_code is not None and 400 <= bee_status_code < 500:
            raise HTTPException(
                status_code=bee_status_code,
                detail=f"Bee rejected the stamp extension: {bee_message}"
            ) from e
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not extend stamp. The Bee node may be unavailable."
        )
    except ValueError as e:
        logger.error(f"Invalid response from Swarm API during stamp extension: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Invalid response from Swarm API. Please try again."
        )
    except Exception as e:
        logger.error(f"Unexpected error during stamp extension for {stamp_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while extending the stamp."
        )
