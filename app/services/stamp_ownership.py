# app/services/stamp_ownership.py
"""
Stamp Ownership Manager for tracking and enforcing stamp access.

When x402 is enabled, stamps acquired via paid requests are exclusive
to the payer's wallet address. Free tier stamps are shared/communal.
Batches not in the registry are denied unless STAMP_OWNERSHIP_ALLOW_UNTRACKED
is set (#312); the registry is loaded at startup (#349).
Batch ids are keyed in lowercase on every read and write (#558).
"""
import filecmp
import glob
import json
import logging
import re
import shutil
from datetime import datetime, timezone
from threading import Lock
from typing import Dict, Optional, Set, Tuple

from app.core.atomic_io import atomic_write_json, load_json_state, unreadable_state
from app.core.config import settings

logger = logging.getLogger(__name__)


# Owner value for batches the gateway bought for its own pool and has not handed
# out. Not a wallet address, and deliberately not a valid one, so it can never
# collide with a real owner.
POOL_OWNER = "pool"

# Owner values that are not wallet addresses.
_SPECIAL_OWNERS = ("shared", POOL_OWNER)

_ADDRESS_RE = re.compile(r"^(0x)?[0-9a-fA-F]{40}$")


def normalize_address(address: Optional[str]) -> Optional[str]:
    """Return the address lowercased and 0x-prefixed, or None if it is not one.

    Owners were stored and compared as exact strings (#384). The facilitator
    reports the payer in its own casing, for-owner stored `owner` as the caller
    typed it, and a signed owner proof recovers the checksummed form, so the
    same wallet could fail to match itself and be locked out of its own batch.
    One canonical form on the way in and on every comparison removes that.
    """
    if not isinstance(address, str) or not _ADDRESS_RE.match(address):
        return None
    address = address.lower()
    return address if address.startswith("0x") else "0x" + address


def _batch_key(batch_id: str) -> str:
    """Return the registry key for a batch id: the id in lowercase (#558).

    Bee lists batch ids in lowercase, but the API path and query patterns accept
    either case. Keying the registry by the lowercase id means a caller that
    forgets to lowercase cannot miss a record and wrongly deny its owner.
    """
    return batch_id.lower() if isinstance(batch_id, str) else batch_id


def _spellings(keys, full: bool = False) -> str:
    """Name the original spellings of one batch id for a log line or an error.

    With ``full`` (the refusal error, which an operator acts on) every id is
    printed exactly. Otherwise (the merge WARNING) 64-character ids are
    shortened to their first 12 and last 4 characters, as long as the shortened
    forms stay distinct; the exact keys are in the pre-migration copy.
    """
    keys = sorted(keys)
    short = keys if full else [k if len(k) <= 20 else f"{k[:12]}...{k[-4:]}" for k in keys]
    if len(set(short)) < len(keys):
        short = keys
    return ", ".join(repr(k) for k in short)


def _keep_pre_migration_copy(state_file: str) -> str:
    """Copy the state file aside before the load-time migration rewrites it.

    The migration drops the records of merged spellings, so their mode,
    acquired_at and source survive only in this copy. One copy per distinct
    content: a restart that migrates the same file again (because the save
    failed) reuses the existing copy instead of adding another. Raises on
    failure; the caller then does not save.
    """
    for existing in sorted(glob.glob(f"{glob.escape(state_file)}.pre-558-*")):
        if filecmp.cmp(state_file, existing, shallow=False):
            return existing
    backup = f"{state_file}.pre-558-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    shutil.copy2(state_file, backup)
    return backup


