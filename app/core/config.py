# app/core/config.py
import os
from typing import Optional, List
from pydantic_settings import BaseSettings
from pydantic import AnyHttpUrl, field_validator
from functools import lru_cache
from dotenv import load_dotenv

# Load .env file if it exists
load_dotenv()


class Settings(BaseSettings):
    PROJECT_NAME: str = "Swarm API Aggregator"
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
    X402_RATE_LIMIT_PER_IP: int = 10  # Requests per minute per IP

    # === x402 Access Control ===
    X402_BLACKLIST_IPS: str = ""  # Comma-separated blocked IPs
    X402_WHITELIST_IPS: str = ""  # Comma-separated free-access IPs

    # === x402 Audit Settings ===
    X402_AUDIT_LOG_PATH: str = "logs/x402_audit.jsonl"

    # === Base Chain Settings (for monitoring USDC receipts) ===
    BASE_RPC_URL: str = "https://sepolia.base.org"

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

    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "ignore"  # Ignore extra fields from .env


@lru_cache()  # Cache the settings object for performance
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
