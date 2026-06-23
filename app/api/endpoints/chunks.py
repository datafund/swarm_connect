# app/api/endpoints/chunks.py
import logging
import re
from typing import Optional

import httpx
from fastapi import APIRouter, Header, HTTPException, Query, Request, status

from app.api.models.chunk import ChunkUploadResponse
from app.core.config import settings
from app.services.swarm_api import upload_chunk_to_swarm

logger = logging.getLogger(__name__)
router = APIRouter()

# A marshaled postage stamp is 113 bytes = 226 hex characters:
#   batchID[0:32] + index[32:40] + timestamp[40:48] + signature[48:113]
MARSHALED_STAMP_HEX_LEN = 226
_HEX_RE = re.compile(r"^[A-Fa-f0-9]+$")


def _validate_marshaled_stamp(stamp: str) -> str:
    """Validate and normalize the client-supplied marshaled stamp.

    Accepts an optional 0x prefix. Raises HTTP 400 if the value is not
    well-formed hex of the expected length. Returns the bare hex string.
    """
    s = stamp[2:] if stamp[:2].lower() == "0x" else stamp
    if not _HEX_RE.match(s) or len(s) != MARSHALED_STAMP_HEX_LEN:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "INVALID_STAMP",
                "message": (
                    "Swarm-Postage-Stamp must be a hex-encoded 113-byte marshaled stamp "
                    f"({MARSHALED_STAMP_HEX_LEN} hex characters)."
                ),
            },
        )
    return s


@router.post(
    "/",
    response_model=ChunkUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Forward a pre-stamped chunk to Swarm",
)
async def upload_chunk(
    request: Request,
    deferred: bool = Query(
        default=False,
        description="Deferred upload (local first, async sync). Default false = direct upload.",
    ),
    swarm_postage_stamp: Optional[str] = Header(
        default=None,
        alias="Swarm-Postage-Stamp",
        description="Hex-encoded 113-byte marshaled postage stamp signed by the chunk's batch owner.",
    ),
) -> ChunkUploadResponse:
    """
    Forward a single client-supplied **pre-stamped** chunk to the Swarm network.

    The client owns the postage batch and stamps the chunk locally; the gateway is a
    thin forwarder. Send the raw chunk as the request body (`application/octet-stream`)
    and the marshaled stamp in the `Swarm-Postage-Stamp` header. The gateway does NOT
    verify the stamp signature — the Bee node does.

    **Requirements**:
    - Body: raw chunk bytes (8-byte span prefix + up to 4096 bytes payload).
    - Header `Swarm-Postage-Stamp`: hex-encoded 113-byte marshaled stamp.
    - Optional `deferred` query param (default false / non-deferred).

    Returns the Swarm reference hash of the uploaded chunk.
    """
    # Feature toggle: behave as if the route does not exist when disabled.
    if not settings.CHUNK_UPLOAD_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Chunk upload is not enabled on this gateway.",
        )

    if not swarm_postage_stamp:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "MISSING_STAMP",
                "message": "The Swarm-Postage-Stamp header is required for chunk uploads.",
            },
        )

    stamp = _validate_marshaled_stamp(swarm_postage_stamp)

    chunk_bytes = await request.body()
    if not chunk_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "EMPTY_CHUNK", "message": "Request body (the chunk) must not be empty."},
        )

    max_bytes = settings.CHUNK_UPLOAD_MAX_BYTES_PER_REQUEST
    if len(chunk_bytes) > max_bytes:
        raise HTTPException(
            status_code=413,  # Content Too Large
            detail={
                "code": "CHUNK_TOO_LARGE",
                "message": f"Chunk exceeds the maximum size of {max_bytes} bytes.",
                "max_bytes": max_bytes,
            },
        )

    try:
        reference = await upload_chunk_to_swarm(chunk_bytes, stamp, deferred=deferred)
    except httpx.HTTPError as e:
        logger.error(f"Failed to forward chunk to Swarm API: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not forward the chunk. The Bee node may be unavailable or the stamp invalid.",
        )
    except ValueError as e:
        logger.error(f"Invalid response from Swarm API during chunk upload: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Invalid response from Swarm API. Please try again.",
        )

    return ChunkUploadResponse(reference=reference)
