# app/api/endpoints/stamps.py
from fastapi import APIRouter, HTTPException, Path, Query, Request, status, Body
from typing import Any, Optional, Union
import datetime
from requests.exceptions import RequestException
import logging

from app.core.config import settings
from app.services import swarm_api
from app.services.stamp_ownership import stamp_ownership_manager
from app.services.stamp_tracker import record_purchase
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
) -> Any:
    """
    Retrieves a list of postage stamp batches from the Swarm network.

    **Default behavior**: Returns only **local** stamps (stamps owned by this Bee node).
    This is the practical default since only local stamps can be used for uploads.

    **Filtering options**:
    - `?global=true` — Return all stamps visible on the network (old behavior)
    - `?wallet=0xABC...` — Return stamps accessible by this wallet (owned + shared + untracked local).
      Only effective when x402 is enabled; ignored otherwise.

    Returns:
        StampListResponse: Contains list of filtered stamps and total count

    Raises:
        HTTPException: 502 if Swarm API is unreachable, 500 for other errors
    """
    try:
        processed_stamps = swarm_api.get_all_stamps_processed()

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
            # Show stamps accessible to this wallet
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

    except RequestException as e:
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
        health_check = swarm_api.get_stamp_health_check(stamp_id)

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

    except RequestException as e:
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
        all_stamps = swarm_api.get_all_stamps_processed()
    except RequestException as e:
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

        # Determine the amount to use
        if stamp_request.amount is not None:
            # Legacy mode: use provided amount directly
            amount = stamp_request.amount
        else:
            # Calculate amount from duration (default 25 hours)
            duration_hours = stamp_request.duration_hours or 25
            chainstate = swarm_api.get_chainstate()
            current_price = int(chainstate["currentPrice"])
            amount = swarm_api.calculate_stamp_amount(duration_hours, current_price)
            logger.info(f"Calculated amount {amount} for {duration_hours} hours at price {current_price}")

        # Calculate total cost and check funds
        total_cost = swarm_api.calculate_stamp_total_cost(amount, effective_depth)
        funds_check = swarm_api.check_sufficient_funds(total_cost)

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

        batch_id = swarm_api.purchase_postage_stamp(
            amount=amount,
            depth=effective_depth,
            label=stamp_request.label
        )

        # Record purchase time for propagation tracking
        record_purchase(batch_id)

        # Register stamp ownership
        x402_mode = getattr(request.state, 'x402_mode', None)
        x402_payer = getattr(request.state, 'x402_payer', None)
        if x402_mode == "paid" and x402_payer:
            stamp_ownership_manager.register_stamp(
                batch_id=batch_id,
                owner=x402_payer,
                mode="paid",
                source="direct_purchase"
            )
        else:
            stamp_ownership_manager.register_stamp(
                batch_id=batch_id,
                owner="shared",
                mode="free",
                source="direct_purchase"
            )

        return StampPurchaseResponse(
            batchID=batch_id,
            message="Postage stamp purchased successfully"
        )

    except HTTPException:
        raise  # Re-raise HTTP exceptions as-is
    except RequestException as e:
        logger.error(f"Failed to purchase stamp from Swarm API: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not purchase stamp. The Bee node may be unavailable."
        )
    except ValueError as e:
        logger.error(f"Invalid response from Swarm API during stamp purchase: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Invalid response from Swarm API. Please try again."
        )
    except Exception as e:
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
    stamp_id: str = Path(..., description="The Batch ID of the stamp to extend.", example="a1b2c3d4e5f6...", pattern=r"^[a-fA-F0-9]{64}$"),
    extension_request: StampExtensionRequest = ...
) -> Any:
    """
    Extends an existing postage stamp by adding more funds to it.

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
        all_stamps = swarm_api.get_all_stamps_processed()
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

        # Determine the amount to use
        if extension_request.amount is not None:
            # Legacy mode: use provided amount directly
            amount = extension_request.amount
        else:
            # Calculate amount from duration (default 25 hours)
            duration_hours = extension_request.duration_hours or 25
            chainstate = swarm_api.get_chainstate()
            current_price = int(chainstate["currentPrice"])
            amount = swarm_api.calculate_stamp_amount(duration_hours, current_price)
            logger.info(f"Calculated extension amount {amount} for {duration_hours} hours at price {current_price}")

        # Calculate total cost and check funds
        total_cost = swarm_api.calculate_stamp_total_cost(amount, stamp_depth)
        funds_check = swarm_api.check_sufficient_funds(total_cost)

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

        batch_id = swarm_api.extend_postage_stamp(
            stamp_id=stamp_id,
            amount=amount
        )

        return StampExtensionResponse(
            batchID=batch_id,
            message="Postage stamp extended successfully"
        )

    except HTTPException:
        raise  # Re-raise HTTP exceptions as-is
    except RequestException as e:
        logger.error(f"Failed to extend stamp {stamp_id} from Swarm API: {e}")
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
