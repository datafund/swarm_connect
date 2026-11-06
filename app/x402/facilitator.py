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
- X402_FACILITATOR_PAYAI_API_KEY_ID / _SECRET: a PayAI API key (#662). The
  secret is a base64 PKCS#8 Ed25519 private key; PayAIAuth signs a JWT with
  it that lives 120 s, caches it until 30 s before it expires, and sends it as
  Authorization: Bearer <jwt> on /verify, /settle and /supported, as
  @payai/facilitator does. It is not a static token: never put the secret in
  X402_FACILITATOR_BEARER_TOKEN. The configured X402_FACILITATOR_URL is used
  as is (https required). Without these settings PayAI serves the gateway on
  its shared free lane.
- X402_FACILITATOR_BEARER_TOKEN: a static bearer token, for facilitators that
  accept one.

At most one of the three may be set.

validate_x402_config() runs at startup and refuses a configuration that would
otherwise fail on the first payment, or take payment in the wrong asset (#370).
"""
import base64
import json
import logging
import math
import re
import threading
import time
import uuid
from typing import Callable, Optional

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


# --- PayAI API key (#662) -----------------------------------------------------
# Matches @payai/facilitator 2.4.6 (dist/cjs/index.js): normalizeApiKeySecret,
# generatePayAIApiKeyJwt, getOrGenerateJwt (REFRESH_BUFFER_SECONDS) and
# createPayAIAuthHeaders.
PAYAI_ISSUER = "payai-merchant"
PAYAI_TOKEN_LIFETIME_SECONDS = 120
PAYAI_REFRESH_MARGIN_SECONDS = 30
_PAYAI_SECRET_PREFIX = "payai_sk_"
# What String.prototype.trim() strips: WhiteSpace and LineTerminator
# (ECMA-262), that is tab, VT, FF, space, NBSP, U+FEFF, the Zs category, LF,
# CR, LS and PS. Python's str.strip() differs (no U+FEFF; adds U+001C-1F, U+0085).
_JS_TRIM = ("\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007"
            "\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff")
# What atob's forgiving-base64 decode removes first: ASCII whitespace.
_ASCII_WHITESPACE = re.compile(r"[\t\n\f\r ]")
_BASE64_ALPHABET = re.compile(r"[A-Za-z0-9+/]*")

# RFC 8410 / RFC 5958 OneAsymmetricKey (PKCS#8 v2) for Ed25519, the parts
# that are fixed: version 1, the algorithm 1.3.101.112 without parameters,
# and the 32-byte seed wrapped in an OCTET STRING inside the privateKey
# OCTET STRING.
_V2_VERSION = bytes.fromhex("020101")
_ED25519_ALGORITHM = bytes.fromhex("300506032b6570")
_V2_SEED_PREFIX = bytes.fromhex("04220420")


class _BadKey(Exception):
    """A secret that is not a usable key. Its text is a fixed reason only."""


def _atob(text: str) -> bytes:
    """atob's forgiving-base64 decode (WHATWG): ASCII whitespace removed, one
    or two trailing '=' removed when the length is a multiple of 4, then only
    the base64 alphabet, and never a length of 4n+1."""
    data = _ASCII_WHITESPACE.sub("", text)
    if len(data) % 4 == 0:
        data = data[:-2] if data.endswith("==") else data[:-1] if data.endswith("=") else data
    if len(data) % 4 == 1 or not _BASE64_ALPHABET.fullmatch(data):
        raise _BadKey("not base64")
    return base64.b64decode(data + "=" * (-len(data) % 4), validate=True)


def _der_length(der: bytes, i: int):
    """The DER length at der[i] (minimal form, at most two length bytes) and
    the index after it."""
    if i >= len(der):
        raise _BadKey("truncated")
    first = der[i]
    if first < 0x80:
        return first, i + 1
    count = first & 0x7F
    if count not in (1, 2) or i + 1 + count > len(der):
        raise _BadKey("bad length")
    n = int.from_bytes(der[i + 1:i + 1 + count], "big")
    if n < 0x80 or (count == 2 and n < 0x100):
        raise _BadKey("bad length")
    return n, i + 1 + count


def _ed25519_from_pkcs8_v2(der: bytes):
    """An Ed25519 key from a PKCS#8 v2 (OneAsymmetricKey) DER, which
    cryptography's load_der_private_key does not read but WebCrypto and
    OpenSSL do. Strict: the fields above, then optional attributes [0], then
    an optional public key [1], which must be the seed's own; nothing else."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    if not der or der[0] != 0x30:
        raise _BadKey("not PKCS#8")
    n, i = _der_length(der, 1)
    if i + n != len(der):
        raise _BadKey("bad length")
    fixed = _V2_VERSION + _ED25519_ALGORITHM + _V2_SEED_PREFIX
    if der[i:i + len(fixed)] != fixed or len(der) < i + len(fixed) + 32:
        raise _BadKey("not a PKCS#8 v2 Ed25519 key")
    i += len(fixed)
    key = Ed25519PrivateKey.from_private_bytes(der[i:i + 32])
    i += 32
    if i < len(der) and der[i] == 0xA0:              # attributes, not used
        n, i = _der_length(der, i + 1)
        i += n
    if i < len(der) and der[i] == 0x81:              # publicKey BIT STRING
        n, j = _der_length(der, i + 1)
        if n != 33 or j + n > len(der) or der[j] != 0:
            raise _BadKey("bad public key")
        own = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        if der[j + 1:j + 33] != own:
            raise _BadKey("its public key does not match its private key")
        i = j + n
    if i != len(der):
        raise _BadKey("unexpected data after the key")
    return key


def load_payai_private_key(secret: str):
    """The Ed25519 private key in a PayAI API key secret.

    Normalised as @payai/facilitator 2.4.6 does: normalizeApiKeySecret (trim()
    with JavaScript's whitespace set, then a leading payai_sk_ removed), then
    base64ToUint8Array (- and _ mapped to + and /, then atob's
    forgiving-base64 rules, see _atob). The DER is loaded by cryptography as
    PKCS#8; a PKCS#8 v2 Ed25519 DER (with the public key), which cryptography
    50 refuses, is read by _ed25519_from_pkcs8_v2. Where this may still
    differ from the package, which imports the DER with WebCrypto: DER that
    WebCrypto accepts and neither reader here does (unusual encodings), or the
    reverse. Any failure raises ValueError saying only that the setting is
    malformed: no part of the value, and no chained exception, since the
    message reaches the public deploy log (#485).
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import load_der_private_key
    problem = "not a base64 PKCS#8 Ed25519 private key"
    try:
        text = (secret or "").strip(_JS_TRIM)
        if text.startswith(_PAYAI_SECRET_PREFIX):
            text = text[len(_PAYAI_SECRET_PREFIX):]
        der = _atob(text.replace("-", "+").replace("_", "/"))
        try:
            key = load_der_private_key(der, password=None)
        except ValueError:
            key = _ed25519_from_pkcs8_v2(der)
        if isinstance(key, Ed25519PrivateKey):
            return key
        problem = "not an Ed25519 key"
    except _BadKey as e:
        problem = str(e)
    except Exception:
        pass
    raise ValueError(f"X402_FACILITATOR_PAYAI_API_KEY_SECRET is malformed ({problem}); it must be "
                     f"the PayAI API key secret, a base64 PKCS#8 Ed25519 private key")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _compact_json(value: dict) -> bytes:
    # JSON.stringify's output: no spaces, keys in insertion order, non-ASCII
    # characters as themselves (UTF-8 on the wire), not as \u escapes.
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class PayAIAuth:
    """PayAI API key JWTs, cached until PAYAI_REFRESH_MARGIN_SECONDS before expiry.

    One clock (seconds, floored as Math.floor(Date.now() / 1000)) sets iat,
    exp and the cache, as getOrGenerateJwt does. The check and the signing
    run under a lock with no await in between, so concurrent requests on the
    event loop (or on threads) share one token. Holds the loaded key, never
    the secret text; repr shows neither.
    """

    def __init__(self, key_id: str, private_key, clock: Callable[[], float] = time.time):
        self._key_id = key_id
        self._key = private_key
        self._clock = clock
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._expires_at = 0

    def __repr__(self) -> str:
        return "PayAIAuth(<redacted>)"

    __str__ = __repr__

    def _sign(self, now: int) -> str:
        header = {"alg": "EdDSA", "typ": "JWT", "kid": self._key_id}
        payload = {"sub": self._key_id, "iss": PAYAI_ISSUER, "iat": now,
                   "exp": now + PAYAI_TOKEN_LIFETIME_SECONDS, "jti": str(uuid.uuid4())}
        message = f"{_b64url(_compact_json(header))}.{_b64url(_compact_json(payload))}"
        return f"{message}.{_b64url(self._key.sign(message.encode('ascii')))}"

    def token(self) -> str:
        with self._lock:
            now = math.floor(self._clock())
            if self._token is None or self._expires_at <= now + PAYAI_REFRESH_MARGIN_SECONDS:
                self._token = self._sign(now)
                self._expires_at = now + PAYAI_TOKEN_LIFETIME_SECONDS
            return self._token

    async def create_headers(self) -> dict:
        auth = {"Authorization": f"Bearer {self.token()}"}
        # "list" like the bearer hook; "supported" for the startup check (#608).
        return {"verify": auth, "settle": auth, "supported": auth, "list": auth}


def _uses_payai() -> bool:
    return bool(settings.X402_FACILITATOR_PAYAI_API_KEY_ID or settings.X402_FACILITATOR_PAYAI_API_KEY_SECRET)


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
    if _uses_payai():
        # The configured URL as is: a PayAI token is not bound to a path.
        # validate_x402_config() has checked both values and https. The host
        # is not pinned to PayAI's: the token (valid 120 s, not the key) goes
        # to whichever facilitator is configured.
        auth = PayAIAuth(settings.X402_FACILITATOR_PAYAI_API_KEY_ID,
                         load_payai_private_key(settings.X402_FACILITATOR_PAYAI_API_KEY_SECRET))
        config["create_headers"] = auth.create_headers
        return config
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
    uses_payai = _uses_payai()
    mainnet = bool(network) and not is_testnet_network(network)
    if uses_payai and (uses_cdp or uses_bearer):
        problems.append("set only one of CDP credentials, PayAI credentials (X402_FACILITATOR_PAYAI_API_KEY_ID "
                        "/ _SECRET) and X402_FACILITATOR_BEARER_TOKEN")
    elif uses_cdp and uses_bearer:
        problems.append("set either CDP credentials or X402_FACILITATOR_BEARER_TOKEN, not both")
    if uses_payai:
        if not settings.X402_FACILITATOR_PAYAI_API_KEY_SECRET:
            problems.append("X402_FACILITATOR_PAYAI_API_KEY_SECRET is required with X402_FACILITATOR_PAYAI_API_KEY_ID")
        elif not settings.X402_FACILITATOR_PAYAI_API_KEY_ID:
            problems.append("X402_FACILITATOR_PAYAI_API_KEY_ID is required with X402_FACILITATOR_PAYAI_API_KEY_SECRET")
        else:
            try:
                load_payai_private_key(settings.X402_FACILITATOR_PAYAI_API_KEY_SECRET)
            except ValueError as e:
                problems.append(str(e))     # names the setting, never its value
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
        if (mainnet or uses_bearer or uses_payai) and url.startswith("http://"):
            problems.append("X402_FACILITATOR_URL must use https on a mainnet or with credentials")
    if problems:
        raise RuntimeError("Refusing to start with x402 enabled: " + "; ".join(problems))
    used = get_facilitator_client().config["url"] if uses_cdp else url
    logger.info(f"x402 configuration OK: network={network}, facilitator={used}"
                + (" (CDP auth)" if uses_cdp else " (PayAI API key auth)" if uses_payai
                   else " (bearer auth)" if settings.X402_FACILITATOR_BEARER_TOKEN else ""))


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
    2xx answer that cannot be read (also one over 1 MiB, or nested deeper
    than MAX_SUPPORTED_DEPTH, checked before parsing), is refused like one that does not list
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
    import asyncio
    import httpx2
    from app.x402.facilitator_client import (MAX_SUPPORTED_BYTES, SupportedAnswerEncoded,
                                             SupportedAnswerTooLarge)
    from app.x402.models_v1 import SupportedResponse
    unreadable = (f"the facilitator's /supported on {host} answered HTTP {{status}} with something "
                  f"that is not a list of payment kinds ({{why}})")
    try:
        # One deadline for the whole call: httpx2's timeout is per read, so a
        # facilitator dripping a byte per read could otherwise stall startup.
        response = await asyncio.wait_for(client.supported_response(), SUPPORTED_DEADLINE_SECONDS)
    except asyncio.TimeoutError:
        raise _v2_refusal(f"the facilitator's /supported on {host} could not be reached (timed out "
                          f"after {SUPPORTED_DEADLINE_SECONDS:g} s), so it is unknown whether it "
                          f"settles x402 v2 exact on {network}") from None
    except SupportedAnswerEncoded as e:
        raise _v2_refusal(unreadable.format(status=e.status, why="content-encoded; the gateway asks "
                                            "for identity and never inflates this answer")) from None
    except SupportedAnswerTooLarge as e:
        raise _v2_refusal(f"the facilitator's /supported on {host} answered HTTP {e.status} with "
                          f"something that is not a list of payment kinds (larger than "
                          f"{MAX_SUPPORTED_BYTES} bytes)") from None
    except httpx2.HTTPError as e:
        raise _v2_refusal(f"the facilitator's /supported on {host} could not be reached "
                          f"({type(e).__name__}), so it is unknown whether it settles x402 v2 exact "
                          f"on {network}") from None
    except Exception as e:
        # Anything else before an answer: the CDP or PayAI header hook raising, an
        # httpx2 error outside HTTPError (InvalidURL), a bad header value. A
        # refusal, not a traceback, and never the error text (it may hold a
        # credential or the URL).
        raise _v2_refusal(f"the request to the facilitator's /supported on {host} could not be made "
                          f"({type(e).__name__}; check the facilitator URL and credentials)") from None
    status = response.status_code
    if not 200 <= status < 300:
        # Not an answer to trust, whatever its body says (CDP answers 401 in
        # plain text without valid credentials).
        if 300 <= status < 400:
            hint = " (redirects are not followed: set X402_FACILITATOR_URL to the final URL)"
        elif status in (401, 403):
            hint = " (an authentication error: check the facilitator credentials)"
        else:
            hint = ""
        raise _v2_refusal(f"the facilitator's /supported on {host} answered HTTP {status}, not a list "
                          f"of payment kinds{hint}")
    # The body as JSON text, decoded here and nowhere else: httpx2's
    # Response.json() lets json.loads guess UTF-16 or UTF-32, and then the
    # byte-level depth count below would read another text than the parser.
    # RFC 8259 requires UTF-8 on the wire. A leading byte order mark is
    # stripped (the RFC lets a parser ignore it); anything that is not strict
    # UTF-8, or that holds a control character JSON never allows unescaped
    # (UTF-16 and UTF-32 bodies always do: NUL), is unreadable.
    raw = response.content
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _v2_refusal(unreadable.format(status=status, why="not UTF-8")) from None
    if _CONTROL_RE.search(text):
        raise _v2_refusal(unreadable.format(status=status, why="not UTF-8 JSON text: a control "
                                            "character, as in a UTF-16 or UTF-32 body")) from None
    if _nesting_depth(text) > MAX_SUPPORTED_DEPTH:
        # Checked before parsing, on the same text: CPython's JSON parser
        # recurses per level, and on Python 3.10 (the image's) a deep enough
        # body can crash the process instead of raising RecursionError.
        raise _v2_refusal(unreadable.format(status=status,
                                            why=f"nested deeper than {MAX_SUPPORTED_DEPTH} levels"))
    try:
        data = json.loads(text)
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


# Deepest nesting of objects and arrays read in a /supported answer (#608).
# A real answer is 4 deep (kinds[i].extra.x); codec_v2 allows 32 for headers.
MAX_SUPPORTED_DEPTH = 32


# The whole /supported call at startup, connect to last byte (#608).
SUPPORTED_DEADLINE_SECONDS = 15.0

# Characters JSON text never holds unescaped: control characters other than
# the whitespace tab, LF and CR (RFC 8259 sections 2 and 7).
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _nesting_depth(text: str) -> int:
    """The deepest nesting of [ and { in a JSON text, outside strings, without
    parsing it. Linear in the text, which is at most 1 MiB. It must be given
    the exact text json.loads parses."""
    depth = deepest = 0
    in_string = escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "[{":
            depth += 1
            if depth > deepest:
                deepest = depth
                if deepest > MAX_SUPPORTED_DEPTH:
                    return deepest
        elif ch in "]}":
            depth -= 1
    return deepest


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
