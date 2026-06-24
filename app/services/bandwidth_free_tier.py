# app/services/bandwidth_free_tier.py
"""
Free-tier daily byte quota for chunk uploads.

Independent of the x402 (stamp/data) free tier. Tracks bytes uploaded per client
IP per UTC day and refuses uploads once the configured daily quota is exhausted.

State is in-memory only: the quota resets daily anyway, and a process restart
resetting the counters is acceptably lenient for an alpha free tier (it never
over-charges a paying user — paid uploads use the persisted credit ledger).
"""
import logging
from datetime import datetime, timezone
from threading import Lock
from typing import Dict, Tuple

logger = logging.getLogger(__name__)


class BandwidthFreeTierTracker:
    """Per-IP, per-UTC-day byte quota tracker for free chunk uploads."""

    def __init__(self):
        # ip -> {"date": "YYYY-MM-DD", "bytes_used": int}
        self._usage: Dict[str, dict] = {}
        self._lock = Lock()

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def try_consume(self, ip: str, bytes_amount: int, daily_limit_bytes: int) -> Tuple[bool, int]:
        """
        Attempt to consume quota for an upload.

        Args:
            ip: Client IP (the free-tier identity).
            bytes_amount: Bytes the upload would consume.
            daily_limit_bytes: The per-day quota in bytes.

        Returns:
            (allowed, remaining_bytes). On refusal, usage is unchanged and
            remaining_bytes is what is left for the day.
        """
        key = ip or "unknown"
        today = self._today()
        with self._lock:
            entry = self._usage.get(key)
            if not entry or entry.get("date") != today:
                entry = {"date": today, "bytes_used": 0}
                self._usage[key] = entry

            used = int(entry["bytes_used"])
            if used + bytes_amount > daily_limit_bytes:
                return False, max(0, daily_limit_bytes - used)

            entry["bytes_used"] = used + bytes_amount
            return True, daily_limit_bytes - entry["bytes_used"]

    def refund(self, ip: str, bytes_amount: int) -> None:
        """Return quota to an IP (used when a counted upload later fails)."""
        key = ip or "unknown"
        with self._lock:
            entry = self._usage.get(key)
            if entry and entry.get("date") == self._today():
                entry["bytes_used"] = max(0, int(entry["bytes_used"]) - bytes_amount)

    def reset(self) -> None:
        """Clear all usage (used by tests)."""
        with self._lock:
            self._usage.clear()


# Global singleton instance
free_tier_tracker = BandwidthFreeTierTracker()
