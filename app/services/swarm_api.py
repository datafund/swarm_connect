# app/services/swarm_api.py
import requests
from requests.exceptions import RequestException
import logging
from typing import List, Dict, Any, Optional
from urllib.parse import urljoin

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
    api_url = urljoin(str(settings.SWARM_BEE_API_URL), "batches")
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


def get_local_stamps() -> List[Dict[str, Any]]:
    """
    Fetches local postage stamp data from the configured Swarm Bee node.
    This endpoint provides richer information including utilization, usable status,
    owner information, and actual amounts.

    Returns:
        A list of dictionaries representing local stamp data with detailed information.
        Returns an empty list if the request fails or no stamps are found.

    Raises:
        RequestException: If the HTTP request to the Swarm API fails.
    """
    api_url = urljoin(str(settings.SWARM_BEE_API_URL), "stamps")
    try:
        response = requests.get(api_url, timeout=10)
        response.raise_for_status()

        data = response.json()
        if isinstance(data, dict) and "stamps" in data:
            stamps = data.get("stamps")
            if isinstance(stamps, list):
                return stamps
            else:
                logger.warning(f"Local stamps API response 'stamps' field is not a list: {type(stamps)}")
                return []
        elif isinstance(data, list):
            # Handle case where API directly returns a list
            return data
        else:
            logger.warning(f"Unexpected data structure from local stamps API: {type(data)}")
            return []

    except RequestException as e:
        logger.warning(f"Error fetching local stamps from Swarm API ({api_url}): {e}")
        # Don't re-raise here - we want to continue even if local stamps fail
        return []
    except Exception as e:
        logger.warning(f"An unexpected error occurred while processing local stamps response: {e}")
        return []


def purchase_postage_stamp(amount: int, depth: int, label: Optional[str] = None) -> str:
    """
    Purchases a new postage stamp from the configured Swarm Bee node.

    Args:
        amount: The amount of the postage stamp in wei
        depth: The depth of the postage stamp
        label: Optional user-defined label for the stamp

    Returns:
        The batchID of the purchased stamp

    Raises:
        RequestException: If the HTTP request to the Swarm API fails
        ValueError: If the response is malformed or missing expected fields
    """
    api_url = urljoin(str(settings.SWARM_BEE_API_URL), f"stamps/{amount}/{depth}")
    headers = {"Content-Type": "application/json"}

    # Prepare request body if label is provided
    request_body = {}
    if label:
        request_body["label"] = label

    try:
        if request_body:
            response = requests.post(api_url, json=request_body, headers=headers, timeout=120)
        else:
            response = requests.post(api_url, headers=headers, timeout=120)

        response.raise_for_status()
        response_json = response.json()

        batch_id = response_json.get("batchID")
        if not batch_id:
            raise ValueError("API Response missing 'batchID' from purchase")

        logger.info(f"Successfully purchased stamp with batch ID: {batch_id}")
        return batch_id

    except requests.exceptions.RequestException as e:
        logger.error(f"Error purchasing stamp from Swarm API ({api_url}): {e}")
        raise
    except (ValueError, KeyError) as e:
        logger.error(f"Error parsing stamp purchase response: {e}")
        raise ValueError(f"Could not parse stamp purchase response: {e}") from e


def extend_postage_stamp(stamp_id: str, amount: int) -> str:
    """
    Extends an existing postage stamp by adding more funds to it.

    Args:
        stamp_id: The batch ID of the stamp to extend
        amount: Additional amount to add to the stamp in wei

    Returns:
        The batchID of the extended stamp (should be same as input stamp_id)

    Raises:
        RequestException: If the HTTP request to the Swarm API fails
        ValueError: If the response is malformed or missing expected fields
    """
    api_url = urljoin(str(settings.SWARM_BEE_API_URL), f"stamps/topup/{stamp_id}/{amount}")
    headers = {"Content-Type": "application/json"}

    try:
        response = requests.patch(api_url, headers=headers, timeout=120)
        response.raise_for_status()

        # The topup endpoint typically returns the updated batch information
        # We'll extract the batchID to confirm the operation
        response_json = response.json()
        batch_id = response_json.get("batchID", stamp_id)  # Fallback to original stamp_id

        logger.info(f"Successfully extended stamp {stamp_id} with amount {amount}")
        return batch_id

    except requests.exceptions.RequestException as e:
        logger.error(f"Error extending stamp {stamp_id} from Swarm API ({api_url}): {e}")
        raise
    except (ValueError, KeyError) as e:
        logger.error(f"Error parsing stamp extension response for {stamp_id}: {e}")
        raise ValueError(f"Could not parse stamp extension response: {e}") from e


