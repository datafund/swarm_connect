# app/api/models/chunk.py
from pydantic import BaseModel, Field


class ChunkUploadResponse(BaseModel):
    """Response model for a successful pre-stamped chunk upload."""
    reference: str = Field(..., description="Swarm reference hash of the uploaded chunk")
    message: str = Field(default="Chunk uploaded successfully", description="Success message")

    model_config = {
        "json_schema_extra": {
            "example": {
                "reference": "36b7efd913ca4cf880b8eeac5093fa27b0825906c600685b6abdd6566e6cfe8f",
                "message": "Chunk uploaded successfully"
            }
        }
    }
