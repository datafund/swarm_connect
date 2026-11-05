# app/x402/networks.py
"""x402 network identifiers: v1 names and CAIP-2 ids (#599, D6 in #596).

x402 v1 names networks ("base", "base-sepolia"); x402 v2 uses CAIP-2 ids
("eip155:8453", "eip155:84532"). The gateway keeps one internal form, CAIP-2,
and converts at the edges: tables are keyed by CAIP-2, and everything a v1
client or facilitator reads (402 bodies, verify and settle requests, /pricing)
keeps the v1 name.

X402_NETWORK accepts either form, exactly as written here. Anything else is
unknown, and validate_x402_config() refuses to start with it.
"""
from typing import Optional

# The one map between the two forms. Add a network here and in the USDC
# tables in app/x402/middleware.py, nowhere else.
V1_TO_CAIP2 = {
    "base": "eip155:8453",
    "base-sepolia": "eip155:84532",
}
CAIP2_TO_V1 = {caip2: v1 for v1, caip2 in V1_TO_CAIP2.items()}


def to_caip2(network: Optional[str]) -> Optional[str]:
    """The CAIP-2 id of a known network given in either form, else None."""
    if network in V1_TO_CAIP2:
        return V1_TO_CAIP2[network]
    if network in CAIP2_TO_V1:
        return network
    return None


def to_v1(network: Optional[str]) -> Optional[str]:
    """The v1 name of a network given in either form.

    An unknown value is returned unchanged: the gateway does not start with
    one, and this must not turn it into a different known network.
    """
    return CAIP2_TO_V1.get(network, network)


def accepted_network_values() -> list:
    """Every X402_NETWORK value the gateway starts with, sorted."""
    return sorted(set(V1_TO_CAIP2) | set(CAIP2_TO_V1))
