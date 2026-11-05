# app/x402/facilitator.py
"""
The x402 facilitator client, and the checks that keep x402 configuration safe.

Mainnet facilitators authenticate the gateway (#369). The public x402.org
facilitator serves test networks only and takes no credentials; a mainnet
facilitator such as Coinbase CDP signs each request with an API key, through
FacilitatorConfig.create_headers. The client is the gateway's own
(app/x402/facilitator_client.py, #604), a drop-in for x402 1.0.0's.

Two ways to authenticate, both optional:
- X402_FACILITATOR_CDP_API_KEY_ID / _SECRET: the Coinbase CDP facilitator, via
  the cdp-sdk package (pinned in requirements.lock, so the image has it; the
  gateway refuses to start if these are set and it is missing). Note: CDP's
  facilitator route is x402 v2, and this gateway speaks x402 v1;
  whether CDP accepts v1 payloads must be confirmed before relying on it (see
  #524). cdp-sdk signs each token for one path on its x402 route
  (/platform/v2/x402/verify, /settle), so with CDP credentials the gateway
  always calls that route: X402_FACILITATOR_URL must be empty or exactly
  CDP_FACILITATOR_URL, and startup refuses anything else (#527).
- X402_FACILITATOR_BEARER_TOKEN: a static bearer token, for facilitators that
  accept one.

validate_x402_config() runs at startup and refuses a configuration that would
otherwise fail on the first payment, or take payment in the wrong asset (#370).
"""
import logging
import re
from typing import Optional

from app.x402.facilitator_client import FacilitatorClient, FacilitatorConfig

from app.core.config import settings, is_testnet_network

logger = logging.getLogger(__name__)

_client: Optional[FacilitatorClient] = None

PUBLIC_TESTNET_FACILITATOR = "x402.org"


CDP_HOST = "api.cdp.coinbase.com"
# The URL cdp-sdk returns, and the base of the paths it signs its tokens for
# ({url}/verify, {url}/settle). The x402 client appends /verify and /settle to
# the configured URL, so any other value (the bare host, say) would send a
# token signed for one path to another (#527). A test checks this against the
# installed cdp-sdk.
CDP_FACILITATOR_URL = f"https://{CDP_HOST}/platform/v2/x402"


def _host(url: str) -> str:
    from urllib.parse import urlparse
    return (urlparse(url).hostname or "").lower()


def _strip_one_slash(url: str) -> str:
    # The x402 client drops one trailing slash before appending /verify.
    return url[:-1] if url.endswith("/") else url


def _config() -> FacilitatorConfig:
    url = settings.X402_FACILITATOR_URL
    if settings.X402_FACILITATOR_CDP_API_KEY_ID:
        from cdp.x402 import create_facilitator_config  # optional dependency
        cdp = create_facilitator_config(
            settings.X402_FACILITATOR_CDP_API_KEY_ID,
            settings.X402_FACILITATOR_CDP_API_KEY_SECRET,
        )
        cdp_headers = cdp["create_headers"]

        # cdp-sdk builds headers synchronously (a fresh JWT per call). The
        # client accepts either kind of hook; the async wrapper keeps this one
        # the same kind as the bearer hook below.
        async def create_headers():
            headers = cdp_headers()
            return await headers if hasattr(headers, "__await__") else headers

        # CDP signs each token for one path on its own route, so the URL
        # cdp-sdk returns is always used, never the configured one.
        # validate_x402_config() refuses any configured URL other than that
        # route, so the two agree (#527).
        return {"url": cdp["url"], "create_headers": create_headers}

    config: FacilitatorConfig = {"url": url}
    token = settings.X402_FACILITATOR_BEARER_TOKEN
    if token:
        async def create_headers():
            auth = {"Authorization": f"Bearer {token}"}
            # "supported": the startup /supported check (#608) is authenticated too.
            return {"verify": auth, "settle": auth, "supported": auth, "list": auth}
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
    from app.x402.networks import accepted_network_values, to_caip2

    problems = []
    network = settings.X402_NETWORK or ""
    # A v1 name or a CAIP-2 id (#599); the asset table is keyed by CAIP-2.
    if to_caip2(network) not in USDC_ADDRESSES:
        problems.append(
            f"X402_NETWORK={network!r} is not one of {accepted_network_values()}; payments would "
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
                            f"leave it empty or set it to {CDP_FACILITATOR_URL}")
        elif url and _strip_one_slash(url) != CDP_FACILITATOR_URL:
            # Never echo the value: the deploy log is public (#485).
            problems.append(f"CDP credentials are set but X402_FACILITATOR_URL is not CDP's x402 route; "
                            f"CDP signs its tokens only for {CDP_FACILITATOR_URL}/verify and /settle, "
                            f"so leave it empty or set it to exactly {CDP_FACILITATOR_URL}")
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


def _v2_refusal(reason: str) -> RuntimeError:
    return RuntimeError(
        "Refusing to start with x402 enabled: " + f"X402_PROTOCOL_VERSIONS includes 2, but {reason}. The gateway advertises and "
        f"accepts x402 v2 only with a facilitator that settles it; fix the facilitator, or set "
        f"X402_PROTOCOL_VERSIONS=1")


