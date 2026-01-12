# app/x402/access.py
"""
Access control for x402 payment gateway.

This module provides IP-based access control:
- Blacklist: Block specific IPs from using the gateway
- Whitelist: Allow specific IPs to bypass x402 payment requirements

Configuration is loaded from app/core/config.py:
- X402_BLACKLIST_IPS: Comma-separated list of blocked IPs
- X402_WHITELIST_IPS: Comma-separated list of IPs that bypass payment
"""
import logging
import ipaddress
from typing import Optional, Set, Tuple

from app.core.config import settings

logger = logging.getLogger(__name__)


def parse_ip_list(ip_string: Optional[str]) -> Set[str]:
    """
    Parse a comma-separated IP list into a set.

    Handles:
    - Individual IPs: "192.168.1.1"
    - CIDR notation: "192.168.0.0/24"
    - Whitespace trimming

    Args:
        ip_string: Comma-separated IP addresses/ranges

    Returns:
        Set of normalized IP strings/CIDR ranges
    """
    if not ip_string or not ip_string.strip():
        return set()

    result = set()
    for item in ip_string.split(","):
        item = item.strip()
        if not item:
            continue

        # Validate and normalize the IP/CIDR
        try:
            if "/" in item:
                # CIDR notation
                network = ipaddress.ip_network(item, strict=False)
                result.add(str(network))
            else:
                # Single IP
                ip = ipaddress.ip_address(item)
                result.add(str(ip))
        except ValueError as e:
            logger.warning(f"Invalid IP address/range in config: {item} - {e}")
            continue

    return result


def ip_matches_list(client_ip: str, ip_list: Set[str]) -> bool:
    """
    Check if a client IP matches any entry in the IP list.

    Supports:
    - Exact IP match
    - CIDR range matching

    Args:
        client_ip: The client's IP address
        ip_list: Set of IPs/CIDR ranges to check against

    Returns:
        True if the client IP matches any entry
    """
    if not ip_list:
        return False

    try:
        client = ipaddress.ip_address(client_ip)
    except ValueError:
        logger.warning(f"Invalid client IP address: {client_ip}")
        return False

    for entry in ip_list:
        try:
            if "/" in entry:
                # CIDR range check
                network = ipaddress.ip_network(entry, strict=False)
                if client in network:
                    return True
            else:
                # Exact match
                if str(client) == entry:
                    return True
        except ValueError:
            continue

    return False


def is_ip_blacklisted(client_ip: str) -> bool:
    """
    Check if a client IP is blacklisted.

    Blacklisted IPs are completely blocked from using the gateway.

    Args:
        client_ip: The client's IP address

    Returns:
        True if the IP is blacklisted
    """
    blacklist = parse_ip_list(settings.X402_BLACKLIST_IPS)
    is_blocked = ip_matches_list(client_ip, blacklist)

    if is_blocked:
        logger.warning(f"Blocked blacklisted IP: {client_ip}")

    return is_blocked


def is_ip_whitelisted(client_ip: str) -> bool:
    """
    Check if a client IP is whitelisted.

    Whitelisted IPs can bypass x402 payment requirements.

    Args:
        client_ip: The client's IP address

    Returns:
        True if the IP is whitelisted
    """
    whitelist = parse_ip_list(settings.X402_WHITELIST_IPS)
    is_allowed = ip_matches_list(client_ip, whitelist)

    if is_allowed:
        logger.info(f"Allowing whitelisted IP to bypass payment: {client_ip}")

    return is_allowed


def check_access(
    client_ip: str,
    wallet_address: Optional[str] = None
) -> Tuple[str, Optional[str]]:
    """
    Check if a client is allowed to access the service.

    This is the main entry point for access control in the middleware.

    Args:
        client_ip: Client's IP address
        wallet_address: Optional wallet address from payment signature (reserved for future use)

    Returns:
        Tuple of (access_status, reason):
        - ("blocked", reason) - Client is blacklisted
        - ("free", None) - Client is whitelisted (free access)
        - ("pay", None) - Client must pay via x402
    """
    # First check blacklist - blocked IPs cannot proceed at all
    if is_ip_blacklisted(client_ip):
        return ("blocked", "IP address is blocked")

    # Check whitelist - whitelisted IPs bypass payment
    if is_ip_whitelisted(client_ip):
        return ("free", None)

    # Normal access - requires payment
    return ("pay", None)


def get_access_control_status() -> dict:
    """
    Get current access control configuration status.

    Useful for diagnostics and admin endpoints.

    Returns:
        Dict with blacklist and whitelist info
    """
    blacklist = parse_ip_list(settings.X402_BLACKLIST_IPS)
    whitelist = parse_ip_list(settings.X402_WHITELIST_IPS)

    return {
        "blacklist_count": len(blacklist),
        "whitelist_count": len(whitelist),
        "blacklist_entries": sorted(list(blacklist)),
        "whitelist_entries": sorted(list(whitelist)),
    }
