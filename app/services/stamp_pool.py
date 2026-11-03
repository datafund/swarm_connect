# app/services/stamp_pool.py
"""
Stamp Pool Manager for Low-Latency Stamp Provisioning.

This module maintains a reserve pool of pre-purchased postage stamps that can be
released immediately to clients without waiting for blockchain confirmation time
(which typically takes >1 minute).

Architecture:
- Pool tracks stamps by depth (17=small, 20=medium, 22=large)
- Background task monitors pool levels and replenishes when low
- Background task monitors TTL and tops up stamps approaching expiration
- Stamps are "released" to clients (removed from pool tracking)

See GitHub Issue #63 for full specification.
"""
import asyncio
import json
import os
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Dict, List, Optional, Set
from threading import Lock

from app.core.atomic_io import StateLoadError, atomic_write_json, keep_corrupt_copy, unreadable_state
from app.core.config import settings
from app.services import swarm_api
from app.services.bee_errors import SCRUB_CAP, error_cause, public_bee_text
from app.services.swarm_api import coerce_int
from app.services.stamp_ownership import POOL_OWNER, stamp_ownership_manager

logger = logging.getLogger(__name__)


def _bee_error_message(exc, limit: int = 200) -> Optional[str]:
    """Extract Bee's own error text from a failed request, if there is one.

    Bee reports refusals as JSON like {"code":400,"message":"out of funds"}.
    That message names the problem exactly; the exception string does not.
    Cut to `limit` characters; /pool/status asks for more and lets
    public_bee_text scrub before cutting (#549).
    """
    response = getattr(exc, "response", None)
    if response is None:
        return None
    try:
        body = response.json()
        if isinstance(body, dict):
            message = body.get("message") or body.get("detail")
            if message:
                return str(message)[:limit]
    except Exception:
        pass
    text = (getattr(response, "text", "") or "").strip()
    return text[:limit] or None



class PoolStampStatus(str, Enum):
    """Status of a stamp in the pool."""
    AVAILABLE = "available"  # Ready to be released
    RESERVED = "reserved"    # Held while an acquiring payment settles
    RELEASED = "released"    # Released to client, no longer managed


# Statuses that are still the pool's: they count toward the reserve target.
_HELD = (PoolStampStatus.AVAILABLE, PoolStampStatus.RESERVED)


@dataclass
class PoolStamp:
    """Represents a stamp in the pool."""
    batch_id: str
    depth: int
    amount: int  # PLUR amount
    created_at: datetime
    ttl_at_creation: int  # TTL in seconds when added to pool
    status: PoolStampStatus = PoolStampStatus.AVAILABLE
    label: Optional[str] = None
    released_at: Optional[datetime] = None
    released_to: Optional[str] = None  # IP or identifier of recipient
    # False while Bee lists the batch as not usable yet (just bought, still
    # propagating). It is still the pool's and counts toward the reserve
    # target, but it is never selected, reserved or released to a caller.
    usable: bool = True


# How long a full maintenance run waits for a held-batch re-check in progress
# before running anyway. A re-check is one Bee listing call.
_RECHECK_WAIT_SECONDS = 60
# How often to repeat the warning about batches held past the grace period.
_OVERDUE_HELD_LOG_SECONDS = 3600


def _not_yet_usable(stamp_data: Dict) -> bool:
    """Whether Bee lists a batch as not usable only because it is not ready yet.

    True when Bee says usable=false, the batch has TTL left, and no other
    reason applies (not expired, not expiring soon, not full, depth valid):
    a freshly bought batch waiting for Bee's usability delay. Such a batch is
    kept, not dropped as expired, so a restart or TTL check inside that delay
    does not strand a paid batch outside the pool.
    """
    return (not stamp_data.get("usable", False)
            and coerce_int(stamp_data.get("batchTTL"), 0) > 0
            and not stamp_data.get("unusableReason"))


@dataclass
class PoolStatus:
    """Current status of the stamp pool."""
    enabled: bool
    reserve_config: Dict[int, int]  # {depth: target_count}
    current_levels: Dict[int, int]  # {depth: current_count}
    available_stamps: Dict[int, List[str]]  # {depth: [batch_ids]}
    total_stamps: int
    low_reserve_warning: bool
    last_check: Optional[datetime]
    next_check: Optional[datetime]
    errors: List[str] = field(default_factory=list)
    # Bought but not usable yet, by depth: held, counted toward the target,
    # not handed out (see PoolStamp.usable).
    pending_stamps: Dict[int, int] = field(default_factory=dict)


