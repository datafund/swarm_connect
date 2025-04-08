# app/api/models/stamp.py
from pydantic import BaseModel, Field
from typing import Optional # Ensure Optional is imported

class StampDetails(BaseModel):
    """
    Pydantic model representing the processed stamp information served by our API,
    based on data available from the Swarm /batches endpoint. Handles potentially
    missing fields from the upstream API.
    """
    batchID: str = Field(..., description="The unique identifier of the batch.")
    amount: str = Field(..., alias='value', description="The value of the batch (as string).")

    # --- Make these fields Optional ---
    blockNumber: Optional[int] = Field(None, alias='start', description="The block number when the batch was created (if available).")
    owner: Optional[str] = Field(None, description="The Ethereum address of the batch owner (if available).")
    immutableFlag: Optional[bool] = Field(None, alias='immutable', description="Indicates if the batch is immutable (if available).")
    # --- End Optional fields ---

    depth: int = Field(..., description="The depth of the batch.") # Assuming these are always present based on example
    bucketDepth: int = Field(..., description="The bucket depth of the batch.") # Assuming these are always present
    batchTTL: int = Field(..., description="Original Time-To-Live in seconds (from API).") # Assuming always present

    # --- Fields NOT typically available from /batches endpoint ---
    utilization: Optional[int] = Field(None, description="Stamp utilization (likely unavailable from /batches endpoint).")
    usable: Optional[bool] = Field(None, description="Stamp usability (likely unavailable from /batches endpoint).")
    label: Optional[str] = Field(None, description="User-defined label (likely unavailable from /batches endpoint).")

    # --- Calculated Field ---
    expectedExpiration: str = Field(..., description="Calculated expiration timestamp (YYYY-MM-DD-HH-MM UTC).")

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
                "expectedExpiration": "2024-08-15-10-30"
            }
        }
