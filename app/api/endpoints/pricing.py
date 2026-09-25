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

This is a GET on an unprotected path, so it is never x402-gated. It is under
the global rate limiter only when x402 is off: with x402 on that limiter is not
installed, and no GET is rate-limited. So the chain price is read from Bee once
per call and reused for every quote, which makes a call cost the same one Bee
request as an unpaid POST that gets a 402. Caching it across calls is #434.
"""
import json
import logging
from typing import Any, Literal, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query
from starlette.requests import Request

from app.core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter()


async def _quote(path: str, body: Optional[dict] = None, query: str = "",
                 content_length: Optional[int] = None) -> Tuple[dict, Any]:
    """Price a POST to `path` exactly as the x402 dependency would.

    Returns the quote and the PaymentRequirements the 402 would carry.

    Builds the request the client would send (JSON body, query string and
    Content-Length) and hands it to the dependency's pricer. Nothing is sent to
    the handler; only the price is computed.
    """
    from app.x402.dependency import _calculate_price_for_request
    from app.x402.middleware import create_payment_requirements

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

    request = Request(scope, receive)
    priced = await _calculate_price_for_request(request)
    # Built by the same function as the 402's payment requirements, so the
    # amount, network, asset and payTo here are exactly what the 402 carries.
    requirements = create_payment_requirements(
        request=request, price_usd=priced["price_usd"],
        description=priced.get("description", "Gateway operation"),
    )
    return {
        "method": "POST",
        "path": path,
        "price_usd": priced["price_usd"],
        "max_amount_required": requirements.max_amount_required,
        "description": requirements.description,
    }, requirements


@router.get("/pricing", summary="Price quotes for paid operations")
async def get_pricing(
    size: Optional[Literal["small", "medium", "large"]] = Query(
        None, description="Stamp size preset, as in the purchase and pool-acquire bodies."),
    depth: Optional[int] = Query(
        None, ge=16, le=32, description="Stamp depth (advanced), as in the request bodies."),
    duration_hours: Optional[int] = Query(
        None, ge=24, description="Stamp duration in hours, as in the purchase body."),
    upload_bytes: int = Query(
        4096, ge=0,
        description="Size of the upload request body in bytes: the file plus the multipart "
                    "overhead (a few hundred bytes), which is what the 402 prices."),
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

    from app.x402.pricing import get_chainstate, pinned_chainstate

    # Bound to what the real endpoints accept, so an out-of-range value is
    # refused as such instead of surfacing as a pricing failure.
    max_upload = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
    if upload_bytes > max_upload:
        raise HTTPException(status_code=422, detail={
            "code": "FILE_TOO_LARGE",
            "message": f"upload_bytes exceeds the maximum upload size of {settings.MAX_UPLOAD_SIZE_MB} MB.",
        })
    if mb is not None and mb > settings.BANDWIDTH_CREDIT_MAX_TOPUP_MB:
        raise HTTPException(status_code=422, detail={
            "code": "TOPUP_TOO_LARGE",
            "message": f"mb exceeds the maximum single top-up of {settings.BANDWIDTH_CREDIT_MAX_TOPUP_MB} MB.",
        })

    # One Bee request per call, shared by every quote below (see the module
    # docstring). Only this read is reported as PRICING_UNAVAILABLE; a failure
    # while pricing is a bug, and is left to surface as a 500.
    try:
        chainstate = await get_chainstate()
        if int(chainstate.get("currentPrice", 0)) <= 0:
            raise ValueError("chainstate has no usable currentPrice")
    except Exception as e:
        logger.error(f"Pricing: failed to read the chain price: {e}")
        raise HTTPException(
            status_code=503,
            detail={
                "code": "PRICING_UNAVAILABLE",
                "message": "Could not compute prices right now (the chain price is unavailable). Retry shortly.",
            },
        )

    size_body = {k: v for k, v in (("size", size), ("depth", depth)) if v is not None}
    stamp_body = dict(size_body)
    if duration_hours is not None:
        stamp_body["duration_hours"] = duration_hours

    api = settings.API_V1_STR
    quotes = {}
    with pinned_chainstate(chainstate):
        quotes["stamp_purchase"], requirements = await _quote(f"{api}/stamps/", body=stamp_body)
        if settings.STAMP_POOL_ENABLED:
            quotes["pool_acquire"], _ = await _quote(f"{api}/pool/acquire", body=size_body)
        quotes["data_upload"], _ = await _quote(f"{api}/data/", content_length=upload_bytes)
        if settings.CHUNK_UPLOAD_ENABLED:
            query = f"mb={mb}" if mb is not None else ""
            quotes["bandwidth_credit"], _ = await _quote(f"{api}/chunks/credit", query=query)

    return {
        "x402_enabled": True,
        "currency": "USDC",
        "network": requirements.network,
        "asset": requirements.asset,
        "pay_to": requirements.pay_to,
        "min_price_usd": settings.X402_MIN_PRICE_USD,
        "free_tier": {
            "enabled": settings.X402_FREE_TIER_ENABLED,
            "header": "X-Payment-Mode: free",
        },
        "quotes": quotes,
    }
