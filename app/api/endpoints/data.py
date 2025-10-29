# app/api/endpoints/data.py
import base64
import json
import logging
from fastapi import APIRouter, HTTPException, Path, Request, Body, File, UploadFile
from fastapi.responses import Response
from requests.exceptions import RequestException

from app.api.models.data import (
    DataUploadRequest,
    DataUploadResponse,
    DataDownloadResponse
)
from app.services.swarm_api import upload_data_to_swarm, download_data_from_swarm

logger = logging.getLogger(__name__)
router = APIRouter()


def _detect_content_type_and_filename(data_bytes: bytes, reference: str) -> tuple[str, str]:
    """
    Detect content type and generate user-friendly filename for downloads.

    Args:
        data_bytes: The downloaded data
        reference: Swarm reference hash

    Returns:
        Tuple of (content_type, filename)
    """
    # Try to detect if it's JSON
    try:
        json.loads(data_bytes.decode('utf-8'))
        # It's valid JSON - use JSON content type and provenance filename
        short_ref = reference[:8]  # First 8 chars of reference for filename
        return "application/json", f"provenance-{short_ref}.json"
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass

    # Check for common binary file signatures
    if data_bytes.startswith(b'\x89PNG'):
        return "image/png", f"image-{reference[:8]}.png"
    elif data_bytes.startswith(b'\xFF\xD8\xFF'):
        return "image/jpeg", f"image-{reference[:8]}.jpg"
    elif data_bytes.startswith(b'%PDF'):
        return "application/pdf", f"document-{reference[:8]}.pdf"
    elif data_bytes.startswith(b'GIF8'):
        return "image/gif", f"image-{reference[:8]}.gif"

    # Check if it's likely text
    try:
        data_bytes.decode('utf-8')
        # It's valid UTF-8 text
        return "text/plain", f"text-{reference[:8]}.txt"
    except UnicodeDecodeError:
        pass

    # Default to binary with .bin extension
    return "application/octet-stream", f"data-{reference[:8]}.bin"


@router.post("/", response_model=DataUploadResponse)
async def upload_data(
    stamp_id: str,
    content_type: str = "application/json",
    file: UploadFile = File(...)
):
    """
    Upload data to the Swarm network via the configured Bee node.

    Upload a file containing JSON data (default) or raw binary data.
    For JSON files: use Content-Type: application/json
    For binary files: use appropriate Content-Type (e.g., application/octet-stream)

    Example JSON structure (SWIP-compliant):
    {
        "content_hash": "sha256:9f86d...",
        "provenance_standard": "DaTA v1.0.0",
        "encryption": "none",
        "data": { ... provenance data ... },
        "stamp_id": "0xfe2f..."
    }
    """
    try:
        # Read file content as bytes
        data_bytes = await file.read()

        # Upload to Swarm
        reference = upload_data_to_swarm(
            data=data_bytes,
            stamp_id=stamp_id,
            content_type=content_type
        )

        return DataUploadResponse(
            reference=reference,
            message=f"File '{file.filename}' uploaded successfully"
        )

    except RequestException as e:
        logger.error(f"Swarm API error during upload: {e}")
        raise HTTPException(status_code=502, detail=f"Failed to upload data to Swarm: {e}")
    except ValueError as e:
        logger.error(f"Data processing error during upload: {e}")
        raise HTTPException(status_code=400, detail=f"Data upload error: {e}")
    except Exception as e:
        logger.error(f"Unexpected error during upload: {e}")
        raise HTTPException(status_code=500, detail="Internal server error during upload")


@router.get("/{reference}")
async def download_data(
    reference: str = Path(..., description="Swarm reference hash of the data to download")
):
    """
    Download data from the Swarm network and return it as raw bytes.

    This endpoint returns the data directly without JSON wrapping.
    """
    try:
        # Download from Swarm
        data_bytes = download_data_from_swarm(reference)

        # Detect content type and generate user-friendly filename
        content_type, filename = _detect_content_type_and_filename(data_bytes, reference)

        # Return raw data with user-friendly filename
        return Response(
            content=data_bytes,
            media_type=content_type,
            headers={
                "Content-Length": str(len(data_bytes)),
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Swarm-Reference": reference
            }
        )

    except FileNotFoundError as e:
        logger.warning(f"Data not found for reference {reference}: {e}")
        raise HTTPException(status_code=404, detail=f"Data not found: {e}")
    except RequestException as e:
        logger.error(f"Swarm API error during download: {e}")
        raise HTTPException(status_code=502, detail=f"Failed to download data from Swarm: {e}")
    except Exception as e:
        logger.error(f"Unexpected error during download: {e}")
        raise HTTPException(status_code=500, detail="Internal server error during download")



@router.get("/{reference}/json", response_model=DataDownloadResponse)
async def download_data_json(
    reference: str = Path(..., description="Swarm reference hash of the data to download")
):
    """
    Download data from the Swarm network and return it as JSON with metadata.

    Returns the data as base64-encoded content with metadata for API clients.
    """
    try:
        # Download from Swarm
        data_bytes = download_data_from_swarm(reference)

        # Detect content type and generate filename for metadata
        content_type, filename = _detect_content_type_and_filename(data_bytes, reference)

        # Encode as base64
        data_b64 = base64.b64encode(data_bytes).decode('utf-8')

        return DataDownloadResponse(
            data=data_b64,
            content_type=content_type,
            size=len(data_bytes),
            reference=reference
        )

    except FileNotFoundError as e:
        logger.warning(f"Data not found for reference {reference}: {e}")
        raise HTTPException(status_code=404, detail=f"Data not found: {e}")
    except RequestException as e:
        logger.error(f"Swarm API error during download: {e}")
        raise HTTPException(status_code=502, detail=f"Failed to download data from Swarm: {e}")
    except Exception as e:
        logger.error(f"Unexpected error during download: {e}")
        raise HTTPException(status_code=500, detail="Internal server error during download")