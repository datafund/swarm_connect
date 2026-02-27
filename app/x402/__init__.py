# app/x402/__init__.py
"""
x402 Payment Protocol Integration Module.

This module implements the x402 payment protocol for the Swarm gateway,
enabling pay-per-request access to stamp purchases and data uploads.

Key components:
- dependency: FastAPI dependency for payment verification (runs after validation)
- middleware: FastAPI middleware for post-response processing (settlement, headers)
- preflight: Pre-flight balance checks for gateway wallet
- pricing: Price calculation for x402 responses
- access: Whitelist/blacklist access control
- audit: Transaction audit logging

Configuration is loaded from environment variables via app.core.config.
See docs/x402-operator-guide.md for setup instructions.
"""

__version__ = "0.1.0"

from app.x402.dependency import require_x402_payment

__all__ = ["require_x402_payment"]
