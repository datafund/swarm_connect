# app/api/models/chunk.py
from typing import Optional

from pydantic import BaseModel, Field


class ChunkUploadResponse(BaseModel):
    """Response model for a successful pre-stamped chunk upload."""
    reference: str = Field(..., description="Swarm reference hash of the uploaded chunk")
    message: str = Field(default="Chunk uploaded successfully", description="Success message")
    bytes_charged: Optional[int] = Field(
        default=None,
        description="Bytes debited from bandwidth credit for this upload (null when billing is off)."
    )
    credit_balance_bytes: Optional[int] = Field(
        default=None,
        description="Remaining bandwidth credit balance in bytes (null when billing is off)."
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "reference": "36b7efd913ca4cf880b8eeac5093fa27b0825906c600685b6abdd6566e6cfe8f",
                "message": "Chunk uploaded successfully",
                "bytes_charged": 4104,
                "credit_balance_bytes": 99995896
            }
        }
    }


class CreditTopUpResponse(BaseModel):
    """Response model for a successful prepaid bandwidth credit top-up."""
    address: str = Field(..., description="Wallet address (the x402 payer) the credit is bound to.")
    token: str = Field(..., description="Bearer credit token to present on chunk uploads (keep secret).")
    credited_bytes: int = Field(..., description="Bytes added to the balance by this top-up.")
    balance_bytes: int = Field(..., description="New total balance in bytes after the top-up.")
    message: str = Field(default="Bandwidth credit added successfully", description="Success message")
