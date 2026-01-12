# app/x402/middleware.py
"""
FastAPI middleware for x402 payment verification.

This module provides HTTP middleware that:
1. Intercepts requests to protected endpoints
2. Checks if payment is required (X402_ENABLED)
3. Verifies PAYMENT-SIGNATURE header
4. Settles payments via facilitator
5. Returns 402 Payment Required when needed

TODO: Implementation pending - see Issue #4
"""
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from typing import Callable


class X402Middleware(BaseHTTPMiddleware):
    """
    x402 payment verification middleware for FastAPI.

    When X402_ENABLED=true, this middleware:
    - Checks if the endpoint requires payment
    - Verifies payment signatures on protected endpoints
    - Returns HTTP 402 with payment requirements if no valid payment
    - Settles payments via the configured facilitator

    When X402_ENABLED=false, all requests pass through unchanged.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Response]
    ) -> Response:
        """
        Process the request through x402 payment verification.

        TODO: Implementation pending - see Issue #4
        """
        # TODO: Implement x402 payment flow
        # For now, just pass through all requests (X402_ENABLED=false behavior)
        return await call_next(request)


# Protected endpoints configuration
# These endpoints will require x402 payment when X402_ENABLED=true
PROTECTED_ENDPOINTS = [
    ("POST", "/api/v1/stamps/"),
    ("POST", "/api/v1/data/"),
    ("POST", "/api/v1/data/manifest"),
]
