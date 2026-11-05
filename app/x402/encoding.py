# app/x402/encoding.py
"""Base64 helpers for the x402 headers (#604).

Copies of x402 1.0.0's x402.encoding, which the gateway no longer imports:
standard base64 (with padding), UTF-8 text in and out.
"""
import base64
from typing import Union


def safe_base64_encode(data: Union[str, bytes]) -> str:
    """Encode a string (as UTF-8) or bytes to a base64 string."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return base64.b64encode(data).decode("utf-8")


def safe_base64_decode(data: str) -> str:
    """Decode a base64 string to bytes, then to a UTF-8 string."""
    return base64.b64decode(data).decode("utf-8")