class StampPoolManager:
    """
    Manages a pool of pre-purchased stamps for instant release.

    The pool maintains configured reserve levels for each stamp depth,
    purchases new stamps when reserves are low, and tops up stamps
    approaching expiration.
    """

    def __init__(self, state_file: Optional[str] = None):
        self._pool: Dict[str, PoolStamp] = {}  # batch_id -> PoolStamp
        self._lock = Lock()
        self._running = False
        # Single-flight guard for check_and_replenish. See its docstring:
        # overlapping runs each buy to cover the same shortfall.
        self._replenishing = False
        # A held-batch re-check in progress (_recheck_held). Its own flag, so a
        # full run is never skipped because of it: it waits for the re-check
        # (bounded) and then runs.
        self._rechecking = False
        self._recheck_done: Optional[asyncio.Event] = None
        # Last warning about batches held longer than the grace period.
        self._overdue_held_logged_at: Optional[datetime] = None
        # Background re-check step and the held set it last saw (backoff).
        self._recheck_step: float = settings.STAMP_POOL_HELD_RECHECK_SECONDS
        self._recheck_held_ids: frozenset = frozenset()
        self._since_recheck: float = 0.0
        # Batches whose top-up is running (#540). Kept AVAILABLE and counted as
        # held, but never selected, reserved or sold: an acquire during the
        # extend call would otherwise buy a batch the pool had just paid to
        # extend. Changed only under self._lock; see _topup_stamp.
        self._topup_in_flight: Set[str] = set()
        # Timestamps of recent purchases, for the hourly ceiling. See
        # _spend_budget_remaining().
        self._spend_times: List[datetime] = []
        # Until when pool spending is paused because the spend record could
        # not be read at startup (see _load_spend_times); None when it was
        # readable. Read by the gateway_state_file_unreadable gauge (#468).
        self._spend_times_unreadable_until: Optional[datetime] = None
        # Refusals by the spending ceilings since the last maintenance run. Kept
        # apart from _errors, which each run replaces with its own results, so
        # a refusal from a purchase or top-up is not lost from /pool/status.
        self._refusals: List[str] = []
        self._task: Optional[asyncio.Task] = None
        self._last_check: Optional[datetime] = None
        self._errors: List[str] = []
        self._pending_replenishments: Dict[int, int] = {}  # depth -> count of pending purchases
        self._state_file = state_file  # Allow override for testing
        self._spend_times = self._load_spend_times()
        # Purchases Bee has not listed yet: batch id -> {purchased_at, depth,
        # amount, ttl, label}. Persisted beside the state file so a restart
        # inside Bee's listing delay still holds the batch instead of dropping
        # it as gone; bounded by STAMP_POOL_UNLISTED_GRACE_SECONDS. An entry is
        # cleared once Bee lists the batch or the batch leaves the pool.
        self._pending: Dict[str, Dict] = self._load_pending()
        # False until a sync has actually read the node. Starts False so the very
        # first check cannot purchase against an unverified (empty) pool.
        self._last_sync_ok: bool = False
        # Why the last sync failed: "state_file" when the pool's own state file
        # could not be read, "bee" for anything else. The skip message names
        # the cause, so operators do not go looking at Bee for a file problem.
        self._last_sync_failure: Optional[str] = None
        self._last_sync_error: Optional[str] = None
        # Saves refused because the state file is unreadable (#547). While the
        # pool is paused for that, nothing may write over the file: it may hold
        # batch ids the in-memory pool does not. _state_dirty records that the
        # file may not match the in-memory pool (set by a refused save and by
        # finding the file unreadable), so the first good sync writes the
        # reconciled pool. _removed_unsaved holds batches that left the pool (sold,
        # expired) during the pause, so a restored backup that still lists them
        # cannot put a sold batch back up for sale. _refused_logged is the last
        # batch list logged, so a long pause logs it once per change.
        self._state_dirty: bool = False
        self._removed_unsaved: Set[str] = set()
        self._refused_logged: Optional[List[str]] = None
        # Set when the state file was found unreadable, cleared only by a sync
        # that completes fully: one that reads Bee as well as the file, or one
        # that takes the first-run path (the file holds `[]`, is missing, or
        # lists only batches released during the pause). Until then the in-memory pool has not been
        # reconciled with the file, so no save may write over it, even after
        # the file is restored: a sync that reads it and then fails at Bee has
        # not imported what the restored file lists (#547).
        self._needs_reconcile: bool = False
        # Batch ids the last successful sync kept in state without importing
        # (unreadable record or ownership lookup). Every save keeps them, or
        # the next release dropped them from the file. Each sync that reads
        # Bee evaluates them along with the file's ids, even if the file no
        # longer lists them, and replaces the set; the first-run path, which
        # does not read Bee, keeps it.
        self._unreadable_ids: Set[str] = set()
        # Per-depth backoff after a failed purchase. A failure is usually
        # persistent — out of funds, an amount below Bee's minimum validity —
        # and retrying it every cycle achieves nothing while blocking a request
        # handler for as long as Bee takes to refuse.
        self._backoff: Dict[int, datetime] = {}
        self._backoff_count: Dict[int, int] = {}

    def _is_backing_off(self, depth: int) -> bool:
        """Whether this depth is still waiting out a previous failure."""
        until = self._backoff.get(depth)
        return until is not None and datetime.now(timezone.utc) < until

    def _backoff_seconds(self, depth: int) -> int:
        """Exponential, capped. Repeated failures wait progressively longer."""
        n = self._backoff_count.get(depth, 0) + 1
        self._backoff_count[depth] = n
        return min(60 * (2 ** (n - 1)), 3600)

    @property
    def is_enabled(self) -> bool:
        """Check if stamp pool feature is enabled."""
        return settings.STAMP_POOL_ENABLED

    def get_reserve_config(self) -> Dict[int, int]:
        """Get the configured reserve levels by depth."""
        return settings.get_stamp_pool_reserve_config()

    def _get_state_file_path(self) -> str:
        """Get the state file path, using override or settings."""
        return self._state_file or settings.STAMP_POOL_STATE_FILE

    def _save_state(self, removed: Optional[Set[str]] = None) -> bool:
        """Persist current pool batch IDs to state file.

        Returns True when the file was written.

        `removed` names batches that have just left the pool. If the save is
        refused they are remembered, so a sync after recovery does not re-import
        them from a restored file.

        Never writes over an unreadable state file (#547). Once the file has
        been found unreadable, by a sync or by this check, saves are refused
        until a sync completes fully (reads Bee as well as the file, or takes
        the first-run path), even if the file is restored meanwhile:
        the file may list batches the in-memory pool does not, and writing over
        it would also end the pause without anyone repairing it. That sync
        writes the refused changes. A missing file is written as usual.

        Ids the last sync kept without importing (_unreadable_ids) are always
        written: records whose data could not be read. They are still ours and
        may parse fine next time, so writing only the pool would drop them.
        """
        state_file = self._get_state_file_path()
        batch_ids = sorted(set(self._pool.keys()) | self._unreadable_ids)

        refused = self._needs_reconcile
        if not refused:
            try:
                # Logs, keeps the #440 copy, and raises when the file exists but
                # cannot be read; returns normally for a readable or missing one.
                self._load_state(quiet=True)
            except StateLoadError:
                refused = True
                self._mark_state_file_unreadable()
        if refused:
            self._state_dirty = True
            if removed:
                self._removed_unsaved |= set(removed)
            if batch_ids != self._refused_logged:
                self._refused_logged = batch_ids
                # Full ids, so the operator can rebuild the file if the process
                # restarts before recovery. Batch ids are public on-chain.
                logger.error(
                    f"Pool state file {state_file} has not been reconciled with a full sync "
                    f"since it was found unreadable; not saving pool state over it. The pool "
                    f"currently holds {len(batch_ids)} batch(es): "
                    f"{json.dumps(batch_ids)}"
                    + (f"; released or removed since the last save: {json.dumps(sorted(self._removed_unsaved))}"
                       if self._removed_unsaved else "")
                )
            return False

        try:
            atomic_write_json(state_file, batch_ids)
            logger.debug(f"Saved pool state: {len(batch_ids)} stamps to {state_file}")
        except Exception as e:
            logger.error(f"Failed to save pool state to {state_file}: {e}")
            # Alerted on (#506): the file is now behind the in-memory pool.
            from app.services.metrics import count_state_save_failure
            count_state_save_failure("pool")
            # The file does not match the pool; the next sync (or the next
            # save) writes it again. This does not pause anything: only an
            # unreadable file does (#547).
            self._state_dirty = True
            return False
        self._state_dirty = False
        self._removed_unsaved.clear()
        self._refused_logged = None
        return True

    def state_file_unreadable(self) -> bool:
        """Whether pool spending is paused for an unreadable state file (#468).

        True from the moment the file is found unreadable until a sync has
        reconciled the pool with it (#547), which is when the pause lifts.
        """
        return self._needs_reconcile

    def spend_times_unreadable(self) -> bool:
        """Whether pool spending is paused for an unreadable spend record (#468).

        The pause fills the hourly ceiling with entries of the load time, so it
        lifts by itself an hour later; so does this.
        """
        until = self._spend_times_unreadable_until
        return until is not None and datetime.now(timezone.utc) < until

    def _mark_state_file_unreadable(self) -> None:
        """Pause pool spending because the state file cannot be read.

        Used by the sync and by a save that finds the file broken between syncs.
        Base name only in _errors: /pool/status is unauthenticated, and the full
        path and backup location are already in the log.
        """
        name = os.path.basename(self._get_state_file_path())
        self._last_sync_error = (f"Sync error: the pool state file ({name}) could not "
                                 "be read; see the gateway log for details.")
        if self._last_sync_error not in self._errors:
            self._errors.append(self._last_sync_error)
        self._last_sync_failure = "state_file"
        self._last_sync_ok = False
        self._needs_reconcile = True
        # The file can no longer be trusted to match the pool, so recovery
        # writes the reconciled pool whether or not a save was refused in the
        # meantime. Without this, recovering by moving the file away, or by
        # restoring an older backup or `[]`, left it short of the batches the
        # pool holds, and a restart then lost them (#547).
        self._state_dirty = True

    def _log_recovered(self) -> None:
        """Confirm in the log that a state-file pause is over (#547)."""
        n = len(set(self._pool.keys()) | self._unreadable_ids)
        name = os.path.basename(self._get_state_file_path())
        logger.info(f"Pool state file ({name}) readable again; pause lifted, wrote {n} batch ids")

    def _sync_succeeded(self, flush: bool = True) -> None:
        """Lift any pause, and write the pool if state is waiting to be saved (#547).

        State is waiting once the file has been found unreadable
        (_mark_state_file_unreadable marks it), whether or not a save was
        refused since, so the sync that ends a state-file pause always writes
        the reconciled pool. It stays waiting until a write succeeds. When
        nothing is waiting, this writes nothing.

        `flush` is False when the caller has just attempted that save itself,
        so a failing write is not tried and logged twice.
        """
        recovering = self._needs_reconcile
        self._last_sync_ok = True
        self._last_sync_failure = None
        self._last_sync_error = None
        self._needs_reconcile = False
        if flush and self._state_dirty:
            with self._lock:
                saved = self._save_state()
            if saved and recovering:
                self._log_recovered()

    def _load_state(self, quiet: bool = False) -> Set[str]:
        """Load the pool's batch IDs from its state file.

        A MISSING file is the only case where starting empty is correct: it is a
        genuine first run. Every other failure raises StateLoadError, after
        copying the file aside once (#440).

        Treating a corrupt file as a first run loses the pool's batches twice
        over: the gateway buys a fresh reserve it already owns, and the next
        _save_state overwrites the only record of the originals with the new
        list. They then live out their TTL owned by the gateway, unacquirable and
        unwritable, having been paid for.

        Raising instead makes the sync fail, which sets _last_sync_ok = False and
        stops replenishment (see check_and_replenish) rather than reading an empty
        pool as a real deficit. The permission case already did this after #416;
        this brings corrupt JSON, a wrong top-level type and anything unexpected
        into line with it.
        """
        state_file = self._get_state_file_path()
        try:
            with open(state_file, 'r') as f:
                batch_ids = json.load(f)
        except FileNotFoundError:
            if not quiet:
                logger.info(f"No pool state file at {state_file}, treating as first run")
            return set()
        except Exception as e:
            # Unparseable JSON, a permission or I/O error, anything else. Not a
            # first run, so do not answer as though it were.
            logger.error(f"Cannot read pool state file {state_file}: {e}; "
                         "pool spending is paused until it is repaired")
            raise unreadable_state(state_file, e) from e

        if not isinstance(batch_ids, list):
            logger.error(f"Pool state in {state_file} is a {type(batch_ids).__name__}, "
                         "expected a list; pool spending is paused until it is repaired")
            raise unreadable_state(
                state_file, f"expected a JSON list of batch IDs, got {type(batch_ids).__name__}")

        # Element types are checked here rather than left to set(): a nested
        # object raises an opaque "unhashable type" from a later line, which
        # reads as a bug in the pool rather than a corrupt file.
        bad = [b for b in batch_ids if not isinstance(b, str)]
        if bad:
            logger.error(f"Pool state in {state_file} has {len(bad)} non-string entries; "
                         "pool spending is paused until it is repaired")
            raise unreadable_state(
                state_file, f"expected strings, got {type(bad[0]).__name__} among {len(batch_ids)} entries")

        if not quiet:
            logger.info(f"Loaded pool state: {len(batch_ids)} stamps from {state_file}")
        return set(batch_ids)

    def get_status(self) -> PoolStatus:
        """Get current pool status."""
        with self._lock:
            reserve_config = self.get_reserve_config()

            # Count available stamps by depth
            current_levels: Dict[int, int] = {}
            available_stamps: Dict[int, List[str]] = {}

            for batch_id, stamp in self._pool.items():
                if stamp.status == PoolStampStatus.AVAILABLE and stamp.usable:
                    depth = stamp.depth
                    current_levels[depth] = current_levels.get(depth, 0) + 1
                    if depth not in available_stamps:
                        available_stamps[depth] = []
                    available_stamps[depth].append(batch_id)

            pending_stamps: Dict[int, int] = {}
            for stamp in self._pool.values():
                if stamp.status == PoolStampStatus.AVAILABLE and not stamp.usable:
                    pending_stamps[stamp.depth] = pending_stamps.get(stamp.depth, 0) + 1

            # Check if any depth is below threshold
            low_reserve_warning = False
            for depth, target in reserve_config.items():
                current = current_levels.get(depth, 0)
                if current <= settings.STAMP_POOL_LOW_RESERVE_THRESHOLD and current < target:
                    low_reserve_warning = True
                    break

            # Calculate next check time
            next_check = None
            if self._running and self._last_check:
                interval = settings.STAMP_POOL_CHECK_INTERVAL_SECONDS
                next_check = datetime.fromtimestamp(
                    self._last_check.timestamp() + interval,
                    tz=timezone.utc
                )

            return PoolStatus(
                enabled=self.is_enabled,
                reserve_config=reserve_config,
                current_levels=current_levels,
                available_stamps=available_stamps,
                total_stamps=len([s for s in self._pool.values()
                                  if s.status == PoolStampStatus.AVAILABLE and s.usable]),
                low_reserve_warning=low_reserve_warning,
                last_check=self._last_check,
                next_check=next_check,
                errors=self._errors.copy(),
                pending_stamps=pending_stamps,
            )

    def get_available_stamp(self, depth: int) -> Optional[PoolStamp]:
        """
        Get an available stamp of the requested depth.

        Returns None if no stamp is available.
        Does NOT release the stamp - call release_stamp() after confirming.
        """
        with self._lock:
            for batch_id, stamp in self._pool.items():
                if (stamp.depth == depth and stamp.status == PoolStampStatus.AVAILABLE
                        and stamp.usable and batch_id not in self._topup_in_flight):
                    return stamp
            return None

    def get_available_stamp_any_size(self, min_depth: int = 17) -> Optional[PoolStamp]:
        """
        Get any available stamp with at least the minimum depth.

        Prefers exact match, then smallest larger stamp.
        Returns None if no suitable stamp is available.
        """
        with self._lock:
            available = [
                s for s in self._pool.values()
                if s.status == PoolStampStatus.AVAILABLE and s.usable and s.depth >= min_depth
                and s.batch_id not in self._topup_in_flight
            ]
            if not available:
                return None

            # Sort by depth (prefer smaller depth first)
            available.sort(key=lambda s: s.depth)
            return available[0]

    def release_stamp(
        self,
        batch_id: str,
        released_to: Optional[str] = None
    ) -> Optional[PoolStamp]:
        """
        Release a stamp from the pool to a client.

        The stamp is marked as released and will no longer be managed by the pool.
        The recipient is responsible for any future top-ups.

        Args:
            batch_id: The batch ID of the stamp to release
            released_to: Optional identifier of the recipient (IP, user ID, etc.)

        Returns:
            The released stamp, or None if not found/not available
        """
        with self._lock:
            stamp = self._pool.get(batch_id)
            if not stamp or stamp.status != PoolStampStatus.AVAILABLE or not stamp.usable:
                return None
            if batch_id in self._topup_in_flight:
                return None

            stamp.status = PoolStampStatus.RELEASED
            stamp.released_at = datetime.now(timezone.utc)
            stamp.released_to = released_to

            logger.info(f"Released stamp {batch_id[:16]}... (depth={stamp.depth}) to {released_to or 'unknown'}")

            # Remove from pool (we no longer manage it)
            del self._pool[batch_id]

            self._forget_pending_locked(batch_id)
            self._save_state(removed={batch_id})
            return stamp

    def reserve_stamp(
        self,
        batch_id: str,
        reserved_for: Optional[str] = None
    ) -> Optional[PoolStamp]:
        """Hold an available batch for a caller whose payment is still settling.

        The batch stays in the pool and in the state file while settlement runs,
        which can take seconds. Taking it out instead (release first, put back on
        failure) left a window in which a replenish check saw one batch fewer and
        bought an extra, and in which a crash dropped the batch from pool state
        so it sat on the node unused until it expired (#403).

        A reserved batch counts toward the reserve target but is never selected
        or sold. Finish with release_reserved_stamp() once payment has settled,
        or unreserve_stamp() if it has not. Statuses are not persisted, so a
        batch reserved when the process dies is simply available after restart.

        A batch whose top-up is in flight is not reserved (#540): the pool has
        just paid to extend it, so the acquire takes another batch.

        Returns:
            The reserved stamp, or None if not found/not available (another
            request got it first, or it is being topped up)
        """
        with self._lock:
            stamp = self._pool.get(batch_id)
            if not stamp or stamp.status != PoolStampStatus.AVAILABLE or not stamp.usable:
                return None
            if batch_id in self._topup_in_flight:
                return None
            stamp.status = PoolStampStatus.RESERVED
            stamp.released_to = reserved_for
            return stamp

    def release_reserved_stamp(self, batch_id: str) -> Optional[PoolStamp]:
        """Hand a reserved batch to its caller: payment settled, the pool lets go."""
        with self._lock:
            stamp = self._pool.get(batch_id)
            if not stamp or stamp.status != PoolStampStatus.RESERVED:
                return None
            stamp.status = PoolStampStatus.RELEASED
            stamp.released_at = datetime.now(timezone.utc)
            del self._pool[batch_id]
            self._forget_pending_locked(batch_id)
            self._save_state(removed={batch_id})
        logger.info(f"Released stamp {batch_id[:16]}... (depth={stamp.depth}) to {stamp.released_to or 'unknown'}")
        return stamp

    def unreserve_stamp(self, batch_id: str) -> None:
        """Make a reserved batch available again: its payment did not settle."""
        with self._lock:
            stamp = self._pool.get(batch_id)
            if not stamp or stamp.status != PoolStampStatus.RESERVED:
                return
            stamp.status = PoolStampStatus.AVAILABLE
            stamp.released_to = None
        logger.info(f"Reservation on {batch_id[:16]}... cancelled, back in the pool")

    def _immediate_replenish_paused(self, depth: int) -> bool:
        """True when the last sync failed, so a release must not buy (#533).

        The same rule as the maintenance path (see _check_and_replenish_locked):
        after any failed sync the pool's contents are unknown. For an unreadable
        state file, buying would also add a batch the gateway cannot reliably
        record, and the skip message promises spending is paused until the file
        is repaired. A Bee failure is usually transient; the next good sync
        lifts the pause and maintenance refills the pool.

        A file that breaks after that sync is noticed by the next save (a
        release, purchase or expiry removal), which pauses the pool the same
        way (#547). _purchase_stamp and _topup_stamp check that pause again
        right before Bee is asked to spend.
        """
        if self._last_sync_ok:
            return False
        if self._last_sync_failure == "state_file":
            cause = "the pool state file could not be read"
        else:
            cause = "the last sync with the Bee node failed"
        logger.warning(
            f"Pool depth {depth}: not replenishing immediately because {cause}; "
            "the pool's current contents are unknown."
        )
        return True

    def trigger_replenishment_if_needed(self, depth: int) -> bool:
        """
        Check if replenishment is needed for the given depth and trigger async purchase.

        This is called after a stamp is released to immediately start purchasing
        a replacement if the reserve count is below target.

        Args:
            depth: The depth of the stamp that was just released

        Returns:
            True if a replenishment task was triggered, False otherwise
        """
        if not settings.STAMP_POOL_IMMEDIATE_REPLENISH:
            logger.debug(f"Immediate replenishment disabled, skipping for depth {depth}")
            return False

        if self._immediate_replenish_paused(depth):
            return False

        reserve_config = self.get_reserve_config()
        target_count = reserve_config.get(depth, 0)

        if target_count == 0:
            # This depth is not configured for pooling
            return False

        # Count current available stamps for this depth
        with self._lock:
            # Reserved batches count: they are still the pool's until their
            # payment settles, and go back to available if it fails.
            current_count = len([
                s for s in self._pool.values()
                if s.depth == depth and s.status in _HELD
            ])
            pending_count = self._pending_replenishments.get(depth, 0)

        # If we're at or above target (including pending), no action needed
        effective_count = current_count + pending_count
        if effective_count >= target_count:
            logger.debug(
                f"Pool depth {depth}: no replenishment needed "
                f"(have {current_count}, pending {pending_count}, target {target_count})"
            )
            return False

        # Need to replenish - spawn async task
        logger.info(
            f"Pool depth {depth}: triggering immediate replenishment "
            f"(have {current_count}, pending {pending_count}, target {target_count})"
        )

        # Track pending replenishment
        with self._lock:
            self._pending_replenishments[depth] = pending_count + 1

        # Spawn fire-and-forget async task
        asyncio.create_task(self._async_replenish_one(depth))

        return True

    async def _async_replenish_one(self, depth: int):
        """
        Async task to purchase one stamp for replenishment.

        This is fire-and-forget - errors are logged but don't affect the caller.
        """
        try:
            # Checked again here: a sync can fail between spawning this task
            # and the task running.
            if self._immediate_replenish_paused(depth):
                return
            logger.info(f"Immediate replenishment: starting purchase for depth {depth}")
            batch_id = await self._purchase_stamp(depth)
            if batch_id:
                logger.info(f"Immediate replenishment: successfully purchased stamp {batch_id[:16]}... for depth {depth}")
            elif not self._needs_reconcile:
                # Not when the state-file pause stopped it: that is logged
                # already, by _spending_paused_for_state_file.
                logger.warning(f"Immediate replenishment: purchase returned no batch_id for depth {depth}")
        except Exception as e:
            logger.error(f"Immediate replenishment failed for depth {depth}: {e}")
            self._errors.append(f"Immediate replenishment failed (depth {depth}): "
                                f"{error_cause(e)}; see the gateway log for details.")
        finally:
            # Remove from pending count
            with self._lock:
                current_pending = self._pending_replenishments.get(depth, 1)
                if current_pending <= 1:
                    self._pending_replenishments.pop(depth, None)
                else:
                    self._pending_replenishments[depth] = current_pending - 1

    def add_stamp_to_pool(self, batch_id: str, depth: int, amount: int, ttl: int,
                          label: Optional[str] = None, usable: bool = True,
                          purchased_at: Optional[datetime] = None) -> PoolStamp:
        """
        Add a newly purchased stamp to the pool.

        Args:
            batch_id: The batch ID of the stamp
            depth: Stamp depth
            amount: Amount in PLUR
            ttl: TTL in seconds
            label: Optional label
            usable: False while Bee does not list the batch as usable yet;
                it is held but not handed out until a TTL check sees it usable
            purchased_at: When Bee accepted the purchase (default: now). The
                grace period for a batch Bee has not listed yet counts from it.

        Returns:
            The created PoolStamp
        """
        with self._lock:
            stamp = PoolStamp(
                batch_id=batch_id,
                depth=depth,
                amount=amount,
                created_at=datetime.now(timezone.utc),
                ttl_at_creation=ttl,
                status=PoolStampStatus.AVAILABLE,
                label=label or f"pool-{depth}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
                usable=usable,
            )
            self._pool[batch_id] = stamp
            logger.info(f"Added stamp {batch_id[:16]}... to pool (depth={depth})"
                        + ("" if usable else ", not usable yet; held until Bee lists it usable"))
            if not usable:
                # Bee may not even list it yet. Remember when it was bought, so
                # sync and the TTL check hold it while unlisted, for a bounded
                # time, instead of dropping it as gone.
                self._pending[batch_id] = {
                    "purchased_at": (purchased_at or stamp.created_at).isoformat(),
                    "depth": depth,
                    "amount": amount,
                    "ttl": ttl,
                    "label": stamp.label,
                }
                self._persist_pending_locked()
            self._save_state()

        # Register as gateway-owned, outside the lock — the ownership manager
        # takes its own.
        #
        # Ownership used to be recorded only when a caller ACQUIRED a batch, so
        # everything sitting in the pool was untracked, and check_access let any
        # caller write to an untracked batch. A production pool batch reached 50%
        # utilisation without ever being acquired: capacity the gateway had paid
        # for, consumed by someone who never asked for it (#312).
        #
        # Registering at purchase closes the window entirely — the batch is owned
        # from the moment it exists. Acquiring it re-registers it to the caller.
        try:
            from app.services.stamp_ownership import POOL_OWNER, stamp_ownership_manager
            stamp_ownership_manager.register_stamp(
                batch_id=batch_id,
                owner=POOL_OWNER,
                mode="pool",
                source="pool_purchase",
            )
        except Exception as e:
            # Never fail a purchase over bookkeeping — the batch exists and was
            # paid for. Log loudly: until it is registered it is writable by
            # anyone, which is the whole point of this change.
            logger.error(
                f"Failed to register pool ownership for {batch_id[:16]}...: {e}. "
                "The batch is unprotected until registered."
            )

        return stamp

    def _register_pool_ownership(self, batch_ids: Set[str]) -> None:
        """Record pool-held batches as gateway-owned, so nobody may write to them."""
        if not batch_ids:
            return
        try:
            from app.services.stamp_ownership import POOL_OWNER, stamp_ownership_manager
            for batch_id in batch_ids:
                # Now that the registry survives restarts (#349), a batch the
                # pool state still lists may already belong to a caller: the
                # pool-state save after an acquire can fail while the ownership
                # save succeeds. Handing such a batch back to the pool would
                # take it from someone who paid for it. Only claim batches that
                # are unregistered or already the pool's.
                existing = stamp_ownership_manager.get_stamp_info(batch_id)
                if existing is not None:
                    if existing.get("owner") != POOL_OWNER:
                        logger.warning(
                            f"Pool state lists {batch_id[:16]}..., but it is registered to "
                            f"{str(existing.get('owner'))[:16]}; leaving it with its owner."
                        )
                    continue
                stamp_ownership_manager.register_stamp(
                    batch_id=batch_id,
                    owner=POOL_OWNER,
                    mode="pool",
                    source="pool_sync",
                )
        except Exception as e:
            logger.error(
                f"Failed to register pool ownership for {len(batch_ids)} batch(es): {e}. "
                "They are unprotected until registered."
            )

    async def sync_from_bee_node(self) -> int:
        """
        Sync pool with stamps from the Bee node using persisted state.

        Only re-imports stamps whose batch IDs are in the state file.
        On first run (no state file), imports nothing — the purchase logic
        will fill the pool to the configured reserve target.

        Returns:
            Number of stamps synced
        """
        try:
            # Batches that left the pool while saves were refused (#547) are not
            # ours any more, even if a restored file still lists them.
            known_ids = self._load_state() - self._removed_unsaved
            # Purchases Bee has not listed yet are ours even if the file lost
            # them; they are evaluated below (held within the grace period).
            known_ids |= set(self._pending) - self._removed_unsaved

            # First run: no state file means empty pool, let purchase logic fill it
            if not known_ids:
                logger.info("No known stamps in state file, pool will be filled by purchase logic")
                # _unreadable_ids are kept: this path does not ask Bee, so
                # nothing says they are gone or readable now (#547).
                # A genuinely empty state is a reliable answer, not a failure.
                self._sync_succeeded()
                return 0

            all_stamps = await swarm_api.get_all_stamps_processed()
            stamp_map = {s.get("batchID"): s for s in all_stamps}
            synced_count = 0
            valid_ids = set()
            # IDs we could not parse this cycle. They are kept in the state file
            # so a transient unreadable field does not permanently lose a stamp.
            unreadable_ids = set()

            with self._lock:
                # Decide against the state as it is now, not as it was before
                # the await above. A batch handed to a caller while Bee was
                # answering has left the pool and the state file, but is still
                # in the earlier read and still usable on the node; importing it
                # would put a sold batch back up for sale.
                file_ids = self._load_state()
                known_ids &= file_ids - self._removed_unsaved
                # Ids an earlier sync kept without importing are still ours even
                # if the file no longer lists them, as after restoring an older
                # backup (#547). Evaluate them with the rest: imported once
                # readable, dropped once Bee no longer lists them, else kept.
                known_ids |= self._unreadable_ids - self._removed_unsaved
                known_ids |= set(self._pending) - self._removed_unsaved
                now = datetime.now(timezone.utc)
                listed_pending: Set[str] = set()
                for batch_id in known_ids:
                    # Never put a batch registered to someone else up for sale
                    # (#477). _save_state swallows write errors, so a failed
                    # save after a release leaves the sold batch in the state
                    # file; without this check the next sync re-imported it as
                    # AVAILABLE. Unregistered batches (pre-#312 inventory) and
                    # the pool's own still import.
                    try:
                        record = stamp_ownership_manager.get_stamp_info(batch_id)
                    except Exception as e:
                        # Fail safe: neither import nor forget it. Kept in
                        # state so the next sync tries again.
                        logger.error(
                            f"Cannot read the ownership record of pool batch {batch_id[:16]}...: "
                            f"{e}; not importing it this cycle, keeping it in state"
                        )
                        if batch_id in self._pool:
                            # Already held: nothing is imported, so leave it.
                            valid_ids.add(batch_id)
                        else:
                            unreadable_ids.add(batch_id)
                        continue
                    if record is not None and record.get("owner") != POOL_OWNER:
                        owner_prefix = str(record.get("owner"))[:16]
                        held = self._pool.get(batch_id)
                        if held is not None and held.status == PoolStampStatus.RESERVED:
                            # Mid-acquire: release_reserved_stamp must still
                            # find it, or a paid caller gets nothing.
                            logger.error(
                                f"Pool batch {batch_id[:16]}... is registered to {owner_prefix} "
                                "but RESERVED for an acquire in progress; left for that acquire"
                            )
                            valid_ids.add(batch_id)
                        else:
                            logger.error(
                                f"Pool state lists {batch_id[:16]}..., but it is registered to "
                                f"{owner_prefix}; not importing it, dropping it from pool state "
                                "(an earlier pool-state save probably failed)"
                            )
                            self._pool.pop(batch_id, None)
                            if self._pending.pop(batch_id, None) is not None:
                                self._persist_pending_locked()
                        continue

                    # Skip if already in pool
                    if batch_id in self._pool:
                        valid_ids.add(batch_id)
                        continue

                    stamp_data = stamp_map.get(batch_id)
                    if not stamp_data:
                        if self._pending_within_grace(batch_id, now):
                            # Bought shortly before the restart; Bee has not
                            # listed it yet. Hold it, not for sale.
                            info = self._pending[batch_id]
                            self._pool[batch_id] = PoolStamp(
                                batch_id=batch_id,
                                depth=int(info["depth"]),
                                amount=int(info["amount"]),
                                created_at=self._pending_purchased_at(batch_id),
                                ttl_at_creation=int(info["ttl"]),
                                status=PoolStampStatus.AVAILABLE,
                                label=info.get("label"),
                                usable=False,
                            )
                            valid_ids.add(batch_id)
                            logger.info(
                                f"Known stamp {batch_id[:16]}... not listed by Bee yet; bought "
                                f"{self._pending_age(batch_id, now)}s ago, holding it (not usable yet)"
                            )
                            continue
                        if batch_id in self._pending:
                            self._drop_unlisted_pending_locked(batch_id)
                            continue
                        # Stamp no longer exists on Bee node (expired/removed)
                        logger.info(f"Known stamp {batch_id[:16]}... no longer on Bee node, removing from state")
                        continue
                    if batch_id in self._pending:
                        # Bee lists it now; the usable rules below decide.
                        # The entry is cleared only once the state file lists
                        # the batch (after the save below), or a failed save
                        # followed by a restart would strand it.
                        listed_pending.add(batch_id)

                    usable = bool(stamp_data.get("usable", False))
                    ttl = coerce_int(stamp_data.get("batchTTL"), 0)
                    # Not usable YET (bought just before a restart, still in
                    # Bee's usability delay) is not expired: import it as held
                    # but not for sale, or the paid batch leaves the pool and
                    # the state file while its ownership record still says
                    # "pool", and nothing ever picks it up again.
                    pending = _not_yet_usable(stamp_data)

                    if not (usable or pending) or ttl <= 0:
                        logger.info(f"Known stamp {batch_id[:16]}... is expired/unusable, removing from state")
                        continue

                    # Re-import this known stamp. Parsing is per-record: Bee returns
                    # `amount` as null on /batches entries (verified on a live node:
                    # every batch), and the merged view only fills it in from /stamps,
                    # which can be incomplete while the node is starting. A record we
                    # cannot read is skipped and kept in state for the next attempt —
                    # it must not discard the rest of the sync, because an unreadable
                    # pool previously read as an empty one and triggered purchasing.
                    try:
                        depth = stamp_data.get("depth")
                        if depth is None:
                            raise ValueError("missing depth")
                        depth = int(depth)
                        amount = coerce_int(stamp_data.get("amount"), 0)
                    except (TypeError, ValueError) as e:
                        logger.warning(
                            f"Known stamp {batch_id[:16]}... has unreadable data "
                            f"({e}); skipping it this cycle, keeping it in state"
                        )
                        valid_ids.add(batch_id)
                        unreadable_ids.add(batch_id)
                        continue

                    label = stamp_data.get("label", "")
                    stamp = PoolStamp(
                        batch_id=batch_id,
                        depth=depth,
                        amount=amount,
                        created_at=datetime.now(timezone.utc),
                        ttl_at_creation=ttl,
                        status=PoolStampStatus.AVAILABLE,
                        label=label or f"synced-{depth}",
                        usable=usable,
                    )
                    self._pool[batch_id] = stamp
                    valid_ids.add(batch_id)
                    synced_count += 1
                    logger.info(f"Synced known stamp {batch_id[:16]}... to pool (depth={depth}, ttl={ttl}s"
                                + ("" if usable else ", not usable yet") + ")")

            # Save cleaned state (stamps still valid, plus any we could not read
            # this cycle — those are not in the pool but must not be forgotten).
            # The file and Bee have both been read and the pool reconciled with
            # them, so a state-file pause is over: clear it first, or this save
            # would be refused as during the pause (#547).
            recovering = self._needs_reconcile
            self._last_sync_failure = None
            self._needs_reconcile = False
            self._unreadable_ids = set(unreadable_ids)
            # Write when the file would change: something dropped, or a batch
            # held now that the file does not list (one known only from the
            # pending record, or a carried unreadable id). Comparing against
            # known_ids, which includes those, left a held pending batch out of
            # the file; once Bee listed it and the pending entry was cleared, a
            # second restart stranded it.
            need_save = (set(self._pool) | self._unreadable_ids) != file_ids or self._state_dirty
            saved = False
            if need_save:
                saved = self._save_state()
                if saved and recovering:
                    self._log_recovered()
            if listed_pending:
                self._clear_listed_pending(listed_pending, file_lists_pool=saved or not need_save)

            # Adopt everything in the pool as gateway-owned.
            #
            # Registering at purchase (add_stamp_to_pool) only protects batches
            # bought from now on. Batches already held predate that and would
            # stay untracked — and therefore writable by anyone — for as long as
            # they live, which is exactly the state that let a production batch
            # reach 50% utilisation unasked (#312).
            #
            # Idempotent, and does not disturb a batch already owned by someone:
            # a batch acquired by a caller was removed from the pool at release,
            # and _register_pool_ownership skips any batch recorded to another
            # owner. A RESERVED batch is still the pool's; the acquiring handler
            # registers its caller only once it has been released.
            self._register_pool_ownership(valid_ids)

            # Held changes were written (or attempted) by the save above. If
            # that save found the file broken since this sync read it, it has
            # paused the pool again; do not lift that pause.
            if not self._needs_reconcile:
                self._sync_succeeded(flush=False)
            return synced_count

        except StateLoadError as e:
            # The full text names the absolute state-file path and its backup.
            # It goes to the log only: /pool/status is unauthenticated, so the
            # entry there names the file by its base name.
            logger.error(f"Error syncing pool: cannot read the pool state file: {e}")
            self._mark_state_file_unreadable()
            return 0

        except Exception as e:
            # The full text can name the Bee API address (httpx puts the URL
            # in HTTPStatusError). It goes to the log only (#534).
            logger.error(f"Error syncing stamps from Bee node: {e}")
            self._last_sync_error = (f"Sync error: could not read stamps from the Bee node "
                                     f"({error_cause(e)}); see the gateway log for details.")
            self._errors.append(self._last_sync_error)
            self._last_sync_failure = "bee"
            # The pool's contents are now UNKNOWN, not known-to-be-empty. Record
            # that so replenishment does not read the empty pool as a real deficit.
            self._last_sync_ok = False
            return 0

    async def check_and_replenish(self) -> Dict[str, any]:
        """
        Check pool levels and replenish if needed.

        This is the main maintenance function called by the background task.

        Only one runs at a time. Two overlapping checks each see the same
        shortfall and each buy to cover it, so the pool ends up with twice the
        reserve and the wallet pays for it. The timer alone could not overlap,
        but POST /api/v1/pool/check now schedules this too, and an operator can
        trigger it while a scheduled run is already in flight.

        A second caller returns immediately with skipped=True rather than
        waiting, because waiting for a purchase is the behaviour #292 removed.

        Returns:
            Dict with results of the check
        """
        results = {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "stamps_purchased": 0,
            "stamps_topped_up": 0,
            "errors": []
        }

        if not self.is_enabled:
            return results

        # A plain flag rather than an asyncio.Lock. With a lock, the check and
        # the acquire are two steps: a second caller can pass `locked()` while
        # the first has not acquired yet, and then WAIT on the acquire instead of
        # skipping — running a redundant replenish afterwards and holding a task
        # for the duration. There is no await between the test and the set here,
        # and asyncio is single-threaded, so this cannot interleave.
        if self._replenishing:
            logger.info("Pool maintenance already in progress — skipping this run")
            results["skipped"] = True
            return results

        self._replenishing = True
        try:
            if self._rechecking and self._recheck_done is not None:
                # A held-batch re-check is running its TTL check. Let it
                # finish rather than run the same check alongside it, but
                # never skip this run because of it.
                try:
                    await asyncio.wait_for(self._recheck_done.wait(), timeout=_RECHECK_WAIT_SECONDS)
                except asyncio.TimeoutError:
                    logger.warning(f"Held-batch re-check still running after {_RECHECK_WAIT_SECONDS}s; "
                                   "running pool maintenance anyway")
            return await self._check_and_replenish_locked(results)
        finally:
            self._replenishing = False

    def _spending_paused_for_state_file(self, what: str) -> bool:
        """True while the state file has not been read back since it was found
        unreadable (#547). Checked right before Bee is asked to spend, after the
        last await, because a save during a price lookup or a 429 wait can set
        the pause after the caller last looked."""
        if not self._needs_reconcile:
            return False
        logger.warning(f"Not going ahead to {what}: the pool state file could not be read, "
                       "so pool spending is paused until it is repaired or restored.")
        return True

    def _skip_replenishment(self, results: Dict[str, any]) -> Dict[str, any]:
        """End a maintenance run without spending: the pool's contents are unknown.

        Used after a failed sync, and when a save pauses the pool part-way
        through a run (#547), so the rest of that run buys and tops up nothing.
        """
        if self._last_sync_failure == "state_file":
            # Base name and setting only: /pool/status is unauthenticated.
            # The full path and backup location are logged by
            # _load_state and sync_from_bee_node, never put in _errors.
            name = os.path.basename(self._get_state_file_path())
            msg = (f"Skipping replenishment: the pool state file ({name}, set by "
                   "STAMP_POOL_STATE_FILE) could not be read, so the pool's "
                   "current contents are unknown. Pool spending is paused until "
                   "the file is repaired or restored.")
        else:
            msg = ("Skipping replenishment: could not read stamp state from the "
                   "Bee node, so the pool's current contents are unknown. "
                   "Will retry on the next check.")
            if self._needs_reconcile:
                # The file was unreadable earlier and has not been reconciled
                # yet (#547). The file is not named: this cause is Bee, and the
                # message must not send operators to a file that now reads
                # fine (#533); the earlier entry named it.
                msg += (" Pool state saves stay held until a sync with the Bee "
                        "node completes, because the state file was unreadable earlier.")
        logger.warning(msg)
        # Replace _errors with this run's entries, as the normal path
        # does. Returning without it let sync append one "Sync error"
        # per failed cycle, and a corrupt state file fails every cycle
        # until repaired, so the list grew without limit (#482).
        if self._last_sync_error:
            results["errors"].append(self._last_sync_error)
        results["errors"].append(msg)
        results["errors"].extend(self._refusals)
        self._refusals.clear()
        self._errors = results["errors"]
        results["skipped"] = True
        return results

    async def _check_and_replenish_locked(self, results: Dict[str, any]) -> Dict[str, any]:
        """The body of check_and_replenish, with the single-flight guard held."""
        try:
            self._last_check = datetime.now(timezone.utc)
            reserve_config = self.get_reserve_config()

            # First, sync existing stamps from node
            synced = await self.sync_from_bee_node()
            results["stamps_synced"] = synced

            # If that sync failed, the pool's contents are unknown — the in-memory
            # pool is empty because nothing could be read back, not because the
            # node holds no stamps. Purchasing here buys a full reserve on top of
            # stamps that already exist. This happens on every restart where the
            # Bee node is still starting up (it answers /batches with 503 until it
            # has finished syncing), which is precisely when the gateway restarts
            # alongside it, so the race is the normal case rather than a rare one.
            if not self._last_sync_ok:
                return self._skip_replenishment(results)

            # Update TTL information for pool stamps
            await self._update_stamp_ttls()

            # Check levels for each depth
            for depth, target_count in reserve_config.items():
                # Reserved batches count toward the target (see reserve_stamp):
                # counting only available ones bought an extra batch whenever a
                # check ran while a paid acquire was settling.
                current_count = len([
                    s for s in self._pool.values()
                    if s.depth == depth and s.status in _HELD
                ])

                # Purchase new stamps if below target
                needed = target_count - current_count
                if needed > 0 and self._is_backing_off(depth):
                    until = self._backoff[depth].isoformat()
                    msg = (f"Pool depth {depth}: {needed} short, but the last purchase "
                           f"failed — not retrying until {until}")
                    logger.info(msg)
                    results["errors"].append(msg)
                elif needed > 0:
                    logger.info(f"Pool depth {depth}: need {needed} stamps (have {current_count}, target {target_count})")
                    for i in range(needed):
                        try:
                            # Delay between purchases to avoid Bee node rate limiting (429)
                            if i > 0:
                                await asyncio.sleep(15)
                            # A save during this run may have found the state
                            # file unreadable and paused the pool (#547).
                            if not self._last_sync_ok:
                                return self._skip_replenishment(results)
                            batch_id = await self._purchase_stamp(depth)
                            if batch_id:
                                results["stamps_purchased"] += 1
                                # Working again: forget the previous failure.
                                self._backoff.pop(depth, None)
                                self._backoff_count.pop(depth, None)
                        except Exception as e:
                            detail = _bee_error_message(e)
                            said = f" — Bee said: {detail}" if detail else ""
                            logger.error(f"Failed to purchase depth-{depth} stamp: {e}{said}")
                            # Bee's own message, not str(e): that names the Bee URL (#534).
                            # Scrubbed at 2 KB, cut to 200, and shown only if the
                            # safety gate passes it (#549).
                            shown = _bee_error_message(e, SCRUB_CAP + 1)
                            shown = public_bee_text(shown) if shown else None
                            shown = f" — Bee said: {shown}" if shown else ""
                            results["errors"].append(
                                f"Failed to purchase depth-{depth} stamp: "
                                f"{error_cause(e)}{shown}")
                            # Back off this depth. Without this the next check
                            # tries again immediately and keeps failing: an
                            # underfunded pool retried an unaffordable purchase
                            # every cycle indefinitely, each attempt holding a
                            # request handler for as long as Bee took to refuse.
                            self._backoff[depth] = (
                                datetime.now(timezone.utc)
                                + timedelta(seconds=self._backoff_seconds(depth))
                            )
                            break

            # Top up stamps with low TTL — but only at depths the pool still
            # wants to hold.
            #
            # This used to top up every batch in the pool regardless of depth,
            # which meant paying to keep alive inventory the configuration had
            # explicitly stopped carrying. The incoherence shows on acquisition:
            # a depth with a target of zero is NOT replaced when handed out, so
            # the pool was maintaining something it had already decided not to
            # keep.
            #
            # It is not a small amount. A depth-20 batch costs eight times a
            # depth-17 one, so two unwanted mediums cost more per day than the
            # entire intended pool.
            #
            # Batches at a dropped depth are left to expire. They stay usable
            # until they do, and sync removes them once they are gone, so
            # nothing is thrown away — the pool simply stops paying for them.
            if not self._last_sync_ok:
                return self._skip_replenishment(results)
            min_ttl_seconds = settings.STAMP_POOL_MIN_TTL_HOURS * 3600
            wanted_depths = set(reserve_config.keys())
            with self._lock:
                stamps_to_topup = [
                    s for s in self._pool.values()
                    if s.status == PoolStampStatus.AVAILABLE and s.depth in wanted_depths
                ]
                retired = [
                    s for s in self._pool.values()
                    if s.status == PoolStampStatus.AVAILABLE and s.depth not in wanted_depths
                ]
            for s in retired:
                logger.info(
                    "Not topping up %s... (depth=%d): that depth is no longer in the "
                    "reserve config, so it will not be replaced when used. Letting it expire.",
                    s.batch_id[:16], s.depth,
                )

            results["topup_debug"] = []
            for stamp in stamps_to_topup:
                # Get current TTL from Bee node
                current_ttl = await self._get_stamp_ttl(stamp.batch_id)
                debug = {
                    "stamp": stamp.batch_id[:16],
                    "ttl_seconds": current_ttl,
                    "ttl_hours": round(current_ttl / 3600, 1) if current_ttl else None,
                    "threshold_hours": settings.STAMP_POOL_MIN_TTL_HOURS,
                    "needs_topup": current_ttl is not None and current_ttl < min_ttl_seconds,
                }
                if current_ttl is not None and current_ttl < min_ttl_seconds:
                    if not self._last_sync_ok:
                        return self._skip_replenishment(results)
                    try:
                        outcome = await self._topup_stamp(stamp.batch_id)
                        if outcome is True:
                            results["stamps_topped_up"] += 1
                            debug["result"] = "topped_up"
                        elif outcome is None and self._needs_reconcile:
                            debug["result"] = ("skipped: pool spending paused, the pool state "
                                               "file could not be read")
                        elif outcome is None:
                            debug["result"] = "skipped: batch reserved or no longer in the pool"
                        else:
                            debug["result"] = "refused by a spending ceiling"
                    except Exception as e:
                        logger.error(f"Failed to top up stamp {stamp.batch_id[:16]}...: {e}")
                        results["errors"].append(
                            f"Failed to top up stamp {stamp.batch_id[:16]}...: "
                            f"{error_cause(e)}; see the gateway log for details.")
                        debug["result"] = f"error: {e}"
                elif current_ttl is None:
                    debug["result"] = "skipped: ttl_lookup_returned_none"
                else:
                    debug["result"] = "skipped: above_threshold"
                results["topup_debug"].append(debug)

            results["errors"].extend(self._refusals)
            self._refusals.clear()
            self._errors = results["errors"]

        except Exception as e:
            logger.error(f"Pool check failed: {e}")
            error_msg = f"Pool check failed: {error_cause(e)}; see the gateway log for details."
            results["errors"].append(error_msg)
            self._errors = [error_msg]

        return results

    def _spend_budget_remaining(self) -> int:
        """How many more BZZ-spending operations the pool may make this hour.

        A hard ceiling across every depth, every kind of spend and every caller,
        not a per-cycle limit. A staging node once bought 82 batches against a
        target of 5, spending about 8.9 BZZ. One mechanism was found and fixed; it
        accounts for five per restart, not seventy, and the rest was never
        identified (#271).

        The point of a ceiling is that it does not need the cause. Whatever the
        defect — a replenish path that miscounts, a restart loop, an external
        caller draining the pool faster than it refills — it cannot spend past
        this. Correctness bugs become expensive without one; with it they become
        merely wrong.

        This counts BOTH buying a batch and topping one up, against one shared
        limit. Topping up was previously uncounted (#334): it calls
        extend_postage_stamp directly, which spends BZZ exactly as buying does,
        so it was the only path in the gateway with no bound. Separate limits per
        kind of spend would also be wrong — the combined spend would then be
        twice the configured ceiling.

        The name says "spend" rather than "purchase" deliberately. The previous
        name is how the gap survived review: a reader sees _record_purchase, sees
        that top-up is not a purchase, and concludes correctly that it is not
        counted, without that reading the alarm it should.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        with self._lock:
            self._spend_times = [t for t in self._spend_times if t > cutoff]
            return max(0, settings.STAMP_POOL_MAX_PURCHASES_PER_HOUR - len(self._spend_times))

    def _reserve_spend_slot(self) -> Optional[datetime]:
        """Take one slot of the hourly ceiling now, before the Bee call (#363).

        Checking the remaining count and recording the spend only after Bee
        returned let a scheduled check and an immediate replenishment both pass
        the check. Returns the slot to release, or None if the ceiling is full.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        with self._lock:
            self._spend_times = [t for t in self._spend_times if t > cutoff]
            if len(self._spend_times) >= settings.STAMP_POOL_MAX_PURCHASES_PER_HOUR:
                return None
            slot = datetime.now(timezone.utc)
            self._spend_times.append(slot)
            self._persist_spend_times_locked()
            return slot

    def _release_spend_slot(self, slot: Optional[datetime]) -> None:
        if slot is None:
            return
        with self._lock:
            try:
                self._spend_times.remove(slot)
            except ValueError:
                pass
            else:
                self._persist_spend_times_locked()

    def _persist_spend_times_locked(self) -> None:
        """Write the spend record; call with self._lock held, after ANY change
        to self._spend_times (a merge that adds reserve/release paths must call
        it too, or the ceiling stops surviving restarts)."""
        try:
            atomic_write_json(self._spend_times_path(), [t.isoformat() for t in self._spend_times])
        except Exception as e:
            logger.error(f"Could not persist the pool's hourly spend record: {e}")

    # The hourly ceiling is persisted (#380). Kept in memory only, every deploy
    # or crash-restart granted a fresh hour of spending, and a restart loop is
    # one of the ways the ceiling exists to bound.
    #
    # #363 replaced record-after-the-call with reserve-before-it, so there are now
    # two places that mutate _spend_times rather than one: the reservation above
    # and the release below. Both persist. The docstring above asked for exactly
    # this, and it is the whole merge: taking either side alone leaves the ceiling
    # either unpersisted or unreserved.

    # ── Purchases Bee has not listed yet ──────────────────────────────────

    def _pending_path(self) -> str:
        base, _ = os.path.splitext(self._get_state_file_path())
        return f"{base}_pending.json"

    def _load_pending(self) -> Dict[str, Dict]:
        """Read the pending-purchase record. Missing or unreadable -> empty.

        An unreadable record only loses the grace period: an unlisted batch is
        then treated as gone, as before the record existed. An unparseable
        file is copied to <path>.corrupt-<ts> once (#440) before the next
        write replaces it. A malformed entry is dropped with an ERROR. A
        timestamp without a time zone is read as UTC, and one in the future
        (clock skew) is clamped to now, so the hold stays bounded by
        STAMP_POOL_UNLISTED_GRACE_SECONDS.
        """
        path = self._pending_path()
        try:
            with open(path) as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raise ValueError(f"expected a JSON object, got {type(raw).__name__}")
        except FileNotFoundError:
            return {}
        except Exception as e:
            kept = keep_corrupt_copy(path)
            logger.error(
                f"Pool pending-purchase record {path} could not be read ({e}); {kept}. "
                "Starting with an empty record, so a purchase Bee has not listed yet "
                "will not be held."
            )
            return {}
        now = datetime.now(timezone.utc)
        out = {}
        for batch_id, info in raw.items():
            try:
                bought = datetime.fromisoformat(info["purchased_at"])
                if bought.tzinfo is None:
                    bought = bought.replace(tzinfo=timezone.utc)
                if bought > now:
                    logger.warning(f"Pending pool purchase {str(batch_id)[:16]}... has a purchase time "
                                   f"in the future ({bought.isoformat()}); using now")
                    bought = now
                entry = dict(info)
                entry["purchased_at"] = bought.isoformat()
                entry["depth"] = int(info["depth"])
                entry["amount"] = int(info["amount"])
                entry["ttl"] = int(info["ttl"])
                out[str(batch_id)] = entry
            except Exception as e:
                logger.error(f"Dropping malformed pending pool purchase {str(batch_id)[:16]}... "
                             f"in {path}: {e}")
        return out

    def _persist_pending_locked(self) -> None:
        """Write the pending-purchase record; call with self._lock held."""
        try:
            atomic_write_json(self._pending_path(), self._pending)
        except Exception as e:
            logger.error(f"Could not persist the pool's pending-purchase record: {e}")

    def _pending_purchased_at(self, batch_id: str) -> datetime:
        return datetime.fromisoformat(self._pending[batch_id]["purchased_at"])

    def _pending_age(self, batch_id: str, now: datetime) -> int:
        return int((now - self._pending_purchased_at(batch_id)).total_seconds())

    def _pending_within_grace(self, batch_id: str, now: datetime) -> bool:
        """Whether an unlisted batch is a recent purchase to hold rather than drop."""
        if batch_id not in self._pending:
            return False
        return self._pending_age(batch_id, now) < settings.STAMP_POOL_UNLISTED_GRACE_SECONDS

    def _forget_pending_locked(self, batch_id: str) -> None:
        """Drop a pending entry for a batch leaving the pool; call with the lock held.

        A sold batch must not stay pending: sync evaluates pending ids even if
        the state file lacks them, and would put it back up for sale while its
        ownership record still says "pool".
        """
        if self._pending.pop(batch_id, None) is not None:
            self._persist_pending_locked()

    def _clear_listed_pending(self, batch_ids: Set[str], file_lists_pool: bool) -> None:
        """Clear pending entries for batches Bee lists now.

        A batch the pool still holds keeps its entry until the state file is
        known to list it (`file_lists_pool`: a save just succeeded, or the
        file already matched the pool). Otherwise a failed write followed by a
        restart lost the batch: no longer pending, not in the file, while its
        ownership record still said "pool". A batch no longer in the pool
        (dropped as expired or unusable) is cleared either way.
        """
        with self._lock:
            done = {b for b in batch_ids
                    if b in self._pending and (file_lists_pool or b not in self._pool)}
            if not done:
                return
            for batch_id in done:
                self._pending.pop(batch_id, None)
            self._persist_pending_locked()

    def _drop_unlisted_pending_locked(self, batch_id: str) -> None:
        """Give up on a purchase Bee never listed within the grace period."""
        logger.warning(
            f"Pool purchase {batch_id[:16]}... is still not listed by Bee "
            f"{settings.STAMP_POOL_UNLISTED_GRACE_SECONDS}s after it was bought; "
            "removing it from the pool and its ownership record"
        )
        self._pending.pop(batch_id, None)
        self._persist_pending_locked()
        self._pool.pop(batch_id, None)
        try:
            stamp_ownership_manager.remove_stamp(batch_id)
        except Exception as e:
            logger.error(f"Could not remove the ownership record of {batch_id[:16]}...: {e}")

    def _warn_overdue_held(self, now: datetime) -> None:
        """Warn, at most once an hour, about batches held past the grace period.

        A batch Bee keeps listing as not usable, with no other reason, is held
        for as long as Bee says so; this makes a long hold visible.
        """
        grace = settings.STAMP_POOL_UNLISTED_GRACE_SECONDS
        with self._lock:
            overdue = sorted(b for b, s in self._pool.items()
                             if s.status == PoolStampStatus.AVAILABLE and not s.usable
                             and (now - s.created_at).total_seconds() > grace)
        if not overdue:
            return
        last = self._overdue_held_logged_at
        if last is not None and (now - last).total_seconds() < _OVERDUE_HELD_LOG_SECONDS:
            return
        self._overdue_held_logged_at = now
        logger.warning(
            f"{len(overdue)} pool batch(es) held for more than {grace}s, still not usable on Bee: "
            + ", ".join(f"{b[:16]}..." for b in overdue)
        )

    def _held_ids(self) -> frozenset:
        """Ids of batches held for the pool but not usable yet."""
        with self._lock:
            return frozenset(b for b, s in self._pool.items()
                             if s.status == PoolStampStatus.AVAILABLE and not s.usable)

    async def _recheck_held(self) -> bool:
        """Re-run the TTL check early so a held batch is offered once usable.

        Skipped while a full maintenance run is in progress (it runs the same
        check), and until a sync has read the node. Uses its own flag: a full
        run arriving meanwhile waits for it and then runs (check_and_replenish).
        Returns whether the check ran.
        """
        if self._replenishing or self._rechecking or not self._last_sync_ok:
            return False
        self._rechecking = True
        self._recheck_done = asyncio.Event()
        try:
            await self._update_stamp_ttls()
        finally:
            self._rechecking = False
            self._recheck_done.set()
        return True

    def _spend_times_path(self) -> str:
        base, _ = os.path.splitext(self._get_state_file_path())
        return f"{base}_spend_times.json"

    def _load_spend_times(self) -> List[datetime]:
        path = self._spend_times_path()
        try:
            with open(path) as f:
                raw = json.load(f)
            cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
            return [t for t in (datetime.fromisoformat(x) for x in raw) if t > cutoff]
        except FileNotFoundError:
            return []
        except Exception as e:
            # Unknown spending in the last hour: assume the ceiling was reached
            # rather than grant a fresh one. It clears on its own within the hour.
            logger.error(f"Unreadable pool spend record {path} ({e}); pausing pool spending for an hour")
            now = datetime.now(timezone.utc)
            self._spend_times_unreadable_until = now + timedelta(hours=1)
            return [now] * settings.STAMP_POOL_MAX_PURCHASES_PER_HOUR

    def _reserve_gateway_spend(self, cost_bzz: float, what: str, operation: str):
        """Charge the gateway-wide daily ceiling for a pool spend (#363).

        Returns the hold, or None (and records why) if the ceiling is reached.
        """
        from app.services.spend_budget import spend_budget_tracker
        from app.services.metrics import stamp_spend_refusals_total
        hold, info = spend_budget_tracker.reserve_gateway(cost_bzz)
        if hold is None:
            if info.get("state_unreadable"):
                msg = (f"Refusing to {what}: the gateway's spend record is unreadable (#378); "
                       f"spending is paused until it is restored or until {info['resets_at']}.")
            else:
                msg = (f"Refusing to {what}: the gateway's daily spending ceiling "
                       f"({info['daily_budget_bzz']} BZZ) is reached; it resets at {info['resets_at']}.")
            logger.error(msg)
            self._errors.append(msg)
            self._refusals.append(msg)
            stamp_spend_refusals_total.labels(operation=operation, limit="gateway_daily").inc()
        return hold

    def _release_if_unspent(self, slot, hold, exc: BaseException) -> None:
        """Give back the hourly slot and the ceiling hold only if nothing was spent."""
        from app.services.spend_budget import spend_budget_tracker, spend_certainly_did_not_happen
        if spend_certainly_did_not_happen(exc):
            self._release_spend_slot(slot)
            spend_budget_tracker.release_hold(hold)
        else:
            from app.services.metrics import gateway_spend_uncertain_bzz_total
            cost = sum(c for _, c in getattr(hold, "charges", []))
            gateway_spend_uncertain_bzz_total.labels(operation="pool").inc(cost)
            logger.warning(f"Pool spend failed with {type(exc).__name__}; the outcome is uncertain, "
                           "so it stays counted against the hourly and daily ceilings")

    async def _purchase_stamp(self, depth: int, max_retries: int = 3) -> Optional[str]:
        """Purchase a new stamp for the pool. Retries on 429 rate limiting.

        Refuses once the hourly ceiling is reached. The check lives here rather
        than in the replenish loop so that every path is covered — the scheduled
        check, the immediate replenishment after an acquire, and anything added
        later that forgets to ask.

        Returns None without spending if pool spending is paused for an
        unreadable state file when Bee would be asked to buy (#547), checked
        before every attempt including 429 retries.
        """
        slot = self._reserve_spend_slot()
        if slot is None:
            msg = (
                f"Refusing to buy a depth-{depth} batch: the pool has already "
                f"bought {settings.STAMP_POOL_MAX_PURCHASES_PER_HOUR} in the last "
                "hour, which is the configured ceiling. Something is wrong — a "
                "healthy pool does not need this many. Raise "
                "STAMP_POOL_MAX_PURCHASES_PER_HOUR only after understanding why."
            )
            logger.error(msg)
            self._errors.append(msg)
            return None

        batch_id = None
        hold = None
        try:
            # Get current price (Bee API returns currentPrice as a string)
            chainstate = await swarm_api.get_chainstate()
            current_price = int(chainstate.get("currentPrice", 0))

            # Calculate amount for configured duration + 1 hour buffer
            # The extra hour ensures the stamp meets minimum TTL requirements
            duration_hours = settings.STAMP_POOL_DEFAULT_DURATION_HOURS + 1
            amount = swarm_api.calculate_stamp_amount(duration_hours, current_price)

            # Generate pool label
            label = f"pool-{depth}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"

            logger.info(f"Purchasing stamp for pool: depth={depth}, amount={amount}, duration={duration_hours}h")

            cost_bzz = swarm_api.plur_to_bzz(swarm_api.calculate_stamp_total_cost(amount, depth))
            hold = self._reserve_gateway_spend(cost_bzz, f"buy a depth-{depth} pool batch", "pool purchase")
            if hold is None:
                self._release_spend_slot(slot)
                return None

            # Purchase the stamp with retry on 429. The hourly slot and the
            # ceiling hold were taken before the call; they are kept once Bee
            # accepts it, before waiting for it to become usable, because the
            # money is spent at that point.
            for attempt in range(max_retries):
                if self._spending_paused_for_state_file(f"buy a depth-{depth} pool batch"):
                    # Nothing spent: give back the slot and the hold and return
                    # None. Logged only, not added to _errors: the pause is
                    # already reported there by the sync or save that set it,
                    # and a maintenance run reports its skip.
                    from app.services.spend_budget import spend_budget_tracker
                    self._release_spend_slot(slot)
                    spend_budget_tracker.release_hold(hold)
                    return None
                try:
                    batch_id = await swarm_api.purchase_postage_stamp(amount, depth, label)
                    bought_at = datetime.now(timezone.utc)
                    break
                except Exception as e:
                    status_code = getattr(getattr(e, "response", None), "status_code", None)
                    if status_code == 429 and attempt < max_retries - 1:
                        wait_time = 15 * (attempt + 1)
                        logger.warning(f"Bee node rate limited (429), retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})")
                        await asyncio.sleep(wait_time)
                    else:
                        raise

            if not batch_id:
                # Only reachable with max_retries <= 0: nothing was attempted.
                from app.services.spend_budget import spend_budget_tracker
                self._release_spend_slot(slot)
                spend_budget_tracker.release_hold(hold)
                return None

            # The batch is paid for, so it enters the pool and its state file
            # whatever happens next. The waits below only refine the TTL.
            # Before, a usable batch missing from the listing (or a listing
            # that raised) was never added: the pool lost a batch it had paid
            # for, and a later run bought another to fill the gap.
            ttl = duration_hours * 3600
            try:
                # Wait for stamp to become usable (up to 90 seconds)
                usable = await self._wait_for_stamp_usable(batch_id, timeout=90)
                if usable:
                    try:
                        stamps = await swarm_api.get_all_stamps_processed()
                        stamp_data = next((s for s in stamps if s.get("batchID") == batch_id), None)
                    except Exception as e:
                        stamp_data = None
                        logger.warning(f"Could not list stamps after buying {batch_id[:16]}...: {e}")
                    if stamp_data:
                        ttl = coerce_int(stamp_data.get("batchTTL"), ttl)
                    else:
                        logger.warning(
                            f"Stamp {batch_id[:16]}... is usable but missing from the stamp listing; "
                            f"adding it with the purchased TTL ({ttl}s)"
                        )
                else:
                    logger.warning(f"Stamp {batch_id[:16]}... purchased but not yet usable")
                    # Add anyway, held but not for sale; the TTL check marks
                    # it usable once Bee does.
            except asyncio.CancelledError:
                # Cancelled (e.g. at shutdown) after Bee accepted the purchase.
                # Add the batch as the not-usable path does, synchronously so
                # no further await can be interrupted, then let the
                # cancellation through.
                logger.warning(
                    f"Purchase of {batch_id[:16]}... was cancelled after Bee accepted it; "
                    f"adding it to the pool with the purchased TTL ({ttl}s)"
                )
                self.add_stamp_to_pool(
                    batch_id=batch_id,
                    depth=depth,
                    amount=amount,
                    ttl=duration_hours * 3600,
                    label=label,
                    usable=False,
                    purchased_at=bought_at,
                )
                raise
            self.add_stamp_to_pool(
                batch_id=batch_id,
                depth=depth,
                amount=amount,
                ttl=ttl,
                label=label,
                usable=usable,
                purchased_at=bought_at,
            )
            return batch_id

        except BaseException as e:
            # Surface Bee's own message. httpx's str(e) is only the status line
            # ("Client error '400 Bad Request' for url ..."), so the actual cause
            # — "out of funds", "insufficient amount for 24h minimum validity" —
            # was discarded and had to be obtained by calling Bee by hand.
            if not batch_id:
                # Nothing confirmed bought. Give the slot and the hold back only
                # if the purchase certainly did not happen; a price lookup that
                # failed before the call always counts as that.
                if hold is None:
                    self._release_spend_slot(slot)
                else:
                    self._release_if_unspent(slot, hold, e)
            if isinstance(e, Exception):
                detail = _bee_error_message(e)
                logger.error(
                    f"Failed to purchase stamp for pool (depth={depth}): {e}"
                    + (f" — Bee said: {detail}" if detail else "")
                )
            raise

    async def _wait_for_stamp_usable(self, batch_id: str, timeout: int = 90) -> bool:
        """Wait for a stamp to become usable."""
        start = datetime.now(timezone.utc)
        while (datetime.now(timezone.utc) - start).total_seconds() < timeout:
            try:
                stamps = await swarm_api.get_all_stamps_processed()
                stamp = next((s for s in stamps if s.get("batchID") == batch_id), None)
                if stamp and stamp.get("usable"):
                    return True
            except Exception as e:
                logger.warning(f"Error checking stamp usability: {e}")

            await asyncio.sleep(5)  # Check every 5 seconds

        return False

    async def _get_stamp_ttl(self, batch_id: str) -> Optional[int]:
        """Get current TTL for a stamp via direct Bee API lookup."""
        try:
            from app.services.http_client import get_client
            from urllib.parse import urljoin
            api_url = urljoin(str(settings.SWARM_BEE_API_URL), f"stamps/{batch_id}")
            client = get_client()
            response = await client.get(api_url, timeout=10)
            if response.status_code == 200:
                data = response.json()
                return data.get("batchTTL", 0)
            else:
                logger.warning(f"Stamp TTL lookup failed for {batch_id[:16]}...: HTTP {response.status_code}")
        except Exception as e:
            logger.warning(f"Error getting stamp TTL for {batch_id[:16]}...: {e}")
        return None

    async def _update_stamp_ttls(self):
        """Update TTL information for all pool stamps.

        Removes batches that are expired, unusable for a lasting reason, or no
        longer listed by Bee. Holds (keeps, not for sale) a batch Bee lists as
        not usable yet, and a purchase Bee has not listed yet within
        STAMP_POOL_UNLISTED_GRACE_SECONDS. Batches added while the listing was
        being fetched are left for the next check: the listing predates them.
        """
        try:
            with self._lock:
                before_fetch = set(self._pool)
            stamps = await swarm_api.get_all_stamps_processed()
            stamp_map = {s.get("batchID"): s for s in stamps}

            with self._lock:
                to_remove = []
                held = []
                now = datetime.now(timezone.utc)
                pending_changed = False
                listed_pending: Set[str] = set()
                for batch_id, pool_stamp in self._pool.items():
                    # A reserved batch belongs to an acquire whose payment is
                    # settling; that request decides what happens to it.
                    if pool_stamp.status == PoolStampStatus.RESERVED:
                        continue
                    if batch_id not in before_fetch:
                        # Added (bought) while the listing was fetched; its
                        # absence from that listing says nothing.
                        continue
                    stamp_data = stamp_map.get(batch_id)
                    if stamp_data and batch_id in self._pending:
                        # Cleared below, once the state file lists the batch.
                        listed_pending.add(batch_id)
                    if not stamp_data and self._pending_within_grace(batch_id, now):
                        pool_stamp.usable = False
                        held.append(f"{batch_id[:16]}... (not listed by Bee yet, bought "
                                    f"{self._pending_age(batch_id, now)}s ago)")
                    elif stamp_data:
                        current_ttl = coerce_int(stamp_data.get("batchTTL"), 0)
                        usable = bool(stamp_data.get("usable", False))

                        if usable and current_ttl > 0:
                            if not pool_stamp.usable:
                                logger.info(f"Pool stamp {batch_id[:16]}... is usable now")
                            pool_stamp.usable = True
                        elif _not_yet_usable(stamp_data):
                            # Still in Bee's usability delay: keep it, and
                            # keep it out of acquire until Bee says usable.
                            pool_stamp.usable = False
                            held.append(f"{batch_id[:16]}... (listed, not usable yet)")
                        else:
                            # Remove expired or unusable stamps
                            logger.warning(f"Removing expired/unusable stamp {batch_id[:16]}... from pool")
                            to_remove.append(batch_id)
                    else:
                        # Stamp no longer exists on node (or a purchase Bee
                        # never listed within the grace period)
                        logger.warning(f"Stamp {batch_id[:16]}... no longer found on node, removing from pool")
                        to_remove.append(batch_id)

                for batch_id in to_remove:
                    del self._pool[batch_id]
                    stamp_ownership_manager.remove_stamp(batch_id)
                    if self._pending.pop(batch_id, None) is not None:
                        pending_changed = True
                if pending_changed:
                    self._persist_pending_locked()

                saved = False
                if to_remove or listed_pending:
                    saved = self._save_state(removed=set(to_remove) or None)

            if listed_pending:
                self._clear_listed_pending(listed_pending - set(to_remove), file_lists_pool=saved)

            if held:
                logger.info(f"Holding {len(held)} pool batch(es) until Bee lists them usable: "
                            + ", ".join(held))
                self._warn_overdue_held(now)

        except Exception as e:
            logger.warning(f"Error updating stamp TTLs: {e}")

    async def _topup_stamp(self, batch_id: str) -> Optional[bool]:
        """Top up a stamp with additional TTL.

        Returns True when topped up, False when a spending ceiling refused it,
        and None when it was skipped because the batch is no longer an
        AVAILABLE pool batch (reserved for an acquire, or gone; #475) or
        because pool spending is paused for an unreadable state file (#547).

        Subject to the same hourly ceiling as buying a batch (#334). Extending
        spends BZZ, so leaving it uncounted made this the only spending path in
        the gateway with no bound — a scheduling or TTL-arithmetic error here
        could extend every batch on every pass with nothing to stop it.

        Checked before the price lookup so a refusal costs no Bee calls, and
        recorded only once Bee has accepted the extension, because that is when
        the money is spent.
        """
        slot = self._reserve_spend_slot()
        if slot is None:
            msg = (
                f"Refusing to top up batch {batch_id[:16]}...: the pool has "
                f"already made {settings.STAMP_POOL_MAX_PURCHASES_PER_HOUR} "
                "BZZ-spending operations in the last hour, which is the "
                "configured ceiling. Something is wrong — a healthy pool does "
                "not need this many. Raise STAMP_POOL_MAX_PURCHASES_PER_HOUR "
                "only after understanding why."
            )
            logger.error(msg)
            self._errors.append(msg)
            self._refusals.append(msg)
            return False

        try:
            # Get current price (Bee API returns currentPrice as a string)
            chainstate = await swarm_api.get_chainstate()
            current_price = int(chainstate.get("currentPrice", 0))

            # Calculate amount for configured top-up duration
            topup_hours = settings.STAMP_POOL_TOPUP_HOURS
            amount = swarm_api.calculate_stamp_amount(topup_hours, current_price)

            logger.info(f"Topping up stamp {batch_id[:16]}... with {topup_hours}h ({amount} PLUR)")

            # Checked here, under the lock and after the last await before the
            # spend, because the top-up list was built from AVAILABLE batches
            # before the TTL and price lookups awaited (#475). Since #439 a
            # reservation keeps the batch in the pool while its payment settles,
            # so being in the pool is not enough: a RESERVED batch is about to
            # be sold. Nothing below awaits until the extend call. In the same
            # locked step the batch is marked as being topped up, so no acquire
            # can reserve or sell it while the extend call is in flight (#540).
            with self._lock:
                held = self._pool.get(batch_id)
                status = held.status if held is not None else None
                depth = held.depth if held is not None else None
                # A save during the price lookup may have paused the pool (#547).
                paused = self._spending_paused_for_state_file(f"top up batch {batch_id[:16]}...")
                if status == PoolStampStatus.AVAILABLE and not paused:
                    self._topup_in_flight.add(batch_id)
            if paused:
                # A skip, as below: nothing spent, slot given back.
                self._release_spend_slot(slot)
                return None
            if status != PoolStampStatus.AVAILABLE:
                # A skip, not a failure: the batch was reserved or handed out
                # after the list was built. Not recorded as an error or refusal.
                reason = "not in the pool" if status is None else f"status is {status.value}"
                logger.info(f"Not topping up {batch_id[:16]}...: {reason}; it is leaving the pool")
                self._release_spend_slot(slot)
                return None
            try:
                cost_bzz = swarm_api.plur_to_bzz(swarm_api.calculate_stamp_total_cost(amount, depth))
                hold = self._reserve_gateway_spend(cost_bzz, f"top up batch {batch_id[:16]}...", "pool top-up")
                if hold is None:
                    self._release_spend_slot(slot)
                    slot = None  # handled
                    return False
                try:
                    await swarm_api.extend_postage_stamp(batch_id, amount)
                except BaseException as e:
                    self._release_if_unspent(slot, hold, e)
                    slot = None  # handled
                    raise
                return True
            finally:
                # Every exit, including a refusal, an error and a cancellation:
                # the batch can be acquired again.
                with self._lock:
                    self._topup_in_flight.discard(batch_id)

        except BaseException as e:
            if slot is not None:
                # Failed before the top-up call (e.g. the price lookup, or a
                # cancellation there): nothing was spent.
                self._release_spend_slot(slot)
            logger.error(f"Failed to top up stamp {batch_id[:16]}...: {e}")
            raise

    async def start_background_task(self):
        """Start the background monitoring task."""
        if self._running:
            return

        if not self.is_enabled:
            logger.info("Stamp pool is disabled, not starting background task")
            return

        self._running = True
        self._task = asyncio.create_task(self._background_loop())
        logger.info("Started stamp pool background task")

    async def stop_background_task(self):
        """Stop the background monitoring task."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Stopped stamp pool background task")

    async def _background_loop(self):
        """Background loop that periodically checks and replenishes the pool."""
        # Initial check
        await self.check_and_replenish()

        while self._running:
            try:
                # Wake every STAMP_POOL_HELD_RECHECK_SECONDS. With nothing
                # held a wake costs nothing. While a batch is held (bought,
                # not usable yet), re-run the TTL check so the batch is offered
                # soon after Bee lists it usable, not at the next full check.
                # While the same batches stay held, the time between re-checks
                # doubles up to the full interval, so a batch Bee keeps listing
                # as not usable is not polled every 30 s indefinitely. A newly
                # held batch resets it: it is re-checked at the next wake.
                base = settings.STAMP_POOL_HELD_RECHECK_SECONDS
                interval = settings.STAMP_POOL_CHECK_INTERVAL_SECONDS
                due = time.monotonic() + interval
                while self._running:
                    remaining = due - time.monotonic()
                    if remaining <= 0:
                        break
                    nap = min(remaining, base)
                    await asyncio.sleep(nap)
                    self._since_recheck += nap
                    held = self._held_ids()
                    if not held:
                        self._recheck_step = base
                        self._recheck_held_ids = frozenset()
                        continue
                    grew = not held <= self._recheck_held_ids
                    if grew:
                        self._recheck_step = base
                    elif self._since_recheck < self._recheck_step:
                        continue
                    if due - time.monotonic() <= 0:
                        break
                    if not await self._recheck_held():
                        # Did not run (a full run is in progress): no backoff.
                        continue
                    self._since_recheck = 0.0
                    still_held = self._held_ids()
                    if still_held and not grew and still_held == held == self._recheck_held_ids:
                        self._recheck_step = min(self._recheck_step * 2, interval)
                    elif still_held != held:
                        self._recheck_step = base
                    self._recheck_held_ids = still_held
                if not self._running:
                    break
                await self.check_and_replenish()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in stamp pool background task: {e}")
                # Continue running despite errors


# Global singleton instance
stamp_pool_manager = StampPoolManager()
