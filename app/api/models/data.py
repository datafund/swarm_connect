# app/api/models/data.py
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any


class UploadTiming(BaseModel):
    """Timing information for upload operations."""
    stamp_validate_ms: Optional[float] = Field(
        default=None,
        description="Time spent validating stamp (ms), only if validate_stamp=true"
    )
    file_read_ms: float = Field(..., description="Time spent reading uploaded file (ms)")
    bee_upload_ms: float = Field(..., description="Time spent uploading to Bee node (ms)")
    total_ms: float = Field(..., description="Total request processing time (ms)")


class ManifestUploadTiming(UploadTiming):
    """Extended timing information for manifest/TAR uploads."""
    tar_validate_ms: float = Field(..., description="Time spent validating TAR archive (ms)")
    tar_count_ms: float = Field(..., description="Time spent counting files in TAR (ms)")
    file_count: int = Field(..., description="Number of files in the TAR archive")
    ms_per_file: float = Field(..., description="Average milliseconds per file (total_ms / file_count)")
    files_per_second: float = Field(..., description="Upload throughput (file_count / total_seconds)")


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
    timing: Optional[UploadTiming] = Field(
        default=None,
        description="Timing breakdown (only included if include_timing=true)"
    )


class DataDownloadResponse(BaseModel):
    """Response model for data download."""
    data: str = Field(..., description="Base64-encoded downloaded data")
    content_type: Optional[str] = Field(default=None, description="MIME type of the content if available")
    size: int = Field(..., description="Size of the data in bytes")
    reference: str = Field(..., description="Swarm reference hash that was downloaded")


class ManifestUploadResponse(BaseModel):
    """Response model for collection/manifest upload."""
    reference: str = Field(
        ...,
        description="Swarm manifest reference hash",
        example="a1b2c3d4e5f6789012345678901234567890123456789012345678901234abcd"
    )
    file_count: int = Field(
        ...,
        description="Number of files in the uploaded collection",
        example=50
    )
    message: str = Field(
        default="Collection uploaded successfully",
        description="Success message"
    )
    timing: Optional[ManifestUploadTiming] = Field(
        default=None,
        description="Timing breakdown (only included if include_timing=true)"
    )