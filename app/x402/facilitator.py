# app/x402/facilitator.py
"""
The x402 facilitator client, and the checks that keep x402 configuration safe.

Mainnet facilitators authenticate the gateway (#369). The public x402.org
facilitator serves test networks only and takes no credentials; a mainnet
facilitator such as Coinbase CDP signs each request with an API key. The x402
SDK supports this through FacilitatorConfig.create_headers, which the gateway
did not use: it built its clients (in two places) from a URL alone.

Two ways to authenticate, both optional:
- X402_FACILITATOR_CDP_API_KEY_ID / _SECRET: the Coinbase CDP facilitator, via
  the cdp-sdk package (installed separately; the gateway refuses to start if
  these are set and it is missing). Note: CDP's facilitator route is x402 v2,
  and this gateway speaks x402 v1 (x402==1.0.0); whether CDP accepts v1
  payloads must be confirmed before relying on it (see #373).
- X402_FACILITATOR_BEARER_TOKEN: a static bearer token, for facilitators that
  accept one.

validate_x402_config() runs at startup and refuses a configuration that would
otherwise fail on the first payment, or take payment in the wrong asset (#370).
"""
import logging
import re
from typing import Optional

from x402.facilitator import FacilitatorClient, FacilitatorConfig

from app.core.config import settings, is_testnet_network

logger = logging.getLogger(__name__)

_client: Optional[FacilitatorClient] = None

PUBLIC_TESTNET_FACILITATOR = "x402.org"


CDP_HOST = "api.cdp.coinbase.com"


def _host(url: str) -> str:
    from urllib.parse import urlparse
    return (urlparse(url).hostname or "").lower()


def _config() -> FacilitatorConfig:
    url = settings.X402_FACILITATOR_URL
    if settings.X402_FACILITATOR_CDP_API_KEY_ID:
        from cdp.x402 import create_facilitator_config  # optional dependency
        cdp = create_facilitator_config(
            settings.X402_FACILITATOR_CDP_API_KEY_ID,
            settings.X402_FACILITATOR_CDP_API_KEY_SECRET,
        )
        cdp_headers = cdp["create_headers"]

        # cdp-sdk builds headers synchronously (a fresh JWT per call), while the
        # x402 1.x client awaits create_headers. Adapt rather than crash on the
        # first payment.
        async def create_headers():
            headers = cdp_headers()
            return await headers if hasattr(headers, "__await__") else headers

        # CDP signs its tokens for its own host, so only its own URL (or an
        # explicit URL on that host) is used; validate_x402_config() refuses
        # any other host rather than sending CDP credentials there.
        return {"url": url if url and _host(url) == CDP_HOST else cdp["url"], "create_headers": create_headers}

    config: FacilitatorConfig = {"url": url}
    token = settings.X402_FACILITATOR_BEARER_TOKEN
    if token:
        async def create_headers():
            auth = {"Authorization": f"Bearer {token}"}
            return {"verify": auth, "settle": auth, "list": auth}
        config["create_headers"] = create_headers
    return config


def get_facilitator_client() -> FacilitatorClient:
    global _client
    if _client is None:
        _client = FacilitatorClient(config=_config())
    return _client


def reset_facilitator_client() -> None:
    global _client
    _client = None


def validate_x402_config() -> None:
    """Refuse to start with an x402 configuration that cannot work safely."""
    if not settings.X402_ENABLED:
        return
    from app.x402.middleware import USDC_ADDRESSES

    problems = []
    network = settings.X402_NETWORK or ""
    if network not in USDC_ADDRESSES:
        problems.append(
            f"X402_NETWORK={network!r} is not one of {sorted(USDC_ADDRESSES)}; payments would "
            f"be requested in an unknown asset"
        )
    pay_to = settings.X402_PAY_TO_ADDRESS or ""
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", pay_to):
        problems.append("X402_PAY_TO_ADDRESS must be a 0x-prefixed 20-byte address")
    elif int(pay_to, 16) == 0:
        problems.append("X402_PAY_TO_ADDRESS is the zero address; payments would be burned")
    elif pay_to != pay_to.lower() and pay_to[2:] != pay_to[2:].upper():
        from eth_utils import is_checksum_address
        if not is_checksum_address(pay_to):
            problems.append("X402_PAY_TO_ADDRESS has mixed case but an invalid checksum (likely a typo)")

    url = settings.X402_FACILITATOR_URL or ""
    uses_cdp = bool(settings.X402_FACILITATOR_CDP_API_KEY_ID)
    uses_bearer = bool(settings.X402_FACILITATOR_BEARER_TOKEN)
    mainnet = bool(network) and not is_testnet_network(network)
    if uses_cdp and uses_bearer:
        problems.append("set either CDP credentials or X402_FACILITATOR_BEARER_TOKEN, not both")
    if uses_cdp:
        if url and _host(url) != CDP_HOST:
            problems.append(f"CDP credentials are set but X402_FACILITATOR_URL points at {_host(url)!r}; "
                            f"leave it empty or use {CDP_HOST}")
        if not settings.X402_FACILITATOR_CDP_API_KEY_SECRET:
            problems.append("X402_FACILITATOR_CDP_API_KEY_SECRET is required with X402_FACILITATOR_CDP_API_KEY_ID")
        try:
            import cdp.x402  # noqa: F401
        except ImportError:
            problems.append("CDP facilitator credentials are set but the cdp-sdk package is not installed")
    else:
        if not url.startswith(("http://", "https://")):
            problems.append("X402_FACILITATOR_URL must be an http(s) URL")
        if mainnet and PUBLIC_TESTNET_FACILITATOR in _host(url):
            problems.append(
                f"X402_NETWORK={network!r} is a mainnet, but X402_FACILITATOR_URL points at the public "
                f"test-network facilitator ({url}); configure a mainnet facilitator"
            )
        if (mainnet or uses_bearer) and url.startswith("http://"):
            problems.append("X402_FACILITATOR_URL must use https on a mainnet or with credentials")
    if problems:
        raise RuntimeError("Refusing to start with x402 enabled: " + "; ".join(problems))
    used = get_facilitator_client().config["url"] if uses_cdp else url
    logger.info(f"x402 configuration OK: network={network}, facilitator={used}"
                + (" (CDP auth)" if uses_cdp else " (bearer auth)" if settings.X402_FACILITATOR_BEARER_TOKEN else ""))
