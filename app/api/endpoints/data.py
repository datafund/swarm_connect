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

        # Return raw data
        return Response(
            content=data_bytes,
            media_type="application/octet-stream",
            headers={
                "Content-Length": str(len(data_bytes)),
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

        # Encode as base64
        data_b64 = base64.b64encode(data_bytes).decode('utf-8')

        return DataDownloadResponse(
            data=data_b64,
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