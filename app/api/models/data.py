# app/api/models/data.py
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any




class DataUploadRequest(BaseModel):
    """Request model for uploading base64-encoded binary data to Swarm."""
    data: str = Field(
        ...,
        description="Base64-encoded binary data to upload",
        example="SGVsbG8sIFN3YXJtISBUaGlzIGlzIGEgdGVzdCBmaWxlIGNvbnRlbnQu"
    )
    content_type: Optional[str] = Field(
        default="application/octet-stream",
        description="MIME type of the content",
        example="text/plain"
    )


class DataUploadResponse(BaseModel):
    """Response model for successful data upload."""
    reference: str = Field(..., description="Swarm reference hash of uploaded data")
    message: str = Field(default="Data uploaded successfully", description="Success message")


class DataDownloadResponse(BaseModel):
    """Response model for data download."""
    data: str = Field(..., description="Base64-encoded downloaded data")
    content_type: Optional[str] = Field(default=None, description="MIME type of the content if available")
    size: int = Field(..., description="Size of the data in bytes")
    reference: str = Field(..., description="Swarm reference hash that was downloaded")