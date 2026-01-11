# app/api/endpoints/data.py
import base64
import json
import logging
import time
from fastapi import APIRouter, HTTPException, Path, Request, Body, File, UploadFile
from fastapi.responses import Response
from requests.exceptions import RequestException

from app.api.models.data import (
    DataUploadRequest,
    DataUploadResponse,
    DataDownloadResponse,
    ManifestUploadResponse,
    UploadTiming,
    ManifestUploadTiming
)
from app.services.swarm_api import (
    upload_data_to_swarm,
    download_data_from_swarm,
    upload_collection_to_swarm,
    validate_tar,
    count_tar_files,
    validate_stamp_for_upload,
    check_upload_failure_reason,
    StampValidationError
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _build_server_timing_header(timing_dict: dict) -> str:
    """
    Build W3C Server-Timing header value from timing dictionary.

    Format: metric;dur=value, metric2;dur=value2
    Example: file-read;dur=0.5, bee-upload;dur=123.4, total;dur=124.1
    """
    parts = []
    for key, value in timing_dict.items():
        if value is not None:
            # Convert snake_case to kebab-case for header
            metric_name = key.replace("_", "-")
            parts.append(f"{metric_name};dur={value:.2f}")
    return ", ".join(parts)


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
    validate_stamp: bool = False,
    deferred: bool = False,
    include_timing: bool = False,
    file: UploadFile = File(...)
):
    """
    Upload data to the Swarm network via the configured Bee node.

    **Requirements**:
    - Upload as **multipart/form-data** with a `file` field
    - Valid `stamp_id` query parameter required
    - Optional `content_type` parameter (defaults to application/json)
    - Optional `validate_stamp` parameter (defaults to false)
    - Optional `deferred` parameter (defaults to false)
    - Optional `include_timing` parameter (defaults to false)

    **Stamp Validation** (opt-in with `validate_stamp=true`):
    When enabled, validates the stamp before upload:
    - Returns 400 if stamp is at 100% utilization (full)
    - Returns 400 if stamp is not usable (expired, invalid)
    - Returns 404 if stamp is not found

    **Deferred Mode** (opt-in with `deferred=true`):
    - `deferred=false` (default): Direct upload - chunks uploaded directly to network,
      ensuring immediate availability. Safer for gateway use cases.
    - `deferred=true`: Deferred upload - data goes to local node first, then syncs to
      network asynchronously. Faster upload response but data may not be immediately
      retrievable from the network.

    **Performance Timing** (opt-in with `include_timing=true`):
    When enabled, includes timing breakdown in response and Server-Timing header:
    - `stamp_validate_ms`: Time validating stamp (only if validate_stamp=true)
    - `file_read_ms`: Time reading uploaded file
    - `bee_upload_ms`: Time uploading to Bee node
    - `total_ms`: Total request processing time

    **Usage Examples**:
    ```bash
    # Upload JSON file (direct mode, default)
    curl -X POST "http://localhost:8000/api/v1/data/?stamp_id=ABC123&content_type=application/json" \\
         -F "file=@data.json"

    # Upload with deferred mode (faster response)
    curl -X POST "http://localhost:8000/api/v1/data/?stamp_id=ABC123&deferred=true" \\
         -F "file=@data.json"

    # Upload with pre-validation
    curl -X POST "http://localhost:8000/api/v1/data/?stamp_id=ABC123&validate_stamp=true" \\
         -F "file=@data.json"

    # Upload with timing information
    curl -X POST "http://localhost:8000/api/v1/data/?stamp_id=ABC123&include_timing=true" \\
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
    start_time = time.perf_counter()
    stamp_validate_ms = None
    file_read_ms = None
    bee_upload_ms = None

    try:
        # Optional pre-upload stamp validation
        if validate_stamp:
            stamp_start = time.perf_counter()
            try:
                validate_stamp_for_upload(stamp_id)
            except StampValidationError as e:
                if e.status == "not_found":
                    raise HTTPException(status_code=404, detail=e.message)
                else:
                    raise HTTPException(status_code=400, detail=e.message)
            stamp_validate_ms = (time.perf_counter() - stamp_start) * 1000

        # Read file content as bytes
        file_start = time.perf_counter()
        data_bytes = await file.read()
        file_read_ms = (time.perf_counter() - file_start) * 1000

        # Upload to Swarm
        bee_start = time.perf_counter()
        reference = upload_data_to_swarm(
            data=data_bytes,
            stamp_id=stamp_id,
            content_type=content_type,
            deferred=deferred
        )
        bee_upload_ms = (time.perf_counter() - bee_start) * 1000

        total_ms = (time.perf_counter() - start_time) * 1000

        # Build response
        timing = None
        if include_timing:
            timing = UploadTiming(
                stamp_validate_ms=stamp_validate_ms,
                file_read_ms=file_read_ms,
                bee_upload_ms=bee_upload_ms,
                total_ms=total_ms
            )

        response = DataUploadResponse(
            reference=reference,
            message=f"File '{file.filename}' uploaded successfully",
            timing=timing
        )

        # Always add Server-Timing header (useful for browser devtools)
        timing_dict = {
            "stamp_validate_ms": stamp_validate_ms,
            "file_read_ms": file_read_ms,
            "bee_upload_ms": bee_upload_ms,
            "total_ms": total_ms
        }
        server_timing = _build_server_timing_header(timing_dict)

        return Response(
            content=response.model_dump_json(),
            media_type="application/json",
            headers={"Server-Timing": server_timing}
        )

    except HTTPException:
        raise  # Re-raise HTTP exceptions as-is
    except RequestException as e:
        logger.error(f"Swarm API error during upload: {e}")
        # Check if the failure was due to stamp utilization
        enhanced_message = check_upload_failure_reason(stamp_id, str(e))
        if enhanced_message:
            raise HTTPException(status_code=400, detail=enhanced_message)
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
    validate_stamp: bool = False,
    deferred: bool = False,
    include_timing: bool = False,
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
    - Optional `validate_stamp` parameter (defaults to false)
    - Optional `deferred` parameter (defaults to false)
    - Optional `include_timing` parameter (defaults to false)

    **Stamp Validation** (opt-in with `validate_stamp=true`):
    When enabled, validates the stamp before upload:
    - Returns 400 if stamp is at 100% utilization (full)
    - Returns 400 if stamp is not usable (expired, invalid)
    - Returns 404 if stamp is not found

    **Deferred Mode** (opt-in with `deferred=true`):
    - `deferred=false` (default): Direct upload - chunks uploaded directly to network,
      ensuring immediate availability. Safer for gateway use cases.
    - `deferred=true`: Deferred upload - data goes to local node first, then syncs to
      network asynchronously. Faster upload response but data may not be immediately
      retrievable from the network.

    **Performance Timing** (opt-in with `include_timing=true`):
    When enabled, includes timing breakdown in response and Server-Timing header:
    - `stamp_validate_ms`: Time validating stamp (only if validate_stamp=true)
    - `file_read_ms`: Time reading uploaded file
    - `tar_validate_ms`: Time validating TAR archive
    - `tar_count_ms`: Time counting files in TAR
    - `bee_upload_ms`: Time uploading to Bee node
    - `total_ms`: Total request processing time
    - `file_count`: Number of files in the TAR archive
    - `ms_per_file`: Average milliseconds per file
    - `files_per_second`: Upload throughput

    **Usage Examples**:
    ```bash
    # Create TAR archive
    tar -cvf files.tar file1.json file2.json file3.json

    # Upload TAR as collection (direct mode, default)
    curl -X POST "http://localhost:8000/api/v1/data/manifest?stamp_id=ABC123" \\
         -F "file=@files.tar"

    # Upload with deferred mode (faster response)
    curl -X POST "http://localhost:8000/api/v1/data/manifest?stamp_id=ABC123&deferred=true" \\
         -F "file=@files.tar"

    # Upload with pre-validation
    curl -X POST "http://localhost:8000/api/v1/data/manifest?stamp_id=ABC123&validate_stamp=true" \\
         -F "file=@files.tar"

    # Upload with timing information
    curl -X POST "http://localhost:8000/api/v1/data/manifest?stamp_id=ABC123&include_timing=true" \\
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
    start_time = time.perf_counter()
    stamp_validate_ms = None
    file_read_ms = None
    tar_validate_ms = None
    tar_count_ms = None
    bee_upload_ms = None

    try:
        # Optional pre-upload stamp validation
        if validate_stamp:
            stamp_start = time.perf_counter()
            try:
                validate_stamp_for_upload(stamp_id)
            except StampValidationError as e:
                if e.status == "not_found":
                    raise HTTPException(status_code=404, detail=e.message)
                else:
                    raise HTTPException(status_code=400, detail=e.message)
            stamp_validate_ms = (time.perf_counter() - stamp_start) * 1000

        # Read TAR file content
        file_start = time.perf_counter()
        tar_bytes = await file.read()
        file_read_ms = (time.perf_counter() - file_start) * 1000

        # Validate TAR archive
        tar_validate_start = time.perf_counter()
        try:
            validate_tar(tar_bytes)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        tar_validate_ms = (time.perf_counter() - tar_validate_start) * 1000

        # Count files for response
        tar_count_start = time.perf_counter()
        file_count = count_tar_files(tar_bytes)
        tar_count_ms = (time.perf_counter() - tar_count_start) * 1000

        # Upload to Swarm as collection
        bee_start = time.perf_counter()
        reference = upload_collection_to_swarm(tar_bytes, stamp_id, deferred=deferred)
        bee_upload_ms = (time.perf_counter() - bee_start) * 1000

        total_ms = (time.perf_counter() - start_time) * 1000

        # Calculate derived metrics
        ms_per_file = total_ms / file_count if file_count > 0 else 0
        files_per_second = (file_count / (total_ms / 1000)) if total_ms > 0 else 0

        # Build response
        timing = None
        if include_timing:
            timing = ManifestUploadTiming(
                stamp_validate_ms=stamp_validate_ms,
                file_read_ms=file_read_ms,
                tar_validate_ms=tar_validate_ms,
                tar_count_ms=tar_count_ms,
                bee_upload_ms=bee_upload_ms,
                total_ms=total_ms,
                file_count=file_count,
                ms_per_file=ms_per_file,
                files_per_second=files_per_second
            )

        response = ManifestUploadResponse(
            reference=reference,
            file_count=file_count,
            message=f"Collection uploaded successfully with {file_count} files",
            timing=timing
        )

        # Always add Server-Timing header (useful for browser devtools)
        timing_dict = {
            "stamp_validate_ms": stamp_validate_ms,
            "file_read_ms": file_read_ms,
            "tar_validate_ms": tar_validate_ms,
            "tar_count_ms": tar_count_ms,
            "bee_upload_ms": bee_upload_ms,
            "total_ms": total_ms,
            "ms_per_file": ms_per_file,
            "files_per_second": files_per_second
        }
        server_timing = _build_server_timing_header(timing_dict)

        return Response(
            content=response.model_dump_json(),
            media_type="application/json",
            headers={"Server-Timing": server_timing}
        )

    except HTTPException:
        # Re-raise HTTP exceptions as-is
        raise
    except RequestException as e:
        logger.error(f"Swarm API error during manifest upload: {e}")
        # Check if the failure was due to stamp utilization
        enhanced_message = check_upload_failure_reason(stamp_id, str(e))
        if enhanced_message:
            raise HTTPException(status_code=400, detail=enhanced_message)
        raise HTTPException(status_code=502, detail=f"Failed to upload collection to Swarm: {e}")
    except ValueError as e:
        logger.error(f"Data processing error during manifest upload: {e}")
        raise HTTPException(status_code=400, detail=f"Manifest upload error: {e}")
    except Exception as e:
        logger.error(f"Unexpected error during manifest upload: {e}")
        raise HTTPException(status_code=500, detail="Internal server error during manifest upload")