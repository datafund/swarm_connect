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

Paths (#693): the whole path must match one entry of _ROUTES exactly, segment by
segment, after the one percent-decode the server already did. Dot segments,
empty segments (also a trailing slash), backslashes, control characters and any
remaining '%' (a second encoding) are refused with 403 DEBUG_PATH_INVALID; a
well-formed path not in _ROUTES with 403 PATH_NOT_ALLOWED; any query string with
403 DEBUG_QUERY_NOT_ALLOWED. The Bee URL is built from the checked segments,
each percent-encoded, never by resolving the caller's path.
"""
import logging
import re
import time
from typing import Optional
from urllib.parse import quote

import httpx2
from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import Response

from app.api.endpoints.data import _path_segments  # also registers the "anypath" convertor
from app.core.config import settings
from app.services.http_client import exc_text, get_client
from app.services.signed_auth import DEBUG_PREFIX, authorize_signed_request

logger = logging.getLogger(__name__)
router = APIRouter()

# A 64-hex-character Bee id: BatchID or SwarmAddress in Bee's OpenAPI.
HEX64 = object()
_HEX64_RE = re.compile(r"^[A-Fa-f0-9]{64}$")

# Every GET Bee 2.8.1 defines under the diagnostic endpoints (openapi/Swarm.yaml),
# and nothing else. None of them takes a query parameter. Bee has no GET
# /chequebook, so the bare name is not listed.
_ROUTES = (
    ("topology",), ("addresses",), ("health",), ("readiness",), ("peers",),
    ("chainstate",), ("reservestate",), ("redistributionstate",), ("node",),
    ("wallet",),
    ("status",), ("status", "peers"), ("status", "neighborhoods"),
    ("stamps",), ("stamps", HEX64), ("stamps", HEX64, "buckets"),
    ("batches",), ("batches", HEX64),
    ("chequebook", "address"), ("chequebook", "balance"), ("chequebook", "cheque"),
    ("chequebook", "cheque", HEX64), ("chequebook", "cashout", HEX64),
)

# Top-level names, listed in PATH_NOT_ALLOWED's `allowed`.
ALLOWED_BEE_PATHS = {route[0] for route in _ROUTES}


def _refuse(code: str, message: str, **extra) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                         detail={"code": code, "message": message, **extra})


def _route_matches(segments: list[str], route: tuple) -> bool:
    if len(segments) != len(route):
        return False
    return all(_HEX64_RE.match(seg) if part is HEX64 else seg == part
               for seg, part in zip(segments, route))


def _validated_segments(bee_path: str) -> list[str]:
    """The segments of an allow-listed Bee path, or a 403 (#693).

    The path has been percent-decoded once by the server. The checks of the
    collection-path route (#700) come first; then no '%' may remain, since no
    allowed segment contains one and it can only be a second encoding; then the
    segments must match one entry of _ROUTES exactly.
    """
    try:
        segments = _path_segments(bee_path)
    except ValueError as e:
        raise _refuse(code="DEBUG_PATH_INVALID", message=str(e))
    if "%" in bee_path:
        raise _refuse(code="DEBUG_PATH_INVALID", message="The path may not contain '%' (a second percent-encoding).")
    if not any(_route_matches(segments, route) for route in _ROUTES):
        shown = segments[0] if segments[0] not in ALLOWED_BEE_PATHS else bee_path
        raise _refuse(code="PATH_NOT_ALLOWED", message=f"'{shown}' is not a permitted debug path",
                      allowed=sorted(ALLOWED_BEE_PATHS))
    return segments


def _bee_url(segments: list[str]) -> str:
    """Bee URL from checked segments, each percent-encoded; keeps any base path."""
    base = str(settings.SWARM_BEE_API_URL).rstrip("/")
    return base + "/" + "/".join(quote(seg, safe="") for seg in segments)


# Kept as the module's own name for readability; defined once in signed_auth.
SIG_MESSAGE_PREFIX = DEBUG_PREFIX


def _authorize(timestamp: Optional[str], signature: Optional[str]) -> str:
    """Verify the request is signed by an allow-listed address over a fresh timestamp.

    Delegates to the shared helper. The message prefix stays exactly as it was,
    so signatures already in use keep working — but it is now passed explicitly,
    because the pool-maintenance endpoint uses the same mechanism with a
    different prefix and the two must not authorise each other.
    """
    return authorize_signed_request(
        prefix=SIG_MESSAGE_PREFIX,
        allowed=settings.get_debug_allowed_addresses(),
        timestamp=timestamp,
        signature=signature,
        operation="Bee diagnostics",
    )


@router.get("/bee/{bee_path:anypath}", summary="Read-only proxy to allow-listed Bee diagnostic endpoints")
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

    if request.url.query:
        raise _refuse(code="DEBUG_QUERY_NOT_ALLOWED", message="The debug proxy forwards no query string.")
    url = _bee_url(_validated_segments(bee_path))

    try:
        client = get_client()
        resp = await client.get(url, timeout=15)
    except httpx2.HTTPError as e:
        logger.warning(f"debug proxy: Bee request failed ({url}): {exc_text(e)}")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Bee node request failed")

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )
