# app/api/models/stamp.py
from pydantic import BaseModel, Field, field_validator
from typing import Optional, List, Literal

# Size presets mapping to depth values
SIZE_PRESETS = {
    "small": 17,   # Use for one small document
    "medium": 20,  # Use for several medium documents
    "large": 22,   # Use for several large documents
}

class StampDetails(BaseModel):
    """
    Pydantic model representing the processed stamp information served by our API,
    based on data available from the Swarm /batches endpoint. Handles potentially
    missing fields from the upstream API.
    """
    batchID: str = Field(..., description="The unique identifier of the batch.")
    amount: str = Field(..., description="The amount of the batch (as string).")

    # --- Make these fields Optional ---
    blockNumber: Optional[int] = Field(None, alias='start', description="The block number when the batch was created (if available).")
    owner: Optional[str] = Field(None, description="The Ethereum address of the batch owner (if available).")
    immutableFlag: Optional[bool] = Field(None, description="Indicates if the batch is immutable (if available).")
    # --- End Optional fields ---

    depth: int = Field(..., description="The depth of the batch.") # Assuming these are always present based on example
    bucketDepth: int = Field(..., description="The bucket depth of the batch.") # Assuming these are always present
    batchTTL: int = Field(..., description="Original Time-To-Live in seconds (from API).") # Assuming always present

    # --- Fields enhanced with local stamp data ---
    utilization: Optional[int] = Field(None, description="Stamp utilization - raw bucket fill level (from local /stamps endpoint when available).")
    utilizationPercent: Optional[float] = Field(None, description="Stamp utilization as percentage (0-100). Calculated as: (utilization / 2^(depth-bucketDepth)) * 100.")
    utilizationStatus: Optional[Literal["ok", "warning", "critical", "full"]] = Field(
        None,
        description="Utilization status: 'ok' (0-80%), 'warning' (80-95%), 'critical' (95-99.99%), 'full' (100%). Null if utilization unknown."
    )
    utilizationWarning: Optional[str] = Field(
        None,
        description="Human-readable warning message when stamp utilization is elevated. Null when status is 'ok' or unknown."
    )
    usable: Optional[bool] = Field(None, description="Stamp usability status. False when stamp is expired, invalid, or at 100% utilization.")
    label: Optional[str] = Field(None, description="User-defined label (from local /stamps endpoint when available).")

    # --- Calculated Fields ---
    expectedExpiration: str = Field(..., description="Calculated expiration timestamp (YYYY-MM-DD-HH-MM UTC).")
    local: bool = Field(..., description="Indicates if this stamp is owned/managed by the local node.")

    class Config:
        populate_by_name = True
        # Update example to show potential nulls
        schema_extra = {
            "example": {
                "batchID": "000de42079daebd58347bb38ce05bdc477701d93651d3bba318a9aee3fbd786a",
                "amount": "303440675840",
                "blockNumber": 36541095,      # Can be null
                "owner": "1fb1f1d3620eab8e3b69dd2b2c40933a61c7f276", # Can be null
                "depth": 20,
                "bucketDepth": 16,
                "immutableFlag": False,     # Can be null
                "batchTTL": 16971999,
                "utilization": 8,
                "utilizationPercent": 50.0,
                "utilizationStatus": "ok",
                "utilizationWarning": None,
                "usable": True,
                "label": None,
                "expectedExpiration": "2024-08-15-10-30",
                "local": True
            }
        }


class StampPurchaseRequest(BaseModel):
    """Request model for purchasing a new postage stamp.

    Storage size can be specified using either:
    - size: Human-friendly preset ("small", "medium", "large")
    - depth: Technical depth value (16-32)

    Duration can be specified using either:
    - duration_hours: Desired duration in hours (minimum 24)
    - amount: Raw PLUR amount (legacy)

    Defaults: size="small" (depth 17), duration_hours=25
    """
    duration_hours: Optional[int] = Field(
        default=None,
        description="Desired stamp duration in hours. Minimum 24 hours. Default is 25 hours if neither duration_hours nor amount is provided.",
        ge=24
    )
    amount: Optional[int] = Field(
        default=None,
        description="The amount of the postage stamp in PLUR (legacy). If provided, overrides duration_hours."
    )
    size: Optional[Literal["small", "medium", "large"]] = Field(
        default=None,
        description="Storage size preset. 'small': one small document, 'medium': several medium documents, 'large': several large documents. Overrides depth if provided."
    )
    depth: Optional[int] = Field(
        default=None,
        description="The depth of the postage stamp (advanced). Default is 17 if neither size nor depth is provided.",
        ge=16,
        le=32
    )
    label: Optional[str] = Field(default=None, description="Optional user-defined label for the stamp.")

    model_config = {
        "json_schema_extra": {
            "example": {
                "duration_hours": 25,
                "size": "small",
                "label": "my-stamp"
            }
        }
    }

    def get_effective_depth(self) -> int:
        """Returns the effective depth based on size preset or explicit depth."""
        if self.size is not None:
            return SIZE_PRESETS[self.size]
        if self.depth is not None:
            return self.depth
        return 17  # Default


