# app/api/models/stamp.py
from pydantic import BaseModel, Field
from typing import Optional, List # Ensure Optional and List are imported

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
    immutableFlag: Optional[bool] = Field(None, alias='immutable', description="Indicates if the batch is immutable (if available).")
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
    """Request model for purchasing a new postage stamp."""
    amount: int = Field(..., description="The amount of the postage stamp in wei.", example=2000000000)
    depth: int = Field(..., description="The depth of the postage stamp.", example=17)
    label: Optional[str] = Field(None, description="Optional user-defined label for the stamp.", example="my-stamp")


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
    """Request model for extending a postage stamp."""
    amount: int = Field(..., description="Additional amount to add to the stamp in wei.", example=500000000)


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
