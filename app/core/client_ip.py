# app/core/client_ip.py
"""Identify the calling client, for limits that are keyed on who is calling.

Three controls key on this value: the global per-IP rate limit, the x402
free-tier rate limit, and the daily spend budget (#102). A caller who can change
the value at will is not limited by any of them.

## Why the previous approach was a trap rather than a bug

There were two near-identical copies of this function, in `app/x402/middleware.py`
and `app/middleware/rate_limit.py`. Both took the FIRST entry of
`X-Forwarded-For` and trusted it:

    return forwarded_for.split(",")[0].strip()

`X-Forwarded-For` is a list, appended to by each proxy it passes through, so the
leftmost entry is the one FURTHEST from us — and if any proxy appends rather than
replaces, that entry is whatever the client sent. A caller rotating the header
would then get a fresh rate-limit window and a fresh spend budget on every
request. That is the same failure the pool allowance had when every distinct
`Origin` minted a new budget.

It was not exploitable: Caddy is configured to replace the header rather than
append, verified on 2026-09-09 by exhausting the free-tier limit and retrying
with three different forged values — all still refused. But the safety lived
entirely in the proxy's configuration, nothing in this repository recorded the
dependency, and no test pinned it. Setting `trusted_proxies` in Caddy, putting a
CDN in front, or moving to a load balancer that appends would have made all three
controls bypassable with nothing failing to say so.

## What this does instead

Count back from the RIGHT by the number of proxies we actually have in front of
us. The rightmost entry is the one our own proxy added and is therefore the only
entry we know to be truthful; each further step left is one hop further out.

With `TRUSTED_PROXY_HOPS = 1` this is correct whether the proxy replaces or
appends, which is the property worth having — it stops depending on a behaviour
nobody here controls:

    header "1.2.3.4"                    (Caddy replaced)  -> 1.2.3.4
    header "9.9.9.9, 1.2.3.4"           (Caddy appended)  -> 1.2.3.4
    header "9.9.9.9, 8.8.8.8, 1.2.3.4"  (forged chain)    -> 1.2.3.4

A forged chain cannot reach past the hop count, because entries to the right of
the client are added by infrastructure we control.
"""
import logging
from typing import Optional

from fastapi import Request

from app.core.config import settings

logger = logging.getLogger(__name__)

UNKNOWN = "unknown"


def _peer(request: Request) -> str:
    """The direct TCP peer — never forgeable, but it is the proxy when behind one."""
    if request.client and request.client.host:
        return request.client.host
    return UNKNOWN


def get_client_ip(request: Request) -> str:
    """The calling client's address, as far as the proxy configuration allows.

    Reads `TRUSTED_PROXY_HOPS`: the number of proxies between the internet and
    the application. 0 means the application is directly exposed, in which case
    forwarding headers are ignored entirely rather than half-trusted.
    """
    hops = settings.TRUSTED_PROXY_HOPS

    # Directly exposed: the headers carry no authority at all, so reading them
    # would be strictly worse than using the connection we actually have.
    if hops <= 0:
        return _peer(request)

    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        parts = [p.strip() for p in forwarded_for.split(",") if p.strip()]
        if parts:
            index = len(parts) - hops
            if index >= 0:
                return parts[index]
            # Fewer entries than configured hops: the header disagrees with the
            # deployment. Falling back to the peer is the safe direction — it may
            # group callers together behind a proxy, which over-limits, rather
            # than trusting a value that is short by a hop, which under-limits.
            logger.warning(
                "X-Forwarded-For has %d entries but TRUSTED_PROXY_HOPS is %d; "
                "using the direct peer instead. Check the setting against the "
                "actual number of proxies.",
                len(parts), hops,
            )
            return _peer(request)

    # Single-valued alternative set by some proxies. Equally forgeable, so it is
    # read only where a proxy is expected to be setting it.
    real_ip = request.headers.get("X-Real-IP")
    if real_ip and real_ip.strip():
        return real_ip.strip()

    return _peer(request)
