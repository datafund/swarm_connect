# app/api/endpoints/notary.py
"""
API endpoints for notary/provenance signing functionality.
"""
import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from app.core.config import settings
from app.services.provenance import get_provenance_service

logger = logging.getLogger(__name__)
router = APIRouter()


class NotaryInfoResponse(BaseModel):
    """Response model for notary info endpoint."""
    enabled: bool
    available: bool
    address: Optional[str] = None
    message: str


class NotaryStatusResponse(BaseModel):
    """Response model for notary status check."""
    enabled: bool
    available: bool
    address: Optional[str] = None


@router.get("/info", response_model=NotaryInfoResponse)
async def get_notary_info():
    """
    Get information about the gateway's notary signing service.

    **Response when enabled and configured:**
    ```json
    {
        "enabled": true,
        "available": true,
        "address": "0x1234...abcd",
        "message": "Notary signing is available. Use sign=notary on upload."
    }
    ```

    **Response when enabled but not configured:**
    ```json
    {
        "enabled": true,
        "available": false,
        "address": null,
        "message": "Notary is enabled but not configured (missing NOTARY_PRIVATE_KEY)."
    }
    ```

    **Response when disabled:**
    ```json
    {
        "enabled": false,
        "available": false,
        "address": null,
        "message": "Notary signing is not enabled on this gateway."
    }
    ```

    **Use case:**
    Clients should call this endpoint to:
    1. Check if notary signing is available before uploading with `sign=notary`
    2. Get the notary's public address for verification purposes

    **Verification:**
    The `address` field contains the Ethereum address derived from the notary's
    private key. Clients can use this address to verify signatures on downloaded
    documents using standard EIP-191 verification.
    """
    provenance_service = get_provenance_service()

    if not settings.NOTARY_ENABLED:
        return NotaryInfoResponse(
            enabled=False,
            available=False,
            address=None,
            message="Notary signing is not enabled on this gateway."
        )

    if not provenance_service.is_available:
        return NotaryInfoResponse(
            enabled=True,
            available=False,
            address=None,
            message="Notary is enabled but not configured (missing NOTARY_PRIVATE_KEY)."
        )

    return NotaryInfoResponse(
        enabled=True,
        available=True,
        address=provenance_service.notary_address,
        message="Notary signing is available. Use sign=notary on upload."
    )


@router.get("/status", response_model=NotaryStatusResponse)
async def get_notary_status():
    """
    Get the notary service status (simplified version for health checks).

    Returns a minimal status response suitable for health check integrations.
    """
    provenance_service = get_provenance_service()

    return NotaryStatusResponse(
        enabled=settings.NOTARY_ENABLED,
        available=provenance_service.is_available,
        address=provenance_service.notary_address
    )