def calculate_usable_status(stamp: Dict[str, Any]) -> bool:
    """
    Calculates if a stamp is usable based on available data.
    A stamp is considered usable if:
    1. It has a positive TTL (not expired)
    2. It exists
    3. It's not immutable or has reasonable depth for uploads

    Args:
        stamp: The stamp data from /batches endpoint

    Returns:
        Boolean indicating if the stamp is usable
    """
    try:
        # Check if stamp exists
        if not stamp.get("exists", True):
            return False

        # Check TTL - if TTL is very low, stamp is likely expired or about to expire
        batch_ttl = int(stamp.get("batchTTL", 0))
        if batch_ttl <= 0:
            return False

        # Check if it's immutable - immutable stamps may have restrictions
        is_immutable = stamp.get("immutableFlag", False) or stamp.get("immutable", False)

        # For immutable stamps, require higher TTL threshold for safety
        min_ttl = 3600 if is_immutable else 60  # 1 hour for immutable, 1 minute for regular

        if batch_ttl < min_ttl:
            return False

        # Additional checks could include:
        # - Depth validation (reasonable depth for uploads)
        # - Amount validation (sufficient balance)
        depth = stamp.get("depth", 0)
        if depth < 16 or depth > 32:  # Reasonable depth range
            return False

        return True

    except (ValueError, TypeError) as e:
        logger.warning(f"Error calculating usable status for stamp: {e}")
        return False


