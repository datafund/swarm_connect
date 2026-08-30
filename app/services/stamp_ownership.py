# app/services/stamp_ownership.py
"""
Stamp Ownership Manager for tracking and enforcing stamp access.

When x402 is enabled, stamps acquired via paid requests are exclusive
to the payer's wallet address. Free tier stamps are shared/communal.
Pre-existing stamps (not in the registry) remain accessible for
backward compatibility.
"""
import json
import logging
from datetime import datetime, timezone
from threading import Lock
from typing import Dict, Optional, Set, Tuple

from app.core.atomic_io import atomic_write_json
from app.core.config import settings

logger = logging.getLogger(__name__)


# Owner value for batches the gateway bought for its own pool and has not handed
# out. Not a wallet address, and deliberately not a valid one, so it can never
# collide with a real owner.
POOL_OWNER = "pool"


class StampOwnershipManager:
    """
    Tracks stamp ownership and enforces access control.

    State is persisted to a JSON file for durability across restarts.
    """

    def __init__(self, state_file: Optional[str] = None):
        self._registry: Dict[str, dict] = {}  # batch_id -> {owner, mode, acquired_at, source}
        self._lock = Lock()
        self._state_file = state_file

    def _get_state_file_path(self) -> str:
        """Get the state file path, using override or settings."""
        return self._state_file or settings.STAMP_OWNERSHIP_FILE

    def _save_state(self):
        """Persist ownership registry to state file."""
        state_file = self._get_state_file_path()
        try:
            atomic_write_json(state_file, self._registry)
            logger.debug(f"Saved ownership state: {len(self._registry)} stamps to {state_file}")
        except Exception as e:
            logger.error(f"Failed to save ownership state to {state_file}: {e}")

    def _load_state(self):
        """Load ownership registry from state file."""
        state_file = self._get_state_file_path()
        try:
            with open(state_file, 'r') as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._registry = data
                logger.info(f"Loaded ownership state: {len(self._registry)} stamps from {state_file}")
            else:
                logger.warning(f"Invalid ownership state format in {state_file}, starting fresh")
                self._registry = {}
        except FileNotFoundError:
            logger.info(f"No ownership state file at {state_file}, starting fresh")
            self._registry = {}
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"Corrupt ownership state file {state_file}: {e}, starting fresh")
            self._registry = {}
        except Exception as e:
            logger.warning(f"Error loading ownership state from {state_file}: {e}, starting fresh")
            self._registry = {}

    def register_stamp(
        self,
        batch_id: str,
        owner: str,
        mode: str,
        source: str
    ):
        """
        Register stamp ownership.

        Args:
            batch_id: The stamp batch ID
            owner: Wallet address (e.g. "0xABC...") or "shared" for communal stamps
            mode: "paid" or "free"
            source: How the stamp was acquired (e.g. "pool_acquire", "direct_purchase")
        """
        with self._lock:
            self._registry[batch_id] = {
                "owner": owner,
                "mode": mode,
                "acquired_at": datetime.now(timezone.utc).isoformat(),
                "source": source
            }
            logger.info(f"Registered stamp {batch_id[:16]}... owner={owner[:16] if owner != 'shared' else 'shared'}, mode={mode}, source={source}")
            self._save_state()

    def check_access(
        self,
        batch_id: str,
        wallet_address: Optional[str],
        mode: Optional[str]
    ) -> Tuple[bool, str]:
        """
        Check if a wallet/mode has access to use a stamp.

        Args:
            batch_id: The stamp batch ID
            wallet_address: The requester's wallet address (None for unauthenticated)
            mode: The requester's x402 mode ("paid", "free-tier", or None)

        Returns:
            Tuple of (allowed: bool, reason: str)
        """
        # If x402 is disabled, skip enforcement
        if not settings.X402_ENABLED:
            return True, "x402 disabled, no ownership enforcement"

        with self._lock:
            entry = self._registry.get(batch_id)

        # Stamp not in registry.
        #
        # This used to return allowed, for backward compatibility with batches
        # predating the registry. The set that default actually covered was not
        # legacy callers: every path by which a caller obtains a batch —
        # /pool/acquire, POST /stamps/, /stamps/for-owner — registers it. What it
        # covered was the POOL'S OWN INVENTORY, bought and funded by the gateway
        # and not yet handed to anyone, which is precisely what no caller should
        # be writing to. A production pool batch reached 50% utilisation without
        # ever being acquired (#312).
        #
        # Pool inventory is now registered at purchase time as POOL_OWNER, so the
        # untracked set is empty in normal operation and denying it costs nothing.
        #
        # STAMP_OWNERSHIP_ALLOW_UNTRACKED restores the old behaviour. It exists
        # for one specific situation: STAMP_OWNERSHIP_FILE is lost or reset, every
        # batch becomes untracked at once, and legitimate owners would otherwise
        # be locked out of batches they paid for. Failing closed is right by
        # default; this is the deliberate escape hatch, not a shrug.
        if entry is None:
            if settings.STAMP_OWNERSHIP_ALLOW_UNTRACKED:
                return True, "stamp not tracked, permissive mode enabled"
            return False, "stamp is not registered to any owner"

        # Gateway-owned pool inventory. Nobody may write to it directly — a
        # caller receives one by acquiring it, which re-registers it to them.
        if entry["owner"] == POOL_OWNER:
            return False, "stamp belongs to the gateway pool; acquire it first"

        # Shared stamps -> always allowed
        if entry["owner"] == "shared":
            return True, "shared stamp, open access"

        # Owner matches wallet -> allowed
        if wallet_address and entry["owner"] == wallet_address:
            return True, "owner match"

        # Otherwise -> denied
        return False, f"stamp owned by {entry['owner'][:10]}..., not accessible to {'wallet ' + wallet_address[:10] + '...' if wallet_address else 'unauthenticated user'}"

    def remove_stamp(self, batch_id: str):
        """Remove a stamp from the ownership registry."""
        with self._lock:
            if batch_id in self._registry:
                del self._registry[batch_id]
                logger.debug(f"Removed stamp {batch_id[:16]}... from ownership registry")
                self._save_state()

    def cleanup_expired(self, valid_batch_ids: Set[str]):
        """
        Remove stamps from registry that are no longer valid.

        Args:
            valid_batch_ids: Set of batch IDs that are still valid on the Bee node
        """
        with self._lock:
            to_remove = [
                bid for bid in self._registry
                if bid not in valid_batch_ids
            ]
            if to_remove:
                for bid in to_remove:
                    del self._registry[bid]
                logger.info(f"Cleaned {len(to_remove)} expired stamps from ownership registry")
                self._save_state()

    def get_stamp_info(self, batch_id: str) -> Optional[dict]:
        """Get ownership info for a stamp (for debugging/status endpoints)."""
        with self._lock:
            return self._registry.get(batch_id)

    def load_on_startup(self):
        """Load ownership state on application startup."""
        self._load_state()


# Global singleton instance
stamp_ownership_manager = StampOwnershipManager()
