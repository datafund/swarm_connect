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

    # === Notary/Provenance Signing Settings ===
    # The notary feature allows the gateway to sign documents with an authoritative timestamp.
    # This provides proof that a document existed at a specific time, signed by the gateway.
    NOTARY_ENABLED: bool = False  # Master switch for notary signing feature
    NOTARY_PRIVATE_KEY: Optional[str] = None  # Hex-encoded private key for signing (without 0x prefix)

    # === Stamp Propagation Timing ===
    STAMP_PROPAGATION_SECONDS: int = 120  # Expected propagation delay after purchase (~2 minutes)

    # === Upload Limits ===
    MAX_UPLOAD_SIZE_MB: int = 10  # Maximum file upload size in megabytes

    # === JSON Body Limits ===
    MAX_JSON_BODY_BYTES: int = 1_048_576  # Maximum JSON body size (1 MB)
    MAX_JSON_DEPTH: int = 20  # Maximum JSON nesting depth

    # === Global Rate Limiting ===
    RATE_LIMIT_ENABLED: bool = True  # Enable global rate limiting
    RATE_LIMIT_PER_MINUTE: int = 60  # Requests per minute per IP
    RATE_LIMIT_BURST: int = 10  # Extra burst capacity above per-minute limit

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
