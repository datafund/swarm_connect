# app/api/endpoints/stamps.py
from fastapi import APIRouter, HTTPException, Path, status, Body
from typing import Any, Union
import datetime
from requests.exceptions import RequestException
import logging

from app.services import swarm_api
from app.api.models.stamp import (
    StampDetails,
    StampPurchaseRequest,
    StampPurchaseResponse,
    StampExtensionRequest,
    StampExtensionResponse,
    StampListResponse
)

router = APIRouter()
logger = logging.getLogger(__name__)

@router.get(
    "/",
    response_model=StampListResponse,
    summary="List All Swarm Stamp Batches"
)
async def list_stamps() -> Any:
    """
    Retrieves a list of all postage stamp batches from the Swarm network.

    Fetches all available stamp batches, processes them to calculate expiration times,
    and returns a comprehensive list with stamp details.

    Returns:
        StampListResponse: Contains list of all stamps and total count

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

        return StampListResponse(
            stamps=stamp_details,
            total_count=len(stamp_details)
        )

    except RequestException as e:
        logger.error(f"Failed to retrieve stamps from Swarm API: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not fetch stamp data from the Swarm Bee node: {e}"
        )
    except Exception as e:
        logger.error(f"Unexpected error fetching stamps: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while fetching stamp data."
        )


@router.get(
    "/{stamp_id}",
    response_model=StampDetails,
    summary="Get Specific Swarm Stamp Batch Details"
)
async def get_stamp_details(
    stamp_id: str = Path(..., description="The Batch ID of the Swarm stamp to retrieve.", example="a1b2c3d4e5f6...")
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
            detail=f"Could not fetch data from the Swarm Bee node: {e}"
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
            usable=found_stamp.get("usable"),
            label=found_stamp.get("label"),
            expectedExpiration=found_stamp.get("expectedExpiration"),
            local=found_stamp.get("local")
        )
        return response_data

    except KeyError as e:
        logger.error(f"Missing expected key '{e}' in Swarm API response for stamp {stamp_id}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Incomplete data received from Swarm API for stamp {stamp_id}. Missing key: {e}."
        )
    except (ValueError, TypeError) as e:
        logger.error(f"Data type error processing Swarm API response for stamp {stamp_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Invalid data format received from Swarm API for stamp {stamp_id}."
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
    stamp_request: StampPurchaseRequest
) -> Any:
    """
    Purchases a new postage stamp from the Swarm network.

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
        HTTPException: 400 if insufficient funds, 502 if Swarm API is unreachable
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
            detail=f"Could not purchase stamp from the Swarm Bee node: {e}"
        )
    except ValueError as e:
        logger.error(f"Invalid response from Swarm API during stamp purchase: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Invalid response from Swarm API: {e}"
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
    stamp_id: str = Path(..., description="The Batch ID of the stamp to extend.", example="a1b2c3d4e5f6..."),
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
            detail=f"Could not extend stamp from the Swarm Bee node: {e}"
        )
    except ValueError as e:
        logger.error(f"Invalid response from Swarm API during stamp extension: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Invalid response from Swarm API: {e}"
        )
    except Exception as e:
        logger.error(f"Unexpected error during stamp extension for {stamp_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while extending the stamp."
        )
