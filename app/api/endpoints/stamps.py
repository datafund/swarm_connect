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

    Creates a new postage stamp batch with the specified amount and depth.
    Optional label can be provided for easier identification.

    Args:
        stamp_request: Purchase request containing amount, depth, and optional label

    Returns:
        StampPurchaseResponse: Contains the new batch ID and success message

    Raises:
        HTTPException: 502 if Swarm API is unreachable, 500 for other errors
    """
    try:
        batch_id = swarm_api.purchase_postage_stamp(
            amount=stamp_request.amount,
            depth=stamp_request.depth,
            label=stamp_request.label
        )

        return StampPurchaseResponse(
            batchID=batch_id,
            message="Postage stamp purchased successfully"
        )

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

    This operation adds the specified amount to the existing stamp,
    extending its validity period and increasing its balance.

    Args:
        stamp_id: The batch ID of the stamp to extend
        extension_request: Extension request containing the additional amount

    Returns:
        StampExtensionResponse: Contains the batch ID and success message

    Raises:
        HTTPException: 502 if Swarm API is unreachable, 500 for other errors
    """
    try:
        batch_id = swarm_api.extend_postage_stamp(
            stamp_id=stamp_id,
            amount=extension_request.amount
        )

        return StampExtensionResponse(
            batchID=batch_id,
            message="Postage stamp extended successfully"
        )

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
