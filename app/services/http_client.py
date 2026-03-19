# app/services/http_client.py
"""
Shared httpx.AsyncClient for all outbound HTTP requests.

Initialized during FastAPI lifespan startup, closed on shutdown.
Provides connection pooling and non-blocking I/O for Swarm Bee API calls.
"""
import httpx
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_client: Optional[httpx.AsyncClient] = None


async def init_client():
    """Initialize the shared HTTP client. Called during app startup."""
    global _client
    _client = httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, connect=5.0),
        follow_redirects=True,
    )
    logger.info("Initialized shared httpx.AsyncClient")


async def close_client():
    """Close the shared HTTP client. Called during app shutdown."""
    global _client
    if _client:
        await _client.aclose()
        _client = None
        logger.info("Closed shared httpx.AsyncClient")


def get_client() -> httpx.AsyncClient:
    """Get the shared HTTP client instance."""
    if _client is None:
        raise RuntimeError("HTTP client not initialized — call init_client() first")
    return _client
