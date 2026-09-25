# app/api/endpoints/pricing.py
"""
Price quotes for paid operations, without paying (#381).

Before this, a client could only learn a price by sending the real request and
reading the 402. This endpoint answers the same question up front: what would
each paid operation cost, for the size and duration the client has in mind.

Every quote is produced by the x402 dependency's own pricer
(_calculate_price_for_request, which is built on get_price_quote), fed a
request shaped like the one the client would send. That is deliberate: the
quote shown here and the amount the 402 then demands must not diverge, and
maintaining a second copy of the per-route pricing rules here would let them
drift the first time one side changes. Routing through the same function means
any change to how an operation is priced shows up here automatically.

This is a GET on an unprotected path, so it is never x402-gated, and it goes
through the same global rate limiter as every other GET.
"""
import json
import logging
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from starlette.requests import Request

from app.core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter()


async def _quote(path: str, body: Optional[dict] = None, query: str = "",
                 content_length: Optional[int] = None) -> dict:
    """Price a POST to `path` exactly as the x402 dependency would.

    Builds the request the client would send (JSON body, query string and
    Content-Length) and hands it to the dependency's pricer. Nothing is sent to
    the handler; only the price is computed.
    """
    from app.x402.dependency import _calculate_price_for_request

    raw = json.dumps(body).encode() if body is not None else b""
    length = content_length if content_length is not None else len(raw)
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "scheme": "http",
        "query_string": query.encode(),
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(length).encode()),
        ],
        "server": ("gateway", 80),
    }

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    priced = await _calculate_price_for_request(Request(scope, receive))
    price_usd = priced["price_usd"]
    return {
        "method": "POST",
        "path": path,
        "price_usd": price_usd,
        # Same conversion as create_payment_requirements, so this equals the
        # maxAmountRequired the 402 would carry.
        "max_amount_required": str(int(price_usd * 1_000_000)),
        "description": priced.get("description"),
    }


@router.get("/pricing", summary="Price quotes for paid operations")
async def get_pricing(
    size: Optional[Literal["small", "medium", "large"]] = Query(
        None, description="Stamp size preset, as in the purchase and pool-acquire bodies."),
    depth: Optional[int] = Query(
        None, ge=16, le=32, description="Stamp depth (advanced), as in the request bodies."),
    duration_hours: Optional[int] = Query(
        None, ge=24, description="Stamp duration in hours, as in the purchase body."),
    upload_bytes: int = Query(
        4096, ge=0, description="Size of the data to upload, in bytes."),
    mb: Optional[int] = Query(
        None, ge=1, description="Bandwidth credit top-up in MB, as in POST /chunks/credit?mb=."),
):
    """
    Quote the price of each paid operation **without paying**.

    The parameters mirror the bodies of the paid requests. Each quote is computed
    by the same function that prices the 402 for that request, so
    `max_amount_required` is the amount (USDC, 6 decimals) the 402 would ask for,
    at the current Bee chain price. Prices move with the chain price, so treat a
    quote as current rather than guaranteed.

    Operations are listed only when they are enabled on this gateway. When x402
    payments are disabled nothing is charged, and `quotes` is empty.

    - `stamp_purchase`: `POST /api/v1/stamps/` (`size`, `depth`, `duration_hours`)
    - `pool_acquire`: `POST /api/v1/pool/acquire` (`size`, `depth`). Payment is
      optional here; the free daily allowance is used first.
    - `data_upload`: `POST /api/v1/data/` (`upload_bytes`)
    - `bandwidth_credit`: `POST /api/v1/chunks/credit` (`mb`)
    """
    if not settings.X402_ENABLED:
        return {"x402_enabled": False, "quotes": {}}

    from app.x402.middleware import USDC_ADDRESSES

    size_body = {k: v for k, v in (("size", size), ("depth", depth)) if v is not None}
    stamp_body = dict(size_body)
    if duration_hours is not None:
        stamp_body["duration_hours"] = duration_hours

    api = settings.API_V1_STR
    try:
        quotes = {"stamp_purchase": await _quote(f"{api}/stamps/", body=stamp_body)}
        if settings.STAMP_POOL_ENABLED:
            quotes["pool_acquire"] = await _quote(f"{api}/pool/acquire", body=size_body)
        quotes["data_upload"] = await _quote(f"{api}/data/", content_length=upload_bytes)
        if settings.CHUNK_UPLOAD_ENABLED:
            query = f"mb={mb}" if mb is not None else ""
            quotes["bandwidth_credit"] = await _quote(f"{api}/chunks/credit", query=query)
    except Exception as e:
        # The pricer needs the current chain price from Bee.
        logger.error(f"Pricing: failed to compute quotes: {e}")
        raise HTTPException(
            status_code=503,
            detail={
                "code": "PRICING_UNAVAILABLE",
                "message": "Could not compute prices right now (the chain price is unavailable). Retry shortly.",
            },
        )

    network = settings.X402_NETWORK
    return {
        "x402_enabled": True,
        "currency": "USDC",
        "network": network,
        "asset": USDC_ADDRESSES.get(network, USDC_ADDRESSES["base-sepolia"]),
        "pay_to": settings.X402_PAY_TO_ADDRESS,
        "min_price_usd": settings.X402_MIN_PRICE_USD,
        "free_tier": {
            "enabled": settings.X402_FREE_TIER_ENABLED,
            "header": "X-Payment-Mode: free",
        },
        "quotes": quotes,
    }