class StampPurchaseResponse(BaseModel):
    """Response model for successful stamp purchase."""
    batchID: str = Field(..., description="The unique identifier of the purchased stamp batch.")
    message: str = Field(..., description="Success message.")

    class Config:
        schema_extra = {
            "example": {
                "batchID": "000de42079daebd58347bb38ce05bdc477701d93651d3bba318a9aee3fbd786a",
                "message": "Postage stamp purchased successfully"
            }
        }


class StampExtensionRequest(BaseModel):
    """Request model for extending a postage stamp.

    Either duration_hours or amount must be provided.
    If duration_hours is provided, amount is calculated automatically based on current price.
    If amount is provided, it overrides duration_hours (legacy support).
    """
    duration_hours: Optional[int] = Field(
        default=None,
        description="Desired additional duration in hours. Minimum 24 hours. Default is 25 hours if neither duration_hours nor amount is provided.",
        ge=24
    )
    amount: Optional[int] = Field(
        default=None,
        description="Additional amount to add to the stamp in PLUR (legacy). If provided, overrides duration_hours."
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "duration_hours": 25
            }
        }
    }


class StampExtensionResponse(BaseModel):
    """Response model for successful stamp extension."""
    batchID: str = Field(..., description="The unique identifier of the extended stamp batch.")
    message: str = Field(..., description="Success message.")

    class Config:
        schema_extra = {
            "example": {
                "batchID": "000de42079daebd58347bb38ce05bdc477701d93651d3bba318a9aee3fbd786a",
                "message": "Postage stamp extended successfully"
            }
        }


class StampListResponse(BaseModel):
    """Response model for listing all stamps."""
    stamps: List[StampDetails] = Field(..., description="List of all available stamp batches")
    total_count: int = Field(..., description="Total number of stamps")

    class Config:
        schema_extra = {
            "example": {
                "stamps": [
                    {
                        "batchID": "000de42079daebd58347bb38ce05bdc477701d93651d3bba318a9aee3fbd786a",
                        "amount": "303440675840",
                        "blockNumber": 36541095,
                        "owner": "1fb1f1d3620eab8e3b69dd2b2c40933a61c7f276",
                        "depth": 20,
                        "bucketDepth": 16,
                        "immutableFlag": False,
                        "batchTTL": 16971999,
                        "utilization": None,
                        "usable": None,
                        "label": None,
                        "expectedExpiration": "2024-08-15-10-30",
                        "local": False
                    }
                ],
                "total_count": 1
            }
        }


class StampHealthIssue(BaseModel):
    """Represents an issue found during stamp health check."""
    code: str = Field(..., description="Error code (e.g., NOT_LOCAL, NOT_USABLE, EXPIRED, FULL)")
    message: str = Field(..., description="Human-readable error message")
    suggestion: str = Field(..., description="Actionable suggestion to fix the issue")


class StampHealthStatus(BaseModel):
    """Detailed status information for a stamp."""
    exists: bool = Field(..., description="Whether the stamp exists on the network")
    local: bool = Field(..., description="Whether the stamp is owned by the local node")
    usable: Optional[bool] = Field(None, description="Whether the stamp is currently usable for uploads")
    utilizationPercent: Optional[float] = Field(None, description="Current utilization as percentage (0-100)")
    utilizationStatus: Optional[Literal["ok", "warning", "critical", "full"]] = Field(None, description="Utilization status category")
    batchTTL: Optional[int] = Field(None, description="Time-to-live in seconds")
    expectedExpiration: Optional[str] = Field(None, description="Expected expiration timestamp (YYYY-MM-DD-HH-MM UTC)")


class StampHealthCheckResponse(BaseModel):
    """Response model for stamp health check endpoint."""
    stamp_id: str = Field(..., description="The batch ID of the checked stamp")
    can_upload: bool = Field(..., description="Whether uploads can proceed with this stamp (no blocking errors)")
    errors: List[StampHealthIssue] = Field(default_factory=list, description="Blocking issues that prevent uploads")
    warnings: List[StampHealthIssue] = Field(default_factory=list, description="Non-blocking issues to be aware of")
    status: StampHealthStatus = Field(..., description="Current stamp status details")

    class Config:
        schema_extra = {
            "example": {
                "stamp_id": "000de42079daebd58347bb38ce05bdc477701d93651d3bba318a9aee3fbd786a",
                "can_upload": True,
                "errors": [],
                "warnings": [
                    {
                        "code": "NEARLY_FULL",
                        "message": "Stamp is 82% utilized.",
                        "suggestion": "Consider purchasing a new stamp soon to avoid upload failures."
                    }
                ],
                "status": {
                    "exists": True,
                    "local": True,
                    "usable": True,
                    "utilizationPercent": 82.5,
                    "utilizationStatus": "warning",
                    "batchTTL": 86400,
                    "expectedExpiration": "2026-01-12-17-30"
                }
            }
        }


class StampValidationErrorDetail(BaseModel):
    """Structured error detail for stamp validation failures."""
    code: str = Field(..., description="Error code (e.g., NOT_LOCAL, NOT_USABLE, EXPIRED, FULL)")
    message: str = Field(..., description="Human-readable error message")
    suggestion: str = Field(..., description="Actionable suggestion to fix the issue")
    stamp_id: str = Field(..., description="The batch ID of the problematic stamp")
    stamp_status: Optional[StampHealthStatus] = Field(None, description="Current stamp status if available")

