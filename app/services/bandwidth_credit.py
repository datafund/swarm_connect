# app/services/bandwidth_credit.py
"""
Bandwidth Credit Ledger for prepaid, byte-denominated upload credit.

The chunk-forwarding feature (Flow A) bills bandwidth, not storage. Because x402
is per-request, pricing each tiny chunk individually would collapse onto the
minimum-price floor every time. Instead a client makes ONE x402 payment to top up
a byte-denominated credit balance keyed to its address; each chunk upload debits
bytes from that balance.

This module is the accounting primitive only — pricing, the top-up endpoint, and
debit-on-upload wiring live in the x402/endpoint layers (see issue #221).

State is persisted to a JSON file (atomic writes) for durability across restarts.
Modeled on app/services/stamp_ownership.py.
"""
import json
import logging
import secrets
from datetime import datetime, timezone
from threading import Lock
from typing import Dict, Optional, Tuple

from app.core.atomic_io import atomic_write_json
from app.core.config import settings

logger = logging.getLogger(__name__)


class BandwidthCreditManager:
    """
    Tracks prepaid bandwidth credit balances (in bytes) per client address.

    Thread-safe: all balance mutations happen under a single lock so that a
    check-and-debit is atomic and concurrent debits can never drive a balance
    negative or double-spend.
    """

    def __init__(self, state_file: Optional[str] = None):
        # address -> {balance_bytes, updated_at, total_topped_up_bytes, token}
        self._balances: Dict[str, dict] = {}
        # token -> address (rebuilt from balances on load; the token also lives
        # inside each balance entry so the existing persistence format is unchanged)
        self._token_index: Dict[str, str] = {}
        self._lock = Lock()
        self._state_file = state_file

    def _get_state_file_path(self) -> str:
        """Get the state file path, using override or settings."""
        return self._state_file or settings.BANDWIDTH_CREDIT_STATE_FILE

    @staticmethod
    def _normalize(address: str) -> str:
        """Normalize an address for use as a ledger key (lowercase)."""
        return address.lower() if address else address

    def _save_state(self):
        """Persist the ledger to the state file."""
        state_file = self._get_state_file_path()
        try:
            atomic_write_json(state_file, self._balances)
            logger.debug(f"Saved bandwidth credit ledger: {len(self._balances)} accounts to {state_file}")
        except Exception as e:
            logger.error(f"Failed to save bandwidth credit ledger to {state_file}: {e}")

    def _load_state(self):
        """Load the ledger from the state file."""
        state_file = self._get_state_file_path()
        try:
            with open(state_file, 'r') as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._balances = data
                logger.info(f"Loaded bandwidth credit ledger: {len(self._balances)} accounts from {state_file}")
            else:
                logger.warning(f"Invalid bandwidth credit ledger format in {state_file}, starting fresh")
                self._balances = {}
        except FileNotFoundError:
            logger.info(f"No bandwidth credit ledger at {state_file}, starting fresh")
            self._balances = {}
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"Corrupt bandwidth credit ledger {state_file}: {e}, starting fresh")
            self._balances = {}
        except Exception as e:
            logger.warning(f"Error loading bandwidth credit ledger from {state_file}: {e}, starting fresh")
            self._balances = {}

        # Rebuild the token index from the loaded balances
        self._token_index = {
            entry["token"]: addr
            for addr, entry in self._balances.items()
            if entry.get("token")
        }

    def credit(self, address: str, bytes_amount: int) -> int:
        """
        Add prepaid credit to an address's balance.

        Args:
            address: Client address (e.g. the x402 payer / owner).
            bytes_amount: Positive number of bytes to add.

        Returns:
            The new balance in bytes.

        Raises:
            ValueError: If bytes_amount is not positive or address is empty.
        """
        if not address:
            raise ValueError("address is required")
        if bytes_amount <= 0:
            raise ValueError(f"credit amount must be positive, got {bytes_amount}")

        key = self._normalize(address)
        with self._lock:
            entry = self._balances.get(key) or {
                "balance_bytes": 0,
                "total_topped_up_bytes": 0,
                "updated_at": None,
            }
            entry["balance_bytes"] = int(entry.get("balance_bytes", 0)) + int(bytes_amount)
            entry["total_topped_up_bytes"] = int(entry.get("total_topped_up_bytes", 0)) + int(bytes_amount)
            entry["updated_at"] = datetime.now(timezone.utc).isoformat()
            self._balances[key] = entry
            new_balance = entry["balance_bytes"]
            logger.info(f"Credited {bytes_amount} bytes to {key[:10]}..., new balance {new_balance} bytes")
            self._save_state()
            return new_balance

    def debit(self, address: str, bytes_amount: int) -> Tuple[bool, int]:
        """
        Atomically debit bytes from an address's balance if sufficient.

        Args:
            address: Client address.
            bytes_amount: Positive number of bytes to debit.

        Returns:
            Tuple of (success, remaining_balance). On insufficient credit,
            returns (False, current_balance) and the balance is left unchanged.

        Raises:
            ValueError: If bytes_amount is not positive or address is empty.
        """
        if not address:
            raise ValueError("address is required")
        if bytes_amount <= 0:
            raise ValueError(f"debit amount must be positive, got {bytes_amount}")

        key = self._normalize(address)
        with self._lock:
            entry = self._balances.get(key)
            current = int(entry.get("balance_bytes", 0)) if entry else 0
            if current < bytes_amount:
                return False, current
            entry["balance_bytes"] = current - int(bytes_amount)
            entry["updated_at"] = datetime.now(timezone.utc).isoformat()
            remaining = entry["balance_bytes"]
            self._save_state()
            return True, remaining

    def balance(self, address: str) -> int:
        """Return the current balance in bytes for an address (0 if unknown)."""
        if not address:
            return 0
        key = self._normalize(address)
        with self._lock:
            entry = self._balances.get(key)
            return int(entry.get("balance_bytes", 0)) if entry else 0

    def get_info(self, address: str) -> Optional[dict]:
        """Return the full ledger entry for an address (for status endpoints)."""
        if not address:
            return None
        key = self._normalize(address)
        with self._lock:
            entry = self._balances.get(key)
            return dict(entry) if entry else None

    def account_count(self) -> int:
        """Number of accounts with a non-zero balance (for metrics)."""
        with self._lock:
            return sum(1 for e in self._balances.values() if int(e.get("balance_bytes", 0)) > 0)

    def total_outstanding_bytes(self) -> int:
        """Sum of all unspent credit balances in bytes (for metrics)."""
        with self._lock:
            return sum(int(e.get("balance_bytes", 0)) for e in self._balances.values())

    def issue_token(self, address: str) -> str:
        """
        Return a bearer credit token bound to an address, creating one if needed.

        The token is the credit account's API key: it is established at x402 top-up
        time (the address is the verified x402 payer) and presented on subsequent
        chunk uploads to spend the prepaid balance. Idempotent — repeated calls for
        the same address return the same token.

        Args:
            address: Client address (the x402 payer that funded the credit).

        Returns:
            The bearer token string.

        Raises:
            ValueError: If address is empty.
        """
        if not address:
            raise ValueError("address is required")
        key = self._normalize(address)
        with self._lock:
            entry = self._balances.get(key)
            if entry is None:
                entry = {
                    "balance_bytes": 0,
                    "total_topped_up_bytes": 0,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                self._balances[key] = entry
            existing = entry.get("token")
            if existing:
                return existing
            token = secrets.token_urlsafe(32)
            entry["token"] = token
            entry["updated_at"] = datetime.now(timezone.utc).isoformat()
            self._token_index[token] = key
            self._save_state()
            return token

    def resolve_token(self, token: str) -> Optional[str]:
        """Resolve a bearer credit token to its address, or None if unknown."""
        if not token:
            return None
        with self._lock:
            return self._token_index.get(token)

    def load_on_startup(self):
        """Load ledger state on application startup."""
        self._load_state()


# Global singleton instance
bandwidth_credit_manager = BandwidthCreditManager()
