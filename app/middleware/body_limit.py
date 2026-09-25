# app/middleware/body_limit.py
"""
Request body size and JSON depth limiting middleware.

Protects against denial-of-service attacks using oversized or deeply
nested JSON payloads. Deeply nested JSON (100+ levels) can cause
Python's json parser to consume excessive CPU/memory, hanging the
event loop.

Configuration (app/core/config.py):
- MAX_JSON_BODY_BYTES: Maximum JSON body size (default 1 MB)
- MAX_JSON_DEPTH: Maximum nesting depth (default 20)
"""
import logging

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.core.config import settings

logger = logging.getLogger(__name__)


def _check_nesting_depth(data: bytes, max_depth: int) -> bool:
    """
    Fast O(n) check for JSON nesting depth.

    Scans for { and [ characters while tracking string/escape state
    so that bracket characters inside JSON strings are ignored.

    Returns True if depth is within limit, False if exceeded.
    """
    depth = 0
    in_string = False
    escape = False

    for byte in data:
        if escape:
            escape = False
            continue

        char = byte  # int in Python 3

        if char == 0x5C and in_string:  # backslash
            escape = True
            continue

        if char == 0x22:  # double quote
            in_string = not in_string
            continue

        if in_string:
            continue

        if char == 0x7B or char == 0x5B:  # { or [
            depth += 1
            if depth > max_depth:
                return False
        elif char == 0x7D or char == 0x5D:  # } or ]
            depth -= 1

    return True


class BodyLimitMiddleware(BaseHTTPMiddleware):
    """
    Middleware to limit request body size and JSON nesting depth.

    Only inspects requests with Content-Type: application/json.
    File uploads (multipart/form-data) are not affected — those are
    governed by MAX_UPLOAD_SIZE_MB at the endpoint level.
    """

    async def dispatch(self, request: Request, call_next):
        content_type = (request.headers.get("content-type") or "").lower()

        # JSON bodies, including +json types and requests with no Content-Type
        # at all: FastAPI parses a body without one as JSON, so skipping those
        # left the size and depth limits open (#354). Multipart uploads and
        # octet-stream chunks carry their own limits and are not inspected.
        is_json = "json" in content_type
        if content_type and not is_json:
            return await call_next(request)
        if not content_type and (
            request.method in ("GET", "HEAD", "OPTIONS", "DELETE")
            # Raw chunk uploads are binary and often sent without a
            # Content-Type; a depth scan would misread random bytes as nested
            # JSON. The route reads the body itself with its own size limit.
            or request.url.path.rstrip("/") == f"{settings.API_V1_STR}/chunks"
        ):
            return await call_next(request)

        max_bytes = settings.MAX_JSON_BODY_BYTES
        max_depth = settings.MAX_JSON_DEPTH

        # Fast reject via Content-Length header
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > max_bytes:
                    logger.warning(
                        f"JSON body rejected: Content-Length {content_length} "
                        f"exceeds limit of {max_bytes} bytes"
                    )
                    return JSONResponse(
                        status_code=413,
                        content={
                            "detail": f"Request body too large. "
                            f"Maximum size for JSON is {max_bytes} bytes."
                        },
                    )
            except ValueError:
                pass

        # Read body for actual size and depth check
        body = await request.body()

        if len(body) > max_bytes:
            logger.warning(
                f"JSON body rejected: {len(body)} bytes exceeds limit of {max_bytes} bytes"
            )
            return JSONResponse(
                status_code=413,
                content={
                    "detail": f"Request body too large. "
                    f"Maximum size for JSON is {max_bytes} bytes."
                },
            )

        if body and not _check_nesting_depth(body, max_depth):
            logger.warning(f"JSON body rejected: nesting depth exceeds {max_depth} levels")
            return JSONResponse(
                status_code=400,
                content={
                    "detail": f"JSON nesting too deep. "
                    f"Maximum depth is {max_depth} levels."
                },
            )

        return await call_next(request)
