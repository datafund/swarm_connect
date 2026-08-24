# app/api/endpoints/stamps_for_owner.py
"""
Flow B endpoint: create a postage batch owned by an ARBITRARY address.

POST /api/v1/stamps/for-owner — calls PostageStamp.createBatch(owner=...) on Gnosis
via the chain client, so the owner can sign its own stamps off-node. SPENDS the
gateway's Gnosis funds, so it is OFF by default and guarded (#230):
  - master toggle STAMP_PURCHASE_FOR_OTHERS_ENABLED (router 404s when off),
  - owner allow-list (STAMP_FOR_OTHERS_REQUIRE_WHITELIST / _OWNER_WHITELIST),
  - hard caps on depth / BZZ cost / duration — all enforced BEFORE any on-chain spend.

When x402 is enabled (#229) the caller pays for the service via the /api/v1/stamps/
protected prefix; free-tier creation is OFF by default (real BZZ is spent). Before the
on-chain write the gateway's own signer wallet is preflighted (#231) so we never attempt
a createBatch we cannot fund.
"""
import logging

from fastapi import APIRouter, HTTPException, Request, status

from app.api.models.stamp import StampForOwnerRequest, StampForOwnerResponse
from app.core.config import settings
from app.services import metrics, swarm_api
from app.services.gnosis_chain import GnosisChainError, gnosis_chain_client
from app.services.stamp_ownership import stamp_ownership_manager
from app.services.stamp_tracker import record_purchase
from app.x402.audit import log_stamp_purchased
from app.x402.middleware import get_client_ip

logger = logging.getLogger(__name__)
router = APIRouter()

PLUR_PER_BZZ = 10 ** 16


@router.post(
    "/for-owner",
    response_model=StampForOwnerResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a postage batch owned by an external address",
)
async def create_batch_for_owner(body: StampForOwnerRequest, request: Request) -> StampForOwnerResponse:
    """Create a postage batch on Gnosis owned by `body.owner` (Flow B).

    Guarded by a master toggle, an owner allow-list, and hard caps (depth/BZZ/duration),
    all checked before any on-chain spend. Returns the resulting batchID + txHash.
    """
    if not settings.STAMP_PURCHASE_FOR_OTHERS_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Buy-batch-for-owner is not enabled on this gateway.")

    owner = body.owner
    depth = body.get_effective_depth()

    # --- caps (#230), before any spend ---
    if depth > settings.STAMP_FOR_OTHERS_MAX_DEPTH:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={
            "code": "DEPTH_TOO_HIGH", "message": f"depth {depth} exceeds max {settings.STAMP_FOR_OTHERS_MAX_DEPTH}",
            "max_depth": settings.STAMP_FOR_OTHERS_MAX_DEPTH})
    if body.duration_hours > settings.STAMP_FOR_OTHERS_MAX_DURATION_HOURS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={
            "code": "DURATION_TOO_LONG",
            "message": f"duration {body.duration_hours}h exceeds max {settings.STAMP_FOR_OTHERS_MAX_DURATION_HOURS}h",
            "max_duration_hours": settings.STAMP_FOR_OTHERS_MAX_DURATION_HOURS})

    # --- allow-list (#230) ---
    if settings.STAMP_FOR_OTHERS_REQUIRE_WHITELIST:
        if owner.lower() not in settings.get_stamp_for_others_whitelist():
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={
                "code": "OWNER_NOT_ALLOWLISTED",
                "message": "owner address is not in the allow-list for buy-batch-for-owner"})

    # --- price + cost cap (#230) ---
    try:
        chainstate = await swarm_api.get_chainstate()
        current_price = int(chainstate["currentPrice"])
    except Exception as e:
        logger.error(f"for-owner: failed to read chainstate: {e}")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                            detail="Could not read current price from the Bee node.")

    amount = swarm_api.calculate_stamp_amount(body.duration_hours, current_price)
    total_cost_plur = swarm_api.calculate_stamp_total_cost(amount, depth)
    cost_bzz = total_cost_plur / PLUR_PER_BZZ
    if cost_bzz > settings.STAMP_FOR_OTHERS_MAX_BZZ:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={
            "code": "COST_TOO_HIGH",
            "message": f"batch cost {cost_bzz:.6f} BZZ exceeds max {settings.STAMP_FOR_OTHERS_MAX_BZZ} BZZ",
            "cost_bzz": round(cost_bzz, 6), "max_bzz": settings.STAMP_FOR_OTHERS_MAX_BZZ})

    # --- x402: free-tier creation is OFF by default (#229), since this spends real BZZ ---
    x402_mode = getattr(request.state, "x402_mode", None)
    payer = getattr(request.state, "x402_payer", None)
    if x402_mode == "free-tier" and not settings.STAMP_FOR_OTHERS_FREE_TIER_ENABLED:
        metrics.for_owner_batches_total.labels(status="payment_required").inc()
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail={
            "code": "FREE_TIER_DISABLED",
            "message": "Buy-batch-for-owner requires payment (free tier disabled); send an X-PAYMENT header."})

    if not gnosis_chain_client.is_configured:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Gnosis chain client is not configured on this gateway.")

    # --- preflight the signer wallet (#231): never attempt a createBatch we can't fund ---
    try:
        pf = await gnosis_chain_client.preflight(required_plur=total_cost_plur)
    except Exception as e:
        logger.error(f"for-owner: signer preflight failed: {e}")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Could not read the gateway signer wallet balance.")
    if pf["is_critical"]:
        metrics.for_owner_batches_total.labels(status="insufficient_funds").inc()
        logger.error(f"for-owner: signer wallet cannot fund batch: {pf['warnings']}")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail={
            "code": "SIGNER_INSUFFICIENT_FUNDS",
            "message": "Gateway signer wallet has insufficient funds to create this batch.",
            "warnings": pf["warnings"]})

    # --- on-chain createBatch(owner=...) ---
    try:
        result = await gnosis_chain_client.create_batch(owner, amount, depth, immutable=body.immutable)
    except GnosisChainError as e:
        metrics.for_owner_batches_total.labels(status="error").inc()
        logger.error(f"for-owner: createBatch failed: {e}")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                            detail=f"createBatch failed: {e}")

    batch_id = result["batch_id"]
    bid = batch_id[2:] if batch_id.startswith("0x") else batch_id  # Bee uses 64-hex, no 0x

    # propagation tracking + informational ownership record (on-chain is source of truth)
    record_purchase(bid)
    stamp_ownership_manager.register_stamp(batch_id=bid, owner=owner, mode="paid", source="created_for_owner")
    prop = swarm_api.calculate_propagation_signals(bid, usable=None)

    # metrics + audit (payer is the x402 caller; owner is the batch owner)
    metrics.for_owner_batches_total.labels(status="success").inc()
    metrics.for_owner_bzz_spent_total.inc(total_cost_plur)
    try:
        log_stamp_purchased(
            client_ip=get_client_ip(request), stamp_id=bid, amount=amount, depth=depth,
            duration_hours=body.duration_hours, cost_bzz=round(cost_bzz, 8),
            wallet_address=payer or result["owner"],
        )
    except Exception as e:  # auditing must never fail the request
        logger.debug(f"for-owner: audit log failed: {e}")

    return StampForOwnerResponse(
        batchID=bid,
        owner=result["owner"],
        depth=depth,
        duration_hours=body.duration_hours,
        txHash=result["tx_hash"],
        secondsSincePurchase=prop["secondsSincePurchase"],
        estimatedReadyAt=prop["estimatedReadyAt"],
        propagationStatus=prop["propagationStatus"],
    )
