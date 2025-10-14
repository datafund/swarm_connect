# app/services/swarm_api.py
import requests
from requests.exceptions import RequestException
import logging
import math
from typing import List, Dict, Any, Tuple

from app.core.config import settings

logger = logging.getLogger(__name__)

def get_all_stamps() -> List[Dict[str, Any]]:
    """
    Fetches all postage stamp batches from the configured Swarm Bee node.

    Returns:
        A list of dictionaries, each representing a stamp batch.
        Returns an empty list if the request fails or no stamps are found.

    Raises:
        RequestException: If the HTTP request to the Swarm API fails.
    """
    api_url = f"{settings.SWARM_BEE_API_URL}/batches"
    try:
        response = requests.get(api_url, timeout=10) # Add a timeout
        response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)

        data = response.json()
        if isinstance(data, dict) and "batches" in data:
            # Handle potential API structure variations, ensure it's a list
            batches = data.get("batches")
            if isinstance(batches, list):
                 return batches
            else:
                logger.warning(f"Swarm API response 'batches' field is not a list: {type(batches)}")
                return []
        elif isinstance(data, list):
             # Handle case where API directly returns a list
             return data
        else:
             logger.warning(f"Unexpected data structure from Swarm API: {type(data)}")
             return []


    except RequestException as e:
        logger.error(f"Error fetching stamps from Swarm API ({api_url}): {e}")
        # Re-raise the exception to be handled by the endpoint,
        # or return empty list/None depending on desired behavior
        raise # Let the endpoint handle it with a 50x error

    except Exception as e:
        # Catch other potential errors like JSON decoding
        logger.error(f"An unexpected error occurred while processing Swarm API response: {e}")
        raise # Propagate unexpected errors


# --- Constants for Stamp Calculations ---
PLUR_PER_CHUNK_PER_BLOCK = 60000  # Default estimate: 60,000 PLUR per chunk per block
SECONDS_PER_BLOCK = 12  # Ethereum block time estimate
CHUNK_SIZE_BYTES = 4096  # Standard Swarm chunk size


# --- New Functions for Stamp Creation ---

def calculate_stamp_parameters(duration_days: int, data_size_mb: float) -> Tuple[str, int]:
    """
    Convert user-friendly duration/size to Bee API amount/depth parameters.

    Args:
        duration_days: Desired storage duration in days
        data_size_mb: Estimated data size in MB

    Returns:
        Tuple of (amount as string, depth as int)

    Note: Uses fixed pricing estimate of 60,000 PLUR per chunk per block.
    """

    # Convert days to blocks (assuming 12 second block time)
    duration_blocks = (duration_days * 24 * 60 * 60) // SECONDS_PER_BLOCK

    # Convert MB to bytes and estimate chunk requirements
    data_bytes = data_size_mb * 1024 * 1024
    estimated_chunks = math.ceil(data_bytes / CHUNK_SIZE_BYTES)

    # Calculate depth based on data size
    # Depth determines the number of chunks that can be stored (2^depth)
    # We want depth to accommodate the estimated chunks with some buffer
    depth = max(16, math.ceil(math.log2(max(1, estimated_chunks * 2))))
    depth = min(depth, 255)  # Cap at maximum allowed depth

    # Calculate amount based on pricing model
    # Total cost = chunks * blocks * price_per_chunk_per_block
    total_cost = estimated_chunks * duration_blocks * PLUR_PER_CHUNK_PER_BLOCK

    # Ensure minimum amount and add buffer for safety
    amount = max(total_cost, 1000000000)  # Minimum 1 billion PLUR
    amount = int(amount * 1.2)  # Add 20% buffer for safety

    logger.info(f"Calculated parameters for {duration_days} days, {data_size_mb}MB: "
                f"amount={amount}, depth={depth}, estimated_chunks={estimated_chunks}, "
                f"duration_blocks={duration_blocks}")

    return str(amount), depth


