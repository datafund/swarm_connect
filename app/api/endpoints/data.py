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
    DataDownloadResponse,
    ManifestUploadResponse
)
from app.services.swarm_api import (
    upload_data_to_swarm,
    download_data_from_swarm,
    upload_collection_to_swarm,
    validate_tar,
    count_tar_files
)

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

    **Requirements**:
    - Upload as **multipart/form-data** with a `file` field
    - Valid `stamp_id` query parameter required
    - Optional `content_type` parameter (defaults to application/json)

    **Usage Examples**:
    ```bash
    # Upload JSON file
    curl -X POST "http://localhost:8000/api/v1/data/?stamp_id=ABC123&content_type=application/json" \\
         -F "file=@data.json"

    # Upload binary file
    curl -X POST "http://localhost:8000/api/v1/data/?stamp_id=ABC123&content_type=image/png" \\
         -F "file=@image.png"
    ```

    **Supported Content Types**: JSON, text, images, PDFs, or any binary data.

    **SWIP-Compliant JSON Example**:
    ```json
    {
        "content_hash": "sha256:9f86d...",
        "provenance_standard": "DaTA v1.0.0",
        "encryption": "none",
        "data": { ... provenance data ... },
        "stamp_id": "0xfe2f..."
    }
    ```
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
    Download data from the Swarm network as a file (triggers browser download).

    **Use case**: End users downloading files, browser integration

    **Features**:
    - User-friendly filenames (provenance-abc12345.json, image-def67890.png, etc.)
    - Auto-detected content types (application/json, image/png, text/plain, etc.)
    - Proper download headers for browsers
    - Direct binary streaming (no JSON wrapper)
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
    Download data from the Swarm network as JSON with metadata (for API clients).

    **Use case**: Web apps, mobile apps, API integrations needing metadata

    **Response format**:
    ```json
    {
        "data": "base64-encoded-content",
        "content_type": "application/json",
        "size": 2048,
        "reference": "abc123..."
    }
    ```

    **Benefits**:
    - Get file metadata without triggering browser download
    - Programmatic access to file info and content
    - Base64 encoding for JSON-safe binary data transport
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


@router.post("/manifest", response_model=ManifestUploadResponse)
async def upload_manifest(
    stamp_id: str,
    file: UploadFile = File(...)
):
    """
    Upload a TAR archive as a collection/manifest to the Swarm network.

    This endpoint bundles multiple files in a single upload for improved performance.
    The TAR archive is uploaded with the `Swarm-Collection: true` header, creating
    a manifest that maps file paths to their individual Swarm references.

    **Performance benefit**: Uploading 50 files as a TAR manifest takes ~500ms vs
    ~14 seconds for sequential individual uploads (15x improvement).

    **Requirements**:
    - Upload as **multipart/form-data** with a `file` field containing a TAR archive
    - Valid `stamp_id` query parameter required
    - TAR must contain at least one file

    **Usage Examples**:
    ```bash
    # Create TAR archive
    tar -cvf files.tar file1.json file2.json file3.json

    # Upload TAR as collection
    curl -X POST "http://localhost:8000/api/v1/data/manifest?stamp_id=ABC123" \\
         -F "file=@files.tar"
    ```

    **Accessing individual files**:
    After upload, individual files can be accessed via:
    - `GET /bzz/{manifest_reference}/{file_path}` on the Bee node directly
    - Or use bee-js `MantarayNode.unmarshal()` to extract individual file references

    **Response**:
    ```json
    {
        "reference": "a1b2c3...",
        "file_count": 50,
        "message": "Collection uploaded successfully"
    }
    ```
    """
    try:
        # Read TAR file content
        tar_bytes = await file.read()

        # Validate TAR archive
        try:
            validate_tar(tar_bytes)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        # Count files for response
        file_count = count_tar_files(tar_bytes)

        # Upload to Swarm as collection
        reference = upload_collection_to_swarm(tar_bytes, stamp_id)

        return ManifestUploadResponse(
            reference=reference,
            file_count=file_count,
            message=f"Collection uploaded successfully with {file_count} files"
        )

    except HTTPException:
        # Re-raise HTTP exceptions as-is
        raise
    except RequestException as e:
        logger.error(f"Swarm API error during manifest upload: {e}")
        raise HTTPException(status_code=502, detail=f"Failed to upload collection to Swarm: {e}")
    except ValueError as e:
        logger.error(f"Data processing error during manifest upload: {e}")
        raise HTTPException(status_code=400, detail=f"Manifest upload error: {e}")
    except Exception as e:
        logger.error(f"Unexpected error during manifest upload: {e}")
        raise HTTPException(status_code=500, detail="Internal server error during manifest upload")