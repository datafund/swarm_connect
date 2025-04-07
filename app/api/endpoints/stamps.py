# app/api/endpoints/stamps.py
from fastapi import APIRouter, HTTPException, Path, status
from typing import Any
import datetime
from requests.exceptions import RequestException
import logging

from app.services import swarm_api
from app.api.models.stamp import StampDetails

router = APIRouter()
logger = logging.getLogger(__name__)

@router.get(
    "/stamps/{stamp_id}",
    response_model=StampDetails,
    summary="Get Specific Swarm Stamp Batch Details",
    tags=["stamps"]
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
        all_stamps = swarm_api.get_all_stamps()
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
        batch_ttl = int(found_stamp.get("batchTTL", 0)) # Ensure TTL is an int, default to 0 if missing/invalid
        if batch_ttl < 0:
             logger.warning(f"Stamp {stamp_id} has negative TTL: {batch_ttl}. Treating as 0.")
             batch_ttl = 0

        # Calculate expiration based on current time + TTL
        # IMPORTANT: Swarm's batchTTL is typically relative to the block it was created in.
        # This calculation is based on the user request (current time + TTL).
        # Consider if you need creation time + TTL instead.
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        expiration_time_utc = now_utc + datetime.timedelta(seconds=batch_ttl)
        # Format: YYYY-MM-DD-HH-MM (UTC)
        expiration_str = expiration_time_utc.strftime('%Y-%m-%d-%H-%M')

        # Prepare the response using the Pydantic model for validation and structure
        # Ensure all required fields for StampDetails are present in found_stamp or handled
        response_data = StampDetails(
            batchID=found_stamp.get("batchID"),
            utilization=found_stamp.get("utilization"),
            usable=found_stamp.get("usable"),
            label=found_stamp.get("label"), # Handles None if missing
            depth=found_stamp.get("depth"),
            amount=str(found_stamp.get("amount")), # Ensure amount is string
            bucketDepth=found_stamp.get("bucketDepth"),
            blockNumber=found_stamp.get("blockNumber"),
            immutableFlag=found_stamp.get("immutableFlag"),
            batchTTL=batch_ttl, # Use the processed TTL
            exists=found_stamp.get("exists", True), # Default to True if field is missing, adjust as needed
            expectedExpiration=expiration_str
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
