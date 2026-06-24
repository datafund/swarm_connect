# app/api/endpoints/debug.py
"""
Signature-gated, read-only proxy for Bee diagnostic endpoints.

Operators who only have access to the gateway (not the Bee node's API) can read
the node's diagnostics (topology, peers, status, ...) by proving control of an
allow-listed address — no shared secret is stored anywhere.

Auth: send an EIP-191 personal_sign of "swarm-connect-debug:<unix_ts>" by an
allow-listed address.
  Headers: X-Debug-Timestamp: <unix seconds>
           X-Debug-Signature: 0x<65-byte sig>
The signer is recovered and must be in DEBUG_ALLOWED_ADDRESSES; the timestamp
must be within DEBUG_SIG_MAX_AGE_SECONDS (replay guard).

Disabled (404) when DEBUG_ALLOWED_ADDRESSES is empty. Only read-only, allow-listed
Bee paths are proxied — never writes.
"""
import logging
import time
from typing import Optional
from urllib.parse import urljoin

import httpx
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import Response

from app.core.config import settings
from app.services.http_client import get_client

logger = logging.getLogger(__name__)
router = APIRouter()

# Read-only Bee endpoints safe to expose for diagnostics (matched on first path segment).
ALLOWED_BEE_PATHS = {
    "topology", "addresses", "health", "readiness", "peers", "chainstate",
    "reservestate", "redistributionstate", "status", "node", "stamps",
    "batches", "chequebook", "wallet",
}

SIG_MESSAGE_PREFIX = "swarm-connect-debug:"


def _authorize(timestamp: Optional[str], signature: Optional[str]) -> str:
    """Verify the request is signed by an allow-listed address over a fresh timestamp.

    Returns the recovered address, or raises HTTPException.
    """
    allowed = settings.get_debug_allowed_addresses()
    if not allowed:
        # Hidden when not configured.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    if not timestamp or not signature:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Debug-Timestamp / X-Debug-Signature",
        )

    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid timestamp")

    if abs(int(time.time()) - ts) > settings.DEBUG_SIG_MAX_AGE_SECONDS:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Stale or future timestamp")

    message = encode_defunct(text=f"{SIG_MESSAGE_PREFIX}{ts}")
    try:
        signer = Account.recover_message(message, signature=signature)
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")

    if signer.lower() not in allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Address not allow-listed")
    return signer


@router.get("/bee/{bee_path:path}", summary="Read-only proxy to allow-listed Bee diagnostic endpoints")
async def debug_bee(
    bee_path: str,
    request: Request,
    x_debug_timestamp: Optional[str] = Header(default=None, alias="X-Debug-Timestamp"),
    x_debug_signature: Optional[str] = Header(default=None, alias="X-Debug-Signature"),
) -> Response:
    """Proxy a GET to the gateway's Bee node for an allow-listed diagnostic path.

    Example: `GET /api/v1/debug/bee/topology`. Requires a valid signature from an
    address in `DEBUG_ALLOWED_ADDRESSES`.
    """
    _authorize(x_debug_timestamp, x_debug_signature)

    top = bee_path.strip("/").split("/")[0]
    if top not in ALLOWED_BEE_PATHS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "PATH_NOT_ALLOWED", "message": f"'{top}' is not a permitted debug path",
                    "allowed": sorted(ALLOWED_BEE_PATHS)},
        )

    url = urljoin(str(settings.SWARM_BEE_API_URL), bee_path.lstrip("/"))
    if request.url.query:
        url = f"{url}?{request.url.query}"

    try:
        client = get_client()
        resp = await client.get(url, timeout=15)
    except httpx.HTTPError as e:
        logger.warning(f"debug proxy: Bee request failed ({url}): {e}")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Bee node request failed")

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )
