# app/x402/access.py
"""
Access control for x402 payments (whitelist/blacklist).

This module handles IP-based and wallet-based access control:
- Blacklist: Block specific IPs or wallets (403 Forbidden)
- Whitelist: Allow free access for specific IPs or wallets (bypass x402)

TODO: Implementation pending - see Issue #5
"""
from typing import Tuple, Optional


def check_access(
    client_ip: str,
    wallet_address: Optional[str] = None
) -> Tuple[str, Optional[str]]:
    """
    Check if a client is allowed to access the service.

    Args:
        client_ip: Client's IP address
        wallet_address: Optional wallet address from payment signature

    Returns:
        Tuple of (access_status, reason):
        - ("blocked", reason) - Client is blacklisted
        - ("free", None) - Client is whitelisted (free access)
        - ("pay", None) - Client must pay via x402
    """
    # TODO: Implement actual access control
    raise NotImplementedError("Access control not yet implemented - see Issue #5")


def is_ip_blacklisted(ip: str) -> bool:
    """Check if an IP is in the blacklist."""
    # TODO: Implement
    raise NotImplementedError("Access control not yet implemented - see Issue #5")


def is_ip_whitelisted(ip: str) -> bool:
    """Check if an IP is in the whitelist (free access)."""
    # TODO: Implement
    raise NotImplementedError("Access control not yet implemented - see Issue #5")