def _resolve_case_collision(spellings: Dict[str, dict], state_file: str) -> dict:
    """Pick the record to keep when one batch id is stored under several spellings.

    ``spellings`` maps each original key (all equal when lowercased) to its
    record. The decision depends only on the set of distinct owners, never on
    file order, and never grants access to a wallet that did not own the batch
    or silently drops a real owner:
    - one distinct owner: one record is enough;
    - exactly one wallet plus "shared" and/or "pool": the wallet wins. "shared"
      would open the batch to everyone and "pool" would let the gateway sell it
      again; the wallet is the only real owner, and owner -> pool/shared is
      never an expected transition (register_stamp logs it as an error);
    - anything else (two or more wallets, or "shared" and "pool" with no
      wallet): there is no safe guess, so the registry is refused as unreadable
      and the operator resolves it in the file.

    Among the records with the winning owner, the one already under the
    lowercase key is kept if there is one, else the first spelling in sorted
    order, so the metadata kept is also independent of file order.
    """
    owners = {entry["owner"] for entry in spellings.values()}
    wallets = owners - set(_SPECIAL_OWNERS)
    if len(owners) == 1:
        winner = next(iter(owners))
    elif len(wallets) == 1:
        winner = next(iter(wallets))
    else:
        reason = (
            f"one batch is registered under several spellings ({_spellings(spellings, full=True)}) "
            f"with different owners ({', '.join(repr(o) for o in sorted(owners))}); keep the "
            f"record of the real owner and delete the others")
        # Logged here as well as raised: if the startup traceback is lost, the
        # log still says why the gateway refused to start.
        logger.error(f"Refusing to load ownership registry {state_file}: {reason}")
        raise unreadable_state(state_file, reason)
    candidates = sorted(k for k, entry in spellings.items() if entry["owner"] == winner)
    key = _batch_key(candidates[0])
    return spellings[key] if key in candidates else spellings[candidates[0]]


