# app/api/endpoints/pool.py
"""
API endpoints for the Stamp Pool feature.

Provides endpoints for:
- Getting pool status
- Acquiring/releasing stamps from the pool
- Manual pool maintenance
"""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from typing import Dict, List, Optional, Literal
from datetime import datetime
import logging

from app.core.config import settings
from app.services.stamp_pool import stamp_pool_manager, PoolStampStatus
from app.api.models.stamp import SIZE_PRESETS

router = APIRouter()
logger = logging.getLogger(__name__)


# === Response Models ===

class PoolStatusResponse(BaseModel):
    """Response model for pool status endpoint."""
    enabled: bool = Field(..., description="Whether stamp pool feature is enabled")
    reserve_config: Dict[int, int] = Field(..., description="Target reserve levels by depth")
    current_levels: Dict[int, int] = Field(..., description="Current stamp counts by depth")
    available_stamps: Dict[int, List[str]] = Field(..., description="Available batch IDs by depth")
    total_stamps: int = Field(..., description="Total available stamps in pool")
    low_reserve_warning: bool = Field(..., description="True if any depth is below threshold")
    last_check: Optional[str] = Field(None, description="Last maintenance check timestamp (ISO format)")
    next_check: Optional[str] = Field(None, description="Next scheduled check timestamp (ISO format)")
    errors: List[str] = Field(default_factory=list, description="Recent errors")

    class Config:
        json_schema_extra = {
            "example": {
                "enabled": True,
                "reserve_config": {17: 1, 20: 1},
                "current_levels": {17: 1, 20: 0},
                "available_stamps": {17: ["abc123..."]},
                "total_stamps": 1,
                "low_reserve_warning": True,
                "last_check": "2026-01-21T10:00:00Z",
                "next_check": "2026-01-21T10:15:00Z",
                "errors": []
            }
        }


class PoolStampInfo(BaseModel):
    """Information about a stamp from the pool."""
    batch_id: str = Field(..., description="The batch ID of the stamp")
    depth: int = Field(..., description="Stamp depth (17=small, 20=medium, 22=large)")
    size_name: str = Field(..., description="Human-friendly size name")
    created_at: str = Field(..., description="When stamp was added to pool (ISO format)")
    ttl_at_creation: int = Field(..., description="TTL when added to pool (seconds)")


class AcquireStampRequest(BaseModel):
    """Request to acquire a stamp from the pool."""
    size: Optional[Literal["small", "medium", "large"]] = Field(
        None,
        description="Preferred stamp size. If not available, may return larger size."
    )
    depth: Optional[int] = Field(
        None,
        description="Specific depth requested (overrides size). 17=small, 20=medium, 22=large."
    )

    class Config:
        json_schema_extra = {
            "example": {
                "size": "small"
            }
        }


class AcquireStampResponse(BaseModel):
    """Response when acquiring a stamp from the pool."""
    success: bool = Field(..., description="Whether a stamp was acquired")
    batch_id: Optional[str] = Field(None, description="The batch ID of the acquired stamp")
    depth: Optional[int] = Field(None, description="Depth of the acquired stamp")
    size_name: Optional[str] = Field(None, description="Human-friendly size name")
    message: str = Field(..., description="Status message")
    fallback_used: bool = Field(False, description="True if a larger stamp was provided")

    class Config:
        json_schema_extra = {
            "example": {
                "success": True,
                "batch_id": "abc123def456...",
                "depth": 17,
                "size_name": "small",
                "message": "Stamp acquired from pool",
                "fallback_used": False
            }
        }


class ManualCheckResponse(BaseModel):
    """Response from manual pool maintenance check."""
    checked_at: str = Field(..., description="Timestamp of check (ISO format)")
    stamps_purchased: int = Field(..., description="Number of stamps purchased")
    stamps_topped_up: int = Field(..., description="Number of stamps topped up")
    stamps_synced: int = Field(0, description="Number of existing stamps synced to pool")
    errors: List[str] = Field(default_factory=list, description="Errors encountered")


# === Helper Functions ===

def depth_to_size_name(depth: int) -> str:
    """Convert depth to human-friendly size name."""
    for name, d in SIZE_PRESETS.items():
        if d == depth:
            return name
    return f"depth-{depth}"


# === Endpoints ===

@router.get(
    "/status",
    response_model=PoolStatusResponse,
    summary="Get Pool Status",
    description="Get current status of the stamp pool including reserve levels and available stamps."
)
async def get_pool_status():
    """Get current stamp pool status."""
    status = stamp_pool_manager.get_status()

    return PoolStatusResponse(
        enabled=status.enabled,
        reserve_config=status.reserve_config,
        current_levels=status.current_levels,
        available_stamps=status.available_stamps,
        total_stamps=status.total_stamps,
        low_reserve_warning=status.low_reserve_warning,
        last_check=status.last_check.isoformat() if status.last_check else None,
        next_check=status.next_check.isoformat() if status.next_check else None,
        errors=status.errors
    )