async def check_facilitator_v2_support(client: Optional[FacilitatorClient] = None) -> None:
    """With x402 v2 enabled, refuse to start unless the facilitator settles it (#608, D3 in #596).

    Calls GET {facilitator}/supported once and requires a kind with
    x402Version 2, scheme "exact" and the configured network as its CAIP-2 id
    (eip155:84532, not base-sepolia), in a 2xx answer. Fails closed: an
    unreachable facilitator, a request that cannot be made (the credential
    hook raising, an invalid URL), a non-2xx answer whatever its body, or a
    2xx answer that cannot be read (also one over 1 MiB, or nested deep
    enough to exhaust the parser), is refused like one that does not list
    it. x402Version must be the int 2, as the spec has it. Does nothing when
    x402 is off or 2 is not enabled, so the default configuration never
    calls /supported.

    Messages name the facilitator host and, of what it lists, only values of
    a safe form (CAIP-2 ids, v1 names, short lower-case schemes; anything
    else as <invalid>), at most 10 of each: the deploy log is public.
    """
    if not settings.X402_ENABLED or 2 not in settings.x402_protocol_versions():
        return
    from app.x402.networks import to_caip2
    network = to_caip2(settings.X402_NETWORK)
    client = client or get_facilitator_client()
    host = _host(client.config["url"]) or "the facilitator"
    import httpx
    from app.x402.facilitator_client import MAX_SUPPORTED_BYTES, SupportedAnswerTooLarge
    from app.x402.models_v1 import SupportedResponse
    try:
        response = await client.supported_response()
    except SupportedAnswerTooLarge as e:
        raise _v2_refusal(f"the facilitator's /supported on {host} answered HTTP {e.status} with "
                          f"something that is not a list of payment kinds (larger than "
                          f"{MAX_SUPPORTED_BYTES} bytes)") from None
    except httpx.HTTPError as e:
        raise _v2_refusal(f"the facilitator's /supported on {host} could not be reached "
                          f"({type(e).__name__}), so it is unknown whether it settles x402 v2 exact "
                          f"on {network}") from None
    except Exception as e:
        # Anything else before an answer: the CDP header hook raising, an
        # httpx error outside HTTPError (InvalidURL), a bad header value. A
        # refusal, not a traceback, and never the error text (it may hold a
        # credential or the URL).
        raise _v2_refusal(f"the request to the facilitator's /supported on {host} could not be made "
                          f"({type(e).__name__}; check the facilitator URL and credentials)") from None
    status = response.status_code
    if not 200 <= status < 300:
        # Not an answer to trust, whatever its body says (CDP answers 401 in
        # plain text without valid credentials).
        raise _v2_refusal(f"the facilitator's /supported on {host} answered HTTP {status}, not a list "
                          f"of payment kinds (a 401 or 403 is an authentication error)")
    try:
        data = response.json()
        SupportedResponse(**data)       # the shape: an object with a list of kinds
        kinds = [k for k in data["kinds"] if isinstance(k, dict)]
    except Exception as e:
        # Any parse failure: not JSON (ValueError), not an object (TypeError),
        # the wrong shape (ValidationError), nesting deep enough to exhaust the
        # parser (RecursionError, a RuntimeError).
        raise _v2_refusal(f"the facilitator's /supported on {host} answered HTTP {status} with "
                          f"something that is not a list of payment kinds ({type(e).__name__})") from None
    # Exact types, as the spec has them: x402Version the int 2 (not "2", 2.0
    # or true), scheme and network strings. Read from the JSON itself, since
    # the v1 model coerces.
    def _v2_exact(k):
        v = k.get("x402Version")
        return type(v) is int and v == 2 and k.get("scheme") == "exact"
    if any(_v2_exact(k) and k.get("network") == network for k in kinds):
        logger.info(f"x402 v2: the facilitator on {host} settles x402 v2 exact on {network}")
        return
    v2_exact = _shown(_safe_network(k.get("network")) for k in kinds if _v2_exact(k))
    on_network = _shown(_kind_label(k) for k in kinds
                        if isinstance(k.get("network"), str) and to_caip2(k["network"]) == network)
    raise _v2_refusal(
        f"the facilitator on {host} does not list x402Version 2, scheme exact, network {network} in "
        f"/supported (it lists v2 exact for {v2_exact or 'no network'}; for {network}, in either "
        f"name form: {on_network or 'nothing'})")


# What a refusal may repeat from a facilitator's answer (#608). The message
# goes to the deploy log, which is public, so only values of a known safe
# form are shown, at most _SHOWN_MAX of them.
_SCHEME_RE = re.compile(r"^[a-z0-9-]{1,32}\Z")
_SHOWN_MAX = 10


def _safe_network(value) -> str:
    from app.x402.models_v2 import CAIP2_RE
    from app.x402.networks import V1_TO_CAIP2
    if isinstance(value, str) and (CAIP2_RE.match(value) or value in V1_TO_CAIP2):
        return value
    return "<invalid>"


def _kind_label(kind: dict) -> str:
    v = kind.get("x402Version")
    version = str(v) if type(v) is int and 0 <= v < 1000 else "?"
    scheme = kind.get("scheme")
    scheme = scheme if isinstance(scheme, str) and _SCHEME_RE.match(scheme) else "<invalid>"
    return f"v{version} {scheme}"


def _shown(labels) -> str:
    """Sanitised labels as a short list for a message, or '' when there are none."""
    unique = sorted(set(labels))
    if not unique:
        return ""
    head = ", ".join(unique[:_SHOWN_MAX])
    more = len(unique) - _SHOWN_MAX
    return f"[{head}]" if more <= 0 else f"[{head}, and {more} more]"