class StampOwnershipManager:
    """
    Tracks stamp ownership and enforces access control.

    State is persisted to a JSON file for durability across restarts.
    """

    def __init__(self, state_file: Optional[str] = None):
        self._registry: Dict[str, dict] = {}  # lowercase batch_id -> {owner, mode, acquired_at, source}
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
        """Load ownership registry from state file.

        A missing file starts an empty registry. An unreadable one raises
        StateLoadError (see load_json_state): an empty registry denies every
        owner, and the next registration would overwrite their records.
        """
        state_file = self._get_state_file_path()
        data = load_json_state(state_file)
        if data is None:
            logger.info(f"No ownership state file at {state_file}, starting fresh")
            self._registry = {}
            return
        normalized = 0
        for batch_id, entry in data.items():
            if not isinstance(entry, dict) or not isinstance(entry.get("owner"), str):
                raise unreadable_state(state_file, f"entry {batch_id[:16]} has no owner")
            # Records written before owners were normalised (#384) may hold a
            # mixed-case address. Canonicalise them here, or those owners stay
            # locked out of batches registered before the fix.
            owner = entry["owner"]
            if owner in _SPECIAL_OWNERS:
                continue
            canonical = normalize_address(owner)
            if canonical is None:
                raise unreadable_state(state_file, f"entry {batch_id[:16]} has an invalid owner")
            if canonical != owner:
                entry["owner"] = canonical
                normalized += 1

        # Keys are lowercase batch ids (#558). A file written before that may
        # hold mixed-case keys; lowercase them once here. All spellings of one
        # id are grouped first and resolved together by _resolve_case_collision,
        # so the outcome does not depend on the order of keys in the file.
        groups: Dict[str, Dict[str, dict]] = {}
        for batch_id, entry in data.items():
            groups.setdefault(_batch_key(batch_id), {})[batch_id] = entry
        registry: Dict[str, dict] = {}
        lowered = sum(1 for batch_id in data if _batch_key(batch_id) != batch_id)
        merged = 0
        for key, spellings in groups.items():
            if len(spellings) == 1:
                registry[key] = next(iter(spellings.values()))
                continue
            kept = _resolve_case_collision(spellings, state_file)
            logger.warning(
                f"Ownership registry has one batch under {len(spellings)} spellings "
                f"({_spellings(spellings)}) with owners "
                f"{', '.join(repr(spellings[k]['owner']) for k in sorted(spellings))}; "
                f"keeping {kept['owner']!r}")
            registry[key] = kept
            merged += len(spellings) - 1

        self._registry = registry
        if normalized or lowered:
            logger.info(
                f"Migrated {state_file}: {lowered} batch key(s) lowercased, {merged} duplicate "
                f"spelling(s) merged, {normalized} owner address(es) normalised")
            # Persist so the file ends lowercase, after keeping the original
            # aside (merged spellings' records exist only there afterwards).
            # If the copy fails, do not save. _save_state writes atomically and
            # only logs on failure. Either way the original file is then left
            # intact, the registry in memory is complete, and the next load
            # migrates the same file again.
            try:
                backup = _keep_pre_migration_copy(state_file)
            except Exception as e:
                logger.error(f"Could not copy {state_file} before migrating it ({e}); "
                             f"not saving the migration, the file is left as it was")
            else:
                logger.info(f"Kept the pre-migration ownership registry at {backup}")
                self._save_state()
        logger.info(f"Loaded ownership state: {len(self._registry)} stamps from {state_file}")

    def register_stamp(
        self,
        batch_id: str,
        owner: str,
        mode: str,
        source: str,
        only_if_unowned: bool = False,
    ) -> bool:
        """
        Register stamp ownership.

        Args:
            batch_id: The stamp batch ID
            owner: Wallet address (e.g. "0xABC...") or "shared" for communal stamps
            mode: "paid" or "free"
            source: How the stamp was acquired (e.g. "pool_acquire", "direct_purchase")
            only_if_unowned: Refuse (return False) if the batch is registered already.

        Returns:
            False if refused, else True.

        Raises:
            ValueError: owner is neither a special owner nor a valid address.
        """
        if owner not in _SPECIAL_OWNERS:
            canonical = normalize_address(owner)
            if canonical is None:
                raise ValueError(f"invalid owner address: {owner!r}")
            owner = canonical
        batch_id = _batch_key(batch_id)
        with self._lock:
            existing = self._registry.get(batch_id)
            if existing is not None:
                if only_if_unowned:
                    logger.error(f"Refusing to register {batch_id[:16]} to {owner}: already registered "
                                 f"to {existing.get('owner')}")
                    return False
                if existing.get("owner") not in (POOL_OWNER, owner):
                    # A batch changing hands between callers is never expected.
                    logger.error(f"Re-registering {batch_id[:16]} from {existing.get('owner')} to {owner} "
                                 f"(source={source})")
            self._registry[batch_id] = {
                "owner": owner,
                "mode": mode,
                "acquired_at": datetime.now(timezone.utc).isoformat(),
                "source": source
            }
            logger.info(f"Registered stamp {batch_id[:16]}... owner={owner[:16] if owner != 'shared' else 'shared'}, mode={mode}, source={source}")
            self._save_state()
            return True

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

        batch_id = _batch_key(batch_id)
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

        # Owner matches wallet -> allowed. Both sides are canonical addresses, so
        # casing cannot make a wallet fail to match itself (#384).
        if wallet_address and entry["owner"] == normalize_address(wallet_address):
            return True, "owner match"

        # Otherwise -> denied
        return False, f"stamp owned by {entry['owner'][:10]}..., not accessible to {'wallet ' + wallet_address[:10] + '...' if wallet_address else 'unauthenticated user'}"

    def remove_stamp(self, batch_id: str):
        """Remove a stamp from the ownership registry."""
        batch_id = _batch_key(batch_id)
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
        valid_batch_ids = {_batch_key(bid) for bid in valid_batch_ids}
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
            return self._registry.get(_batch_key(batch_id))

    def load_on_startup(self):
        """Load ownership state on application startup."""
        self._load_state()


# Global singleton instance
stamp_ownership_manager = StampOwnershipManager()