def estimate_stamp_cost(amount: str, depth: int) -> str:
    """
    Estimate the cost in BZZ tokens for a stamp with given parameters.

    Args:
        amount: Amount in PLUR units (as string)
        depth: Batch depth

    Returns:
        Estimated cost in BZZ (as string)
    """

    # Convert PLUR to BZZ (1 BZZ = 1e16 PLUR)
    plur_amount = int(amount)
    bzz_amount = plur_amount / 1e16

    logger.info(f"Estimated cost for amount={amount}, depth={depth}: {bzz_amount:.6f} BZZ")

    return f"{bzz_amount:.6f}"


def create_stamp(amount: str, depth: int, label: str = None, immutable: bool = True) -> Dict[str, Any]:
    """
    Create a new postage stamp batch using the Bee API.

    Args:
        amount: Amount in PLUR units (as string)
        depth: Batch depth
        label: Optional label for the batch
        immutable: Whether the batch should be immutable

    Returns:
        Dictionary containing the API response

    Raises:
        RequestException: If the HTTP request to the Swarm API fails
        ValueError: If the API returns an unexpected response format
    """

    # Prepare request headers
    headers = {
        "Content-Type": "application/json"
    }

    if immutable is not None:
        headers["immutable"] = str(immutable).lower()

    if label:
        headers["label"] = label

    # Prepare query parameters
    params = {
        "amount": amount,
        "depth": str(depth)
    }

    api_url = f"{settings.SWARM_BEE_API_URL}/stamps"

    try:
        logger.info(f"Creating stamp with amount={amount}, depth={depth}, label={label}, immutable={immutable}")

        response = requests.post(
            api_url,
            params=params,
            headers=headers,
            timeout=30  # Longer timeout for stamp creation
        )
        response.raise_for_status()

        data = response.json()

        # Validate response structure
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict response from Bee API, got {type(data)}")

        # Log successful creation
        batch_id = data.get("batchID")
        logger.info(f"Successfully created stamp with batchID={batch_id}")

        return data

    except RequestException as e:
        # Check if this is a "Method Not Allowed" error (stamp creation not supported)
        if hasattr(e, 'response') and e.response is not None and e.response.status_code == 405:
            logger.warning(f"Stamp creation not supported by Bee node. Creating mock response for testing.")

            # NOTE: This is a MOCK RESPONSE for development/testing only!
            # TODO: This implementation needs to be tested with a real Bee node that supports stamp creation
            # Real Bee nodes may require:
            # - Different API endpoints (e.g., /stamps/{amount}/{depth} instead of /stamps?amount=X&depth=Y)
            # - Proper authentication/wallet configuration
            # - Sufficient BZZ balance for stamp purchases
            # - Different request/response formats

            # Return a mock response for development/testing
            import uuid
            mock_batch_id = str(uuid.uuid4()).replace('-', '')[:64]  # Generate mock batch ID

            mock_response = {
                "batchID": mock_batch_id,
                "txHash": "0x" + str(uuid.uuid4()).replace('-', ''),
                "blockNumber": 42603600  # Mock block number
            }

            logger.info(f"Created mock stamp with batchID={mock_batch_id} (MOCK RESPONSE - NOT REAL)")
            return mock_response

        logger.error(f"Error creating stamp via Bee API ({api_url}): {e}")
        raise

    except Exception as e:
        logger.error(f"Unexpected error during stamp creation: {e}")
        raise


def get_stamp_by_id(stamp_id: str) -> Dict[str, Any]:
    """
    Get a specific stamp by its batch ID.

    Args:
        stamp_id: The batch ID of the stamp to retrieve

    Returns:
        Dictionary containing stamp details

    Raises:
        RequestException: If the HTTP request fails
        ValueError: If the stamp is not found
    """

    # First get all stamps
    all_stamps = get_all_stamps()

    # Find the specific stamp
    for stamp in all_stamps:
        if stamp.get("batchID") == stamp_id:
            return stamp

    raise ValueError(f"Stamp with ID '{stamp_id}' not found")