def merge_stamp_data(global_stamp: Dict[str, Any], local_stamp: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Merges global stamp data with local stamp data, preferring local data when available.

    Args:
        global_stamp: Stamp data from /batches endpoint (global view)
        local_stamp: Stamp data from /stamps endpoint (local node view) or None

    Returns:
        Merged stamp data with priority given to local information
    """
    merged = global_stamp.copy()

    if local_stamp:
        # Prefer local data for these fields as they're more accurate
        if local_stamp.get("utilization") is not None:
            merged["utilization"] = local_stamp["utilization"]
        if local_stamp.get("usable") is not None:
            merged["usable"] = local_stamp["usable"]
        if local_stamp.get("label") is not None:
            merged["label"] = local_stamp["label"]
        if local_stamp.get("amount"):
            merged["amount"] = str(local_stamp["amount"])
        if local_stamp.get("owner"):
            merged["owner"] = local_stamp["owner"]
        # Local stamps might have more current blockNumber, exists, etc.
        if local_stamp.get("blockNumber") is not None:
            merged["blockNumber"] = local_stamp["blockNumber"]
        if local_stamp.get("exists") is not None:
            merged["exists"] = local_stamp["exists"]
        if local_stamp.get("immutableFlag") is not None:
            merged["immutableFlag"] = local_stamp["immutableFlag"]
        if local_stamp.get("batchTTL") is not None:
            merged["batchTTL"] = local_stamp["batchTTL"]

    return merged


def get_all_stamps_processed() -> List[Dict[str, Any]]:
    """
    Fetches all postage stamp batches and processes them with expiration calculations.
    Merges global batch data with local stamp information for comprehensive results.

    Returns:
        A list of processed stamp dictionaries with merged global/local data,
        calculated expiration times, and accurate usable status.

    Raises:
        RequestException: If the HTTP request to the Swarm API fails.
    """
    import datetime

    # Get stamps data from both endpoints
    global_stamps = get_all_stamps()  # /batches endpoint
    local_stamps = get_local_stamps()  # /stamps endpoint

    # Create a lookup dictionary for local stamps by batchID
    local_stamps_dict = {stamp.get("batchID"): stamp for stamp in local_stamps if stamp.get("batchID")}

    processed_stamps = []

    for global_stamp in global_stamps:
        try:
            batch_id = global_stamp.get("batchID")
            if not batch_id:
                logger.warning("Skipping stamp with missing batchID")
                continue

            # Find corresponding local stamp data
            local_stamp = local_stamps_dict.get(batch_id)

            # Merge global and local data
            merged_stamp = merge_stamp_data(global_stamp, local_stamp)

            # Calculate expiration time
            batch_ttl = int(merged_stamp.get("batchTTL", 0))
            if batch_ttl < 0:
                logger.warning(f"Stamp {batch_id} has negative TTL: {batch_ttl}. Treating as 0.")
                batch_ttl = 0

            # Calculate expiration based on current time + TTL
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            expiration_time_utc = now_utc + datetime.timedelta(seconds=batch_ttl)
            expiration_str = expiration_time_utc.strftime('%Y-%m-%d-%H-%M')

            # Use local usable status if available, otherwise calculate
            usable = merged_stamp.get("usable")
            if usable is None:
                usable = calculate_usable_status(merged_stamp)

            # Create processed stamp data
            processed_stamp = {
                "batchID": batch_id,
                "utilization": merged_stamp.get("utilization"),
                "usable": usable,
                "label": merged_stamp.get("label"),
                "depth": merged_stamp.get("depth"),
                "amount": str(merged_stamp.get("amount", "")),  # Ensure amount is string
                "bucketDepth": merged_stamp.get("bucketDepth"),
                "blockNumber": merged_stamp.get("blockNumber"),
                "immutableFlag": merged_stamp.get("immutableFlag"),
                "batchTTL": batch_ttl,
                "exists": merged_stamp.get("exists", True),
                "owner": merged_stamp.get("owner"),  # New field from local data
                "expectedExpiration": expiration_str,
                "local": local_stamp is not None  # True if stamp found in local data
            }
            processed_stamps.append(processed_stamp)

        except (ValueError, TypeError, KeyError) as e:
            logger.warning(f"Skipping malformed stamp data: {e}")
            continue

    return processed_stamps


def upload_data_to_swarm(data: bytes, stamp_id: str, content_type: str = "application/json") -> str:
    """
    Uploads data to the Swarm network using the configured Bee node.

    Args:
        data: The data to upload as bytes
        stamp_id: The postage stamp batch ID to use for the upload
        content_type: MIME type of the content

    Returns:
        The Swarm reference hash of the uploaded data

    Raises:
        RequestException: If the HTTP request to the Swarm API fails
        ValueError: If the response is malformed or missing expected fields
    """
    api_url = urljoin(str(settings.SWARM_BEE_API_URL), "bzz")
    headers = {
        "Swarm-Postage-Batch-Id": stamp_id.lower(),
        "Content-Type": content_type
    }

    try:
        response = requests.post(api_url, data=data, headers=headers, timeout=60)
        response.raise_for_status()

        response_json = response.json()
        reference = response_json.get("reference")
        if not reference:
            raise ValueError("API Response missing 'reference' from upload")

        logger.info(f"Successfully uploaded data to Swarm with reference: {reference}")
        return reference

    except requests.exceptions.RequestException as e:
        logger.error(f"Error uploading data to Swarm API ({api_url}): {e}")
        raise
    except (ValueError, KeyError) as e:
        logger.error(f"Error parsing data upload response: {e}")
        raise ValueError(f"Could not parse data upload response: {e}") from e


def download_data_from_swarm(reference: str) -> bytes:
    """
    Downloads data from the Swarm network using a reference hash.

    Args:
        reference: The Swarm reference hash of the data to download

    Returns:
        The downloaded data as bytes

    Raises:
        RequestException: If the HTTP request to the Swarm API fails
        FileNotFoundError: If the data is not found (404)
    """
    api_url = urljoin(str(settings.SWARM_BEE_API_URL), f"bzz/{reference.lower()}")

    try:
        response = requests.get(api_url, timeout=60)

        if response.status_code == 404:
            raise FileNotFoundError(f"Data not found on Swarm at reference {reference}")

        response.raise_for_status()

        logger.info(f"Successfully downloaded {len(response.content)} bytes from Swarm reference: {reference}")
        return response.content

    except requests.exceptions.RequestException as e:
        logger.error(f"Error downloading data from Swarm API ({api_url}): {e}")
        raise
