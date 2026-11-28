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

## Only addresses become keys (#697)

Without a proxy in front, the header is whatever the caller sent. Two forms
used to pass every limit: `X-Real-IP`, read whenever `X-Forwarded-For` was
absent (a fresh key per request), and the literal value "unknown", which the
x402 free-tier limiter did not limit at all. Now X-Real-IP is not read, every
X-Forwarded-For entry we rely on must parse as an IP address or the direct peer
is used, and "unknown" (no peer at all) is limited as one shared caller.
"""
import ipaddress
import logging
from typing import Optional

from fastapi import Request

from app.core.config import settings

logger = logging.getLogger(__name__)

UNKNOWN = "unknown"


def _peer(request: Request) -> str:
    """The direct TCP peer — never forgeable, but it is the proxy when behind one.

    Used as it is, address or not (a test client's peer is "testclient"). The
    UNKNOWN key exists only for a connection with no peer at all, and every
    limit keyed on the caller limits it as one shared caller (#697).
    """
    if request.client and request.client.host:
        return request.client.host
    return UNKNOWN


def _valid_ip(value: str) -> Optional[str]:
    """The value as a normalised IP address, or None if it is not one.

    Header entries are caller-written text. Anything that is not an address —
    "unknown", an obfuscated identifier, an address with a port, garbage — must
    not become a limit key, or every distinct string is a fresh budget (#697).
    """
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def _forwarded_for(headers) -> str:
    """Every X-Forwarded-For line, joined as one list (RFC 9110 5.3).

    Reading only the first line would let a caller's own line win over the one
    a proxy adds as a separate field.
    """
    getlist = getattr(headers, "getlist", None)
    if getlist is not None:
        return ",".join(getlist("X-Forwarded-For"))
    return headers.get("X-Forwarded-For") or ""


def get_client_ip(request: Request) -> str:
    """The calling client's address, as far as the proxy configuration allows.

    Reads `TRUSTED_PROXY_HOPS`: the number of proxies between the internet and
    the application. 0 means the application is directly exposed, in which case
    forwarding headers are ignored entirely rather than half-trusted.

    Only X-Forwarded-For is read. X-Real-IP is not (#697): the hosted proxy does
    not set it, and when X-Forwarded-For was absent it handed the caller a key
    of their own choosing.
    """
    hops = settings.TRUSTED_PROXY_HOPS

    # Directly exposed: the headers carry no authority at all, so reading them
    # would be strictly worse than using the connection we actually have.
    if hops <= 0:
        return _peer(request)

    parts = [p.strip() for p in _forwarded_for(request.headers).split(",") if p.strip()]
    if parts:
        index = len(parts) - hops
        if index >= 0:
            # The chosen entry and everything to its right were written by
            # proxies we run, so all of them must be addresses. One that is not
            # means the header is not what the deployment produces; the peer
            # is the safe answer, as for a short chain below (#697).
            trusted = [_valid_ip(p) for p in parts[index:]]
            if all(trusted):
                return trusted[0]
            logger.warning(
                "X-Forwarded-For entry %r is not an IP address; using the "
                "direct peer instead.",
                parts[index:][trusted.index(None)][:64],
            )
            return _peer(request)
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

    return _peer(request)


def client_key(ip: str) -> str:
    """The key per-caller limits use for an address (#367).

    IPv6 clients are normally given a whole /64, so keying on the full address
    lets one client present billions of fresh ones. IPv6 addresses are grouped
    by their /64; IPv4 addresses are used as they are.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if addr.version == 6:
        if addr.ipv4_mapped:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


def get_client_key(request: Request) -> str:
    """get_client_ip() grouped for per-caller limits (see client_key)."""
    return client_key(get_client_ip(request))
