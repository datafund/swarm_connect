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
    StampPurchaseRequestAdvanced,
    StampPurchaseResponse
)

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


@router.post(
    "/stamps",
    response_model=StampPurchaseResponse,
    summary="Create New Swarm Stamp Batch",
    tags=["stamps"]
)
async def create_stamp_batch(
    request: dict = Body(...)
) -> Any:
    """
    Create a new Swarm postage stamp batch.

    Supports two request formats:
    1. Time-based: Specify duration_days and data_size_mb for user-friendly interface
    2. Advanced: Specify amount and depth for direct control over technical parameters

    The API automatically converts time-based parameters to technical parameters when needed.
    """

    try:
        # Determine if this is a time-based or advanced request
        if 'duration_days' in request and request['duration_days'] is not None:
            # Time-based request - validate and convert to amount/depth
            time_request = StampPurchaseRequest(**request)
            logger.info(f"Processing time-based stamp request: {time_request.duration_days} days, {time_request.data_size_mb} MB")

            amount, depth = swarm_api.calculate_stamp_parameters(
                time_request.duration_days,
                time_request.data_size_mb
            )

            # Store original request values for response
            requested_duration_days = time_request.duration_days
            requested_data_size_mb = time_request.data_size_mb
            label = time_request.label
            immutable = time_request.immutable

        else:
            # Advanced request - validate and use provided amount/depth directly
            advanced_request = StampPurchaseRequestAdvanced(**request)
            logger.info(f"Processing advanced stamp request: amount={advanced_request.amount}, depth={advanced_request.depth}")

            amount = advanced_request.amount
            depth = advanced_request.depth
            requested_duration_days = None
            requested_data_size_mb = None
            label = advanced_request.label
            immutable = advanced_request.immutable

        # Estimate cost before creation
        estimated_cost = swarm_api.estimate_stamp_cost(amount, depth)

        # Create the stamp via Bee API
        creation_result = swarm_api.create_stamp(
            amount=amount,
            depth=depth,
            label=label,
            immutable=immutable
        )

        # Calculate estimated expiration
        # Note: This is an estimate based on current time + calculated duration
        if requested_duration_days:
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            expiration_time_utc = now_utc + datetime.timedelta(days=requested_duration_days)
            estimated_expiration = expiration_time_utc.strftime('%Y-%m-%d-%H-%M')
        else:
            # For advanced requests, we can't easily calculate duration
            # Could enhance this by reverse-calculating from amount/depth
            estimated_expiration = "N/A (advanced request)"

        # Prepare response
        response_data = StampPurchaseResponse(
            batchID=creation_result.get("batchID"),
            estimatedExpiration=estimated_expiration,
            estimatedCostBZZ=estimated_cost,
            actualAmount=amount,
            actualDepth=depth,
            txHash=creation_result.get("txHash"),
            blockNumber=creation_result.get("blockNumber"),
            label=label,
            immutable=immutable,
            requestedDurationDays=requested_duration_days,
            requestedDataSizeMB=requested_data_size_mb
        )

        logger.info(f"Successfully created stamp batch: {response_data.batchID}")
        return response_data

    except RequestException as e:
        logger.error(f"Failed to create stamp via Bee API: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not create stamp via Swarm Bee node: {e}"
        )
    except ValueError as e:
        logger.error(f"Invalid parameters for stamp creation: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid stamp creation parameters: {e}"
        )
    except Exception as e:
        logger.error(f"Unexpected error creating stamp: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while creating the stamp."
        )
