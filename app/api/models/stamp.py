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
    utilization: Optional[int] = Field(None, description="Stamp utilization (from local /stamps endpoint when available).")
    usable: Optional[bool] = Field(None, description="Stamp usability status (from local /stamps endpoint or calculated).")
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
                "utilization": None,
                "usable": None,
                "label": None,
                "expectedExpiration": "2024-08-15-10-30",
                "local": False
            }
        }


class StampPurchaseRequest(BaseModel):
    """Request model for purchasing a new postage stamp.

    Storage size can be specified using either:
    - size: Human-friendly preset ("small", "medium", "large")
    - depth: Technical depth value (16-32)

    Duration can be specified using either:
    - duration_hours: Desired duration in hours
    - amount: Raw PLUR amount (legacy)

    Defaults: size="small" (depth 17), duration_hours=25
    """
    duration_hours: Optional[int] = Field(
        None,
        description="Desired stamp duration in hours. Default is 25 hours if neither duration_hours nor amount is provided.",
        example=25,
        ge=1
    )
    amount: Optional[int] = Field(
        None,
        description="The amount of the postage stamp in PLUR (legacy). If provided, overrides duration_hours.",
        example=8000000000
    )
    size: Optional[Literal["small", "medium", "large"]] = Field(
        None,
        description="Storage size preset. 'small': one small document, 'medium': several medium documents, 'large': several large documents. Overrides depth if provided."
    )
    depth: Optional[int] = Field(
        None,
        description="The depth of the postage stamp (advanced). Default is 17 if neither size nor depth is provided.",
        example=17,
        ge=16,
        le=32
    )
    label: Optional[str] = Field(None, description="Optional user-defined label for the stamp.", example="my-stamp")

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
        None,
        description="Desired additional duration in hours. Default is 25 hours if neither duration_hours nor amount is provided.",
        example=25,
        ge=1
    )
    amount: Optional[int] = Field(
        None,
        description="Additional amount to add to the stamp in PLUR (legacy). If provided, overrides duration_hours.",
        example=8000000000
    )


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

