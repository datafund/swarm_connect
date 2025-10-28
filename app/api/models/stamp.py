# app/api/models/stamp.py
from pydantic import BaseModel, Field, validator
from typing import Optional, Union
import re

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


# --- New Models for Stamp Purchasing ---

class StampPurchaseRequest(BaseModel):
    """
    Time-based stamp purchase request using duration in days and data size.
    User-friendly interface that converts to technical parameters.
    """
    duration_days: int = Field(..., ge=1, le=365, description="Storage duration in days (1-365)")
    data_size_mb: float = Field(..., ge=0.1, le=10000, description="Estimated data size in MB (0.1-10000)")
    label: Optional[str] = Field(None, max_length=255, description="Optional label for the stamp batch")
    immutable: bool = Field(True, description="Whether the batch should be immutable")

    @validator('label')
    def validate_label(cls, v):
        if v is not None and not re.match(r'^[a-zA-Z0-9\s\-_.]+$', v):
            raise ValueError('Label can only contain alphanumeric characters, spaces, hyphens, underscores, and periods')
        return v

    class Config:
        schema_extra = {
            "example": {
                "duration_days": 30,
                "data_size_mb": 100.5,
                "label": "My Storage Batch",
                "immutable": True
            }
        }


class StampPurchaseRequestAdvanced(BaseModel):
    """
    Traditional stamp purchase request using technical amount and depth parameters.
    For advanced users who want direct control over parameters.
    """
    amount: str = Field(..., description="Amount in PLUR units (as string)")
    depth: int = Field(..., ge=16, le=255, description="Batch depth (16-255)")
    label: Optional[str] = Field(None, max_length=255, description="Optional label for the stamp batch")
    immutable: bool = Field(True, description="Whether the batch should be immutable")

    @validator('amount')
    def validate_amount(cls, v):
        if not re.match(r'^\d+$', v):
            raise ValueError('Amount must be a string containing only digits')
        if int(v) <= 0:
            raise ValueError('Amount must be greater than 0')
        return v

    @validator('label')
    def validate_label(cls, v):
        if v is not None and not re.match(r'^[a-zA-Z0-9\s\-_.]+$', v):
            raise ValueError('Label can only contain alphanumeric characters, spaces, hyphens, underscores, and periods')
        return v

    class Config:
        schema_extra = {
            "example": {
                "amount": "10000000000",
                "depth": 17,
                "label": "Advanced Batch",
                "immutable": True
            }
        }


class StampPurchaseResponse(BaseModel):
    """
    Response model for successful stamp purchase operations.
    Contains both user-friendly and technical information.
    """
    batchID: str = Field(..., description="The unique identifier of the created batch")

    # User-friendly information
    estimatedExpiration: str = Field(..., description="Estimated expiration timestamp (YYYY-MM-DD-HH-MM UTC)")
    estimatedCostBZZ: str = Field(..., description="Estimated cost in BZZ tokens")

    # Technical details (for transparency and debugging)
    actualAmount: str = Field(..., description="Actual amount used in PLUR units")
    actualDepth: int = Field(..., description="Actual depth used")

    # Transaction information
    txHash: Optional[str] = Field(None, description="Transaction hash (if available)")
    blockNumber: Optional[int] = Field(None, description="Block number where transaction was included")

    # Request details
    label: Optional[str] = Field(None, description="Label assigned to the batch")
    immutable: bool = Field(..., description="Whether the batch is immutable")

    # Duration information (for time-based requests)
    requestedDurationDays: Optional[int] = Field(None, description="Originally requested duration in days")
    requestedDataSizeMB: Optional[float] = Field(None, description="Originally requested data size in MB")

    class Config:
        schema_extra = {
            "example": {
                "batchID": "2856b8c7ccd751d0413e2e16251b90882351c7cea658f91a19ba6b6cc57ea865",
                "estimatedExpiration": "2025-11-13-12-30",
                "estimatedCostBZZ": "0.001",
                "actualAmount": "10000000000",
                "actualDepth": 17,
                "txHash": "0xabc123def456...",
                "blockNumber": 42603500,
                "label": "My Storage Batch",
                "immutable": True,
                "requestedDurationDays": 30,
                "requestedDataSizeMB": 100.5
            }
        }


class StampPurchaseError(BaseModel):
    """
    Error response model for failed stamp purchase operations.
    """
    error_code: str = Field(..., description="Machine-readable error code")
    message: str = Field(..., description="Human-readable error message")
    details: Optional[dict] = Field(None, description="Additional error details")

    class Config:
        schema_extra = {
            "example": {
                "error_code": "INSUFFICIENT_FUNDS",
                "message": "Insufficient BZZ balance for requested stamp purchase",
                "details": {
                    "required_bzz": "0.001",
                    "available_bzz": "0.0005"
                }
            }
        }