@router.post(
    "/acquire",
    response_model=AcquireStampResponse,
    summary="Acquire Stamp from Pool",
    description=(
        "Acquire a stamp from the pool for immediate use. "
        "This is much faster than purchasing a new stamp (~5 seconds vs >1 minute). "
        "The stamp is released from the pool and becomes the caller's responsibility."
    )
)
async def acquire_stamp(
    request: AcquireStampRequest,
    http_request: Request
):
    """Acquire a stamp from the pool."""
    if not settings.STAMP_POOL_ENABLED:
        raise HTTPException(
            status_code=404,
            detail="Stamp pool feature is not enabled on this gateway. Use POST /api/v1/stamps/ to purchase stamps directly."
        )

    # Determine requested depth
    if request.depth is not None:
        requested_depth = request.depth
    elif request.size is not None:
        requested_depth = SIZE_PRESETS.get(request.size, 17)
    else:
        requested_depth = 17  # Default to small

    # Try to get exact match first
    stamp = stamp_pool_manager.get_available_stamp(requested_depth)
    fallback_used = False

    # If no exact match, try any larger stamp
    if not stamp:
        stamp = stamp_pool_manager.get_available_stamp_any_size(requested_depth)
        if stamp:
            fallback_used = True

    if not stamp:
        size_name = depth_to_size_name(requested_depth)
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"No stamp available for depth {requested_depth} (size: {size_name}). Pool is exhausted.",
                "suggestion": "Purchase a stamp directly via POST /api/v1/stamps/"
            }
        )

    # Get client identifier for logging
    client_ip = http_request.client.host if http_request.client else "unknown"

    # Release the stamp
    released = stamp_pool_manager.release_stamp(stamp.batch_id, released_to=client_ip)

    if not released:
        # Race condition - stamp was taken between check and release
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Stamp was acquired by another request.",
                "suggestion": "Retry the request or purchase a stamp directly via POST /api/v1/stamps/"
            }
        )

    # Trigger immediate replenishment if pool is below target
    # This runs in the background and doesn't affect the response
    replenishment_triggered = stamp_pool_manager.trigger_replenishment_if_needed(released.depth)
    if replenishment_triggered:
        logger.info(f"Triggered immediate replenishment for depth {released.depth}")

    size_name = depth_to_size_name(released.depth)

    message = f"Stamp acquired from pool (depth={released.depth}, size={size_name})"
    if fallback_used:
        message = f"Requested size not available. {message} (larger than requested)"

    return AcquireStampResponse(
        success=True,
        batch_id=released.batch_id,
        depth=released.depth,
        size_name=size_name,
        message=message,
        fallback_used=fallback_used
    )


@router.get(
    "/available",
    response_model=List[PoolStampInfo],
    summary="List Available Stamps",
    description="List all stamps currently available in the pool."
)
async def list_available_stamps():
    """List all available stamps in the pool."""
    if not settings.STAMP_POOL_ENABLED:
        raise HTTPException(
            status_code=404,
            detail="Stamp pool feature is not enabled on this gateway. Use POST /api/v1/stamps/ to purchase stamps directly."
        )

    status = stamp_pool_manager.get_status()
    stamps = []

    # Get stamp details
    for depth, batch_ids in status.available_stamps.items():
        for batch_id in batch_ids:
            stamp = stamp_pool_manager._pool.get(batch_id)
            if stamp:
                stamps.append(PoolStampInfo(
                    batch_id=stamp.batch_id,
                    depth=stamp.depth,
                    size_name=depth_to_size_name(stamp.depth),
                    created_at=stamp.created_at.isoformat(),
                    ttl_at_creation=stamp.ttl_at_creation
                ))

    return stamps


@router.post(
    "/check",
    response_model=ManualCheckResponse,
    summary="Trigger Pool Maintenance",
    description=(
        "Manually trigger a pool maintenance check. "
        "This will sync existing stamps, purchase new ones if needed, and top up low-TTL stamps. "
        "Normally runs automatically in the background."
    )
)
async def trigger_pool_check():
    """Manually trigger pool maintenance check."""
    if not settings.STAMP_POOL_ENABLED:
        raise HTTPException(
            status_code=404,
            detail="Stamp pool feature is not enabled on this gateway. Use POST /api/v1/stamps/ to purchase stamps directly."
        )

    result = await stamp_pool_manager.check_and_replenish()

    return ManualCheckResponse(
        checked_at=result.get("checked_at", datetime.now().isoformat()),
        stamps_purchased=result.get("stamps_purchased", 0),
        stamps_topped_up=result.get("stamps_topped_up", 0),
        stamps_synced=result.get("stamps_synced", 0),
        errors=result.get("errors", [])
    )
