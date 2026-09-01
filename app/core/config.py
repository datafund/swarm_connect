# app/core/config.py
import os
from typing import Optional, List
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import AnyHttpUrl, field_validator
from functools import lru_cache
from dotenv import load_dotenv

# Load .env file if it exists
load_dotenv()


class Settings(BaseSettings):
    PROJECT_NAME: str = "Provenance Gateway"
    API_V1_STR: str = "/api/v1"
    SWARM_BEE_API_URL: AnyHttpUrl  # validates that it's a URL

    # === x402 Core Settings ===
    X402_ENABLED: bool = False  # Master switch - gateway works as today when false
    X402_FACILITATOR_URL: str = "https://x402.org/facilitator"  # Testnet facilitator
    X402_PAY_TO_ADDRESS: Optional[str] = None  # Wallet address for USDC receipts (Base)
    X402_NETWORK: str = "base-sepolia"  # Network identifier (v1 style)

    # === x402 Pricing Settings ===
    X402_BZZ_USD_RATE: float = 0.50  # Manual BZZ/USD rate (1 BZZ = $0.50)
    X402_MARKUP_PERCENT: float = 50.0  # Markup percentage
    X402_MIN_PRICE_USD: float = 0.01  # Minimum charge per request

    # === x402 Threshold Settings (Gnosis wallet - warnings) ===
    X402_XBZZ_WARN_THRESHOLD: float = 10.0  # Warn if xBZZ < threshold
    X402_XDAI_WARN_THRESHOLD: float = 0.5  # Warn if xDAI < threshold
    X402_CHEQUEBOOK_WARN_THRESHOLD: float = 5.0  # Warn if chequebook < threshold

    # === x402 Limits ===
    X402_MAX_STAMP_BZZ: float = 5.0  # Max single stamp purchase in BZZ
    X402_RATE_LIMIT_PER_IP: int = 10  # Requests per minute per IP (for paying users)

    # === x402 Free Tier Settings ===
    # When enabled, users without x402 payment can still access with stricter rate limits
    X402_FREE_TIER_ENABLED: bool = True  # Allow non-paying users with rate limits
    X402_FREE_TIER_RATE_LIMIT: int = 3  # Requests per minute for free tier (lower than paid)

    # === x402 Access Control ===
    X402_BLACKLIST_IPS: str = ""  # Comma-separated blocked IPs
    X402_WHITELIST_IPS: str = ""  # Comma-separated free-access IPs

    # === x402 Audit Settings ===
    X402_AUDIT_LOG_PATH: str = "logs/x402_audit.jsonl"

    # === Base Chain Settings (for monitoring USDC receipts) ===
    BASE_RPC_URL: str = "https://sepolia.base.org"

    # === Base Sepolia Gateway Wallet Monitoring ===
    X402_BASE_ETH_WARN_THRESHOLD: float = 0.005  # Warn if ETH < threshold (~50 txs)
    X402_BASE_ETH_CRITICAL_THRESHOLD: float = 0.001  # Block if ETH < critical (~10 txs)

    # === Stamp Pool Settings ===
    # Stamp pool maintains pre-purchased stamps for low-latency provisioning.
    # When enabled, clients can request stamps immediately without waiting for
    # blockchain confirmation (~1 minute).
    STAMP_POOL_ENABLED: bool = False  # Master switch for stamp pool feature

    # Reserve configuration by depth level (JSON string or dict)
    # Format: {"depth": count} - depth 17=small, 20=medium, 22=large
    # Default: 1 small (depth 17), 1 medium (depth 20), 0 large (depth 22)
    STAMP_POOL_RESERVE_SMALL: int = 1   # Number of depth-17 stamps to keep in reserve
    STAMP_POOL_RESERVE_MEDIUM: int = 1  # Number of depth-20 stamps to keep in reserve
    STAMP_POOL_RESERVE_LARGE: int = 0   # Number of depth-22 stamps to keep in reserve

    # Pool monitoring settings
    STAMP_POOL_CHECK_INTERVAL_SECONDS: int = 900  # How often to check pool (15 minutes)

    # Daily allowance of pooled batches per calling origin.
    # Format: "https://app.example=50,https://dev.app.example=20"
    #
    # The pool pre-buys batches and pays to keep them alive, and /pool/acquire had
    # no gate at all — 3,866 acquire calls in a day drove 40 replacement purchases
    # on staging. The main consumer is a static browser app with no backend and no
    # identity of its own, so there is no address to allow-list and no key it could
    # sign with; an origin with a budget is the control that fits.
    #
    # Origin is attribution, not authentication. A browser will not let one site
    # forge another's, so this does stop other WEBSITES spending your postage. Any
    # non-browser client can claim any origin, so the BUDGET is what protects you —
    # a forged origin consumes that origin's allowance and no more.
    POOL_DAILY_ALLOWANCES: str = ""
    # Allowance for origins not listed above, including callers that send no
    # Origin at all: CLIs, SDKs, server-to-server.
    #
    # -1 means unlimited, which is the behaviour before this existed, and is the
    # default so that deploying changes nothing until allowances are deliberately
    # configured. A limit that arrives unannounced breaks callers.
    POOL_DEFAULT_DAILY_ALLOWANCE: int = -1
    POOL_ALLOWANCE_STATE_FILE: str = "data/pool_allowance.json"
    STAMP_POOL_MIN_TTL_HOURS: int = 24  # Top up if TTL below this
    STAMP_POOL_TOPUP_HOURS: int = 168   # How much TTL to add (1 week)
    STAMP_POOL_LOW_RESERVE_THRESHOLD: int = 1  # Alert when reserve drops to this level

    # Stamp duration for new pool stamps (in hours)
    STAMP_POOL_DEFAULT_DURATION_HOURS: int = 168  # 1 week default for pool stamps

    # Immediate replenishment: when true, purchasing a replacement stamp starts
    # immediately (async) when a stamp is released from the pool
    STAMP_POOL_IMMEDIATE_REPLENISH: bool = True

    # State persistence: file path for persisting pool state across restarts
    STAMP_POOL_STATE_FILE: str = "data/pool_state.json"

    # Stamp ownership: file path for persisting stamp ownership records
    STAMP_OWNERSHIP_FILE: str = "data/stamp_owners.json"
    # When a batch is absent from the ownership registry, may any caller use it?
    #
    # False (the default) fails closed. True restores the pre-#312 behaviour and
    # exists for one situation: the registry file is lost, every batch becomes
    # untracked at once, and legitimate owners would be locked out of batches
    # they paid for. Turn it on to recover, re-register, and turn it off again.
    STAMP_OWNERSHIP_ALLOW_UNTRACKED: bool = False

    # === Debug Proxy (read-only Bee diagnostics, signature-gated) ===
    # Comma-separated 0x addresses allowed to read Bee diagnostics via
    # GET /api/v1/debug/bee/{path}. Access requires an EIP-191 signature from one
    # of these addresses over "swarm-connect-debug:<unix_ts>" (headers
    # X-Debug-Address optional, X-Debug-Timestamp, X-Debug-Signature). Empty =>
    # endpoint disabled (404). Addresses are public identifiers, not secrets.
    DEBUG_ALLOWED_ADDRESSES: str = ""
    DEBUG_SIG_MAX_AGE_SECONDS: int = 300  # signature freshness window (replay guard)

    # Addresses permitted to trigger POST /api/v1/pool/check, which SPENDS BZZ
    # by purchasing postage batches. Deliberately a separate list from
    # DEBUG_ALLOWED_ADDRESSES: reading the node's diagnostics and spending the
    # gateway's money are different privileges, and an operator trusted with the
    # former is not automatically trusted with the latter.
    # Empty => the endpoint answers 404. That is the safe default, and it closes
    # the exposure on any deployment that has not configured it.
    POOL_ADMIN_ADDRESSES: str = ""

    # === Flow B: Gnosis chain client (buy postage batches for an external owner) ===
    # Signs/sends approve + createBatch on Gnosis so a batch can be owned by an
    # arbitrary address. The signing key is SENSITIVE — handle like NOTARY_PRIVATE_KEY
    # (env/secret, never logged). Drives POST /api/v1/stamps/for-owner (#228).
    GNOSIS_RPC_URL: Optional[str] = None
    GNOSIS_PRIVATE_KEY: Optional[str] = None  # funded Gnosis wallet (xBZZ + xDAI), pays for batches
    GNOSIS_CHAIN_ID: int = 100  # 100 = Gnosis mainnet, 11155111 = Sepolia testnet
    # Contract/token addresses default per chain id when unset (see gnosis_chain.py).
    POSTAGE_STAMP_CONTRACT_ADDRESS: Optional[str] = None
    BZZ_TOKEN_ADDRESS: Optional[str] = None

    # === Flow B: buy postage batch for an external owner — endpoint (#228) + guards (#230) ===
    # POST /api/v1/stamps/for-owner. SPENDS the gateway's Gnosis funds, so it is OFF by
    # default and gated by an owner allow-list + hard alpha caps.
    STAMP_PURCHASE_FOR_OTHERS_ENABLED: bool = False  # master toggle (router 404s when off)
    STAMP_FOR_OTHERS_REQUIRE_WHITELIST: bool = True   # require owner in the allow-list
    STAMP_FOR_OTHERS_OWNER_WHITELIST: str = ""        # comma-separated 0x owner addresses allowed
    STAMP_FOR_OTHERS_MAX_DEPTH: int = 22              # cap batch depth (capacity)
    STAMP_FOR_OTHERS_MAX_BZZ: float = 1.0             # cap BZZ spent per batch
    STAMP_FOR_OTHERS_MAX_DURATION_HOURS: int = 168    # cap TTL (1 week)
    STAMP_FOR_OTHERS_FREE_TIER_ENABLED: bool = False  # free creation OFF by default (real BZZ spend)
    # Signer (GNOSIS_PRIVATE_KEY) wallet thresholds for preflight + metrics (#231).
    GNOSIS_XDAI_CRITICAL_THRESHOLD: float = 0.005     # block (503) if xDAI below this (no gas)
    GNOSIS_XDAI_WARN_THRESHOLD: float = 0.05
    GNOSIS_XBZZ_WARN_THRESHOLD: float = 0.5           # warn if xBZZ (BZZ units) below this

    # === Notary/Provenance Signing Settings ===
    # The notary feature allows the gateway to sign documents with an authoritative timestamp.
    # This provides proof that a document existed at a specific time, signed by the gateway.
    NOTARY_ENABLED: bool = False  # Master switch for notary signing feature
    NOTARY_PRIVATE_KEY: Optional[str] = None  # Hex-encoded private key for signing (without 0x prefix)

    # === Stamp Propagation Timing ===
    STAMP_PROPAGATION_SECONDS: int = 120  # Expected propagation delay after purchase (~2 minutes)

    # === Upload Limits ===
    MAX_UPLOAD_SIZE_MB: int = 10  # Maximum file upload size in megabytes

    # === Chunk Upload (stamped-chunk forwarding, Flow A) ===
    # When enabled, the gateway forwards a single client-supplied PRE-STAMPED chunk
    # to the Bee node's POST /chunks endpoint using the Swarm-Postage-Stamp header.
    # The client controls the postage stamp; the gateway is a thin forwarder and does
    # NOT verify the stamp signature/owner (Bee does that).
    CHUNK_UPLOAD_ENABLED: bool = False  # Master switch for chunk forwarding feature
    # A single Swarm chunk is at most an 8-byte span prefix + 4096 bytes of payload.
    CHUNK_UPLOAD_MAX_BYTES_PER_REQUEST: int = 4104  # Hard cap on the chunk body size
    # Free tier for chunk uploads: a per-IP daily byte quota, independent of the
    # x402 (stamp/data) free tier. Opt in per request with header X-Payment-Mode: free.
    CHUNK_UPLOAD_FREE_TIER_ENABLED: bool = True  # Allow free chunk uploads within a daily quota
    CHUNK_UPLOAD_FREE_TIER_MB_PER_DAY: int = 100  # Free bytes per IP per UTC day (1 MB = 10^6 bytes)

    # === Bandwidth Credit Ledger ===
    # Prepaid, byte-denominated credit balances keyed by client address. One x402
    # payment tops up a balance; each chunk upload debits bytes from it. This avoids
    # the per-request minimum-price floor collapsing onto every tiny chunk.
    BANDWIDTH_CREDIT_STATE_FILE: str = "data/bandwidth_credit.json"  # Ledger persistence path
    # Bandwidth price used to convert an x402 top-up payment into byte credit.
    X402_BANDWIDTH_USD_PER_GB: float = 0.10  # USD per GB of upload bandwidth (1 GB = 10^9 bytes)
    # Minimum top-up so a single x402 payment clears the per-request price floor.
    BANDWIDTH_CREDIT_MIN_TOPUP_MB: int = 100  # Minimum credit top-up in MB (1 MB = 10^6 bytes)
    # Upper bound on a single top-up. The amount is priced before it is credited,
    # so economics already discourage an absurd request — but that guarantee
    # depends on the pricing and crediting paths agreeing about the number, which
    # is exactly what could not be assumed before they were made to share one
    # parser. A ceiling makes any future divergence bounded rather than unbounded.
    # 1 TB, far above any legitimate top-up.
    BANDWIDTH_CREDIT_MAX_TOPUP_MB: int = 1_000_000

    # === JSON Body Limits ===
    MAX_JSON_BODY_BYTES: int = 1_048_576  # Maximum JSON body size (1 MB)
    MAX_JSON_DEPTH: int = 20  # Maximum JSON nesting depth

    # === Global Rate Limiting ===
    RATE_LIMIT_ENABLED: bool = True  # Enable global rate limiting
    RATE_LIMIT_PER_MINUTE: int = 60  # Requests per minute per IP
    RATE_LIMIT_BURST: int = 10  # Extra burst capacity above per-minute limit

    # === Metrics/Monitoring ===
    METRICS_ENABLED: bool = True  # Expose /metrics endpoint for Prometheus
    METRICS_BALANCE_POLL_SECONDS: int = 60  # How often to poll wallet balances for metrics
    GATEWAY_ENVIRONMENT: str = "development"  # Environment label (development/staging/production)

    # === CORS Settings ===
    # Enable CORS for browser-based SDK usage (e.g., React/Vite frontends)
    CORS_ALLOWED_ORIGINS: str = "*"  # Comma-separated origins or "*" for all
    CORS_ALLOW_CREDENTIALS: bool = False  # Must be False when using "*" origins

    def get_cors_origins(self) -> List[str]:
        """Parse CORS allowed origins from comma-separated string.

        Returns ["*"] for wildcard, or list of specific origins.
        """
        if self.CORS_ALLOWED_ORIGINS == "*":
            return ["*"]
        return [origin.strip() for origin in self.CORS_ALLOWED_ORIGINS.split(",") if origin.strip()]

    @field_validator("X402_BLACKLIST_IPS", "X402_WHITELIST_IPS", mode="before")
    @classmethod
    def empty_str_to_empty(cls, v: str) -> str:
        """Ensure empty strings remain empty, not None."""
        return v if v else ""

    def get_blacklist_ips(self) -> List[str]:
        """Parse blacklist IPs from comma-separated string."""
        if not self.X402_BLACKLIST_IPS:
            return []
        return [ip.strip() for ip in self.X402_BLACKLIST_IPS.split(",") if ip.strip()]

    def get_whitelist_ips(self) -> List[str]:
        """Parse whitelist IPs from comma-separated string."""
        if not self.X402_WHITELIST_IPS:
            return []
        return [ip.strip() for ip in self.X402_WHITELIST_IPS.split(",") if ip.strip()]

    def get_stamp_for_others_whitelist(self) -> List[str]:
        """Parse the for-owner allow-list into lowercased 0x addresses."""
        if not self.STAMP_FOR_OTHERS_OWNER_WHITELIST:
            return []
        return [a.strip().lower() for a in self.STAMP_FOR_OTHERS_OWNER_WHITELIST.split(",") if a.strip()]

    def get_debug_allowed_addresses(self) -> List[str]:
        """Parse the debug allow-list into lowercased 0x addresses."""
        if not self.DEBUG_ALLOWED_ADDRESSES:
            return []
        return [a.strip().lower() for a in self.DEBUG_ALLOWED_ADDRESSES.split(",") if a.strip()]

    def get_pool_daily_allowances(self) -> dict:
        """Parse POOL_DAILY_ALLOWANCES into {normalised_origin: limit}."""
        out = {}
        if not self.POOL_DAILY_ALLOWANCES:
            return out
        from urllib.parse import urlparse
        for entry in self.POOL_DAILY_ALLOWANCES.split(","):
            entry = entry.strip()
            if not entry or "=" not in entry:
                continue
            origin, _, raw = entry.rpartition("=")
            try:
                limit = int(raw.strip())
            except ValueError:
                continue
            p = urlparse(origin.strip())
            if p.scheme and p.hostname:
                out[f"{p.scheme.lower()}://{p.hostname.lower()}"] = limit
        return out

    def get_pool_admin_addresses(self) -> List[str]:
        """Parse the pool-admin allow-list into lowercased 0x addresses."""
        if not self.POOL_ADMIN_ADDRESSES:
            return []
        return [a.strip().lower() for a in self.POOL_ADMIN_ADDRESSES.split(",") if a.strip()]

    def get_stamp_pool_reserve_config(self) -> dict:
        """Get stamp pool reserve configuration as {depth: count} dict.

        Only includes depths with count > 0.
        Maps: small=17, medium=20, large=22
        """
        config = {}
        if self.STAMP_POOL_RESERVE_SMALL > 0:
            config[17] = self.STAMP_POOL_RESERVE_SMALL
        if self.STAMP_POOL_RESERVE_MEDIUM > 0:
            config[20] = self.STAMP_POOL_RESERVE_MEDIUM
        if self.STAMP_POOL_RESERVE_LARGE > 0:
            config[22] = self.STAMP_POOL_RESERVE_LARGE
        return config

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        extra="ignore",  # Ignore extra fields from .env
    )


@lru_cache()  # Cache the settings object for performance
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
