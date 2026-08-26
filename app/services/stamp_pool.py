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
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Dict, List, Optional, Set
from threading import Lock

from app.core.atomic_io import atomic_write_json
from app.core.config import settings
from app.services import swarm_api
from app.services.swarm_api import coerce_int
from app.services.stamp_ownership import stamp_ownership_manager

logger = logging.getLogger(__name__)


def _bee_error_message(exc) -> Optional[str]:
    """Extract Bee's own error text from a failed request, if there is one.

    Bee reports refusals as JSON like {"code":400,"message":"out of funds"}.
    That message names the problem exactly; the exception string does not.
    """
    response = getattr(exc, "response", None)
    if response is None:
        return None
    try:
        body = response.json()
        if isinstance(body, dict):
            message = body.get("message") or body.get("detail")
            if message:
                return str(message)[:200]
    except Exception:
        pass
    text = (getattr(response, "text", "") or "").strip()
    return text[:200] or None



class PoolStampStatus(str, Enum):
    """Status of a stamp in the pool."""
    AVAILABLE = "available"  # Ready to be released
    RESERVED = "reserved"    # Temporarily held (e.g., during release)
    RELEASED = "released"    # Released to client, no longer managed


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
        self._task: Optional[asyncio.Task] = None
        self._last_check: Optional[datetime] = None
        self._errors: List[str] = []
        self._pending_replenishments: Dict[int, int] = {}  # depth -> count of pending purchases
        self._state_file = state_file  # Allow override for testing
        # False until a sync has actually read the node. Starts False so the very
        # first check cannot purchase against an unverified (empty) pool.
        self._last_sync_ok: bool = False
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

    def _save_state(self, extra_ids: Optional[Set[str]] = None):
        """Persist current pool batch IDs to state file.

        `extra_ids` are batch IDs to keep in state that are not in the pool —
        records whose data could not be read this cycle. They are still ours and
        may parse fine next time, so writing only the pool would silently drop
        them.
        """
        state_file = self._get_state_file_path()
        try:
            batch_ids = set(self._pool.keys())
            if extra_ids:
                batch_ids |= set(extra_ids)
            batch_ids = sorted(batch_ids)
            atomic_write_json(state_file, batch_ids)
            logger.debug(f"Saved pool state: {len(batch_ids)} stamps to {state_file}")
        except Exception as e:
            logger.error(f"Failed to save pool state to {state_file}: {e}")

    def _load_state(self) -> Set[str]:
        """Load pool batch IDs from state file.

        Returns:
            Set of batch IDs that were previously in the pool.
            Returns empty set if file is missing or corrupt.
        """
        state_file = self._get_state_file_path()
        try:
            with open(state_file, 'r') as f:
                batch_ids = json.load(f)
            if isinstance(batch_ids, list):
                logger.info(f"Loaded pool state: {len(batch_ids)} stamps from {state_file}")
                return set(batch_ids)
            else:
                logger.warning(f"Invalid pool state format in {state_file}, treating as first run")
                return set()
        except FileNotFoundError:
            logger.info(f"No pool state file at {state_file}, treating as first run")
            return set()
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"Corrupt pool state file {state_file}: {e}, treating as first run")
            return set()
        except Exception as e:
            logger.warning(f"Error loading pool state from {state_file}: {e}, treating as first run")
            return set()

    def get_status(self) -> PoolStatus:
        """Get current pool status."""
        with self._lock:
            reserve_config = self.get_reserve_config()

            # Count available stamps by depth
            current_levels: Dict[int, int] = {}
            available_stamps: Dict[int, List[str]] = {}

            for batch_id, stamp in self._pool.items():
                if stamp.status == PoolStampStatus.AVAILABLE:
                    depth = stamp.depth
                    current_levels[depth] = current_levels.get(depth, 0) + 1
                    if depth not in available_stamps:
                        available_stamps[depth] = []
                    available_stamps[depth].append(batch_id)

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
                total_stamps=len([s for s in self._pool.values() if s.status == PoolStampStatus.AVAILABLE]),
                low_reserve_warning=low_reserve_warning,
                last_check=self._last_check,
                next_check=next_check,
                errors=self._errors.copy()
            )

    def get_available_stamp(self, depth: int) -> Optional[PoolStamp]:
        """
        Get an available stamp of the requested depth.

        Returns None if no stamp is available.
        Does NOT release the stamp - call release_stamp() after confirming.
        """
        with self._lock:
            for batch_id, stamp in self._pool.items():
                if stamp.depth == depth and stamp.status == PoolStampStatus.AVAILABLE:
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
                if s.status == PoolStampStatus.AVAILABLE and s.depth >= min_depth
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
            if not stamp or stamp.status != PoolStampStatus.AVAILABLE:
                return None

            stamp.status = PoolStampStatus.RELEASED
            stamp.released_at = datetime.now(timezone.utc)
            stamp.released_to = released_to

            logger.info(f"Released stamp {batch_id[:16]}... (depth={stamp.depth}) to {released_to or 'unknown'}")

            # Remove from pool (we no longer manage it)
            del self._pool[batch_id]

            self._save_state()
            return stamp

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

        reserve_config = self.get_reserve_config()
        target_count = reserve_config.get(depth, 0)

        if target_count == 0:
            # This depth is not configured for pooling
            return False

        # Count current available stamps for this depth
        with self._lock:
            current_count = len([
                s for s in self._pool.values()
                if s.depth == depth and s.status == PoolStampStatus.AVAILABLE
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
            logger.info(f"Immediate replenishment: starting purchase for depth {depth}")
            batch_id = await self._purchase_stamp(depth)
            if batch_id:
                logger.info(f"Immediate replenishment: successfully purchased stamp {batch_id[:16]}... for depth {depth}")
            else:
                logger.warning(f"Immediate replenishment: purchase returned no batch_id for depth {depth}")
        except Exception as e:
            logger.error(f"Immediate replenishment failed for depth {depth}: {e}")
            self._errors.append(f"Immediate replenishment failed (depth {depth}): {str(e)}")
        finally:
            # Remove from pending count
            with self._lock:
                current_pending = self._pending_replenishments.get(depth, 1)
                if current_pending <= 1:
                    self._pending_replenishments.pop(depth, None)
                else:
                    self._pending_replenishments[depth] = current_pending - 1

    def add_stamp_to_pool(self, batch_id: str, depth: int, amount: int, ttl: int, label: Optional[str] = None) -> PoolStamp:
        """
        Add a newly purchased stamp to the pool.

        Args:
            batch_id: The batch ID of the stamp
            depth: Stamp depth
            amount: Amount in PLUR
            ttl: TTL in seconds
            label: Optional label

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
                label=label or f"pool-{depth}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
            )
            self._pool[batch_id] = stamp
            logger.info(f"Added stamp {batch_id[:16]}... to pool (depth={depth})")
            self._save_state()
            return stamp

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
            known_ids = self._load_state()

            # First run: no state file means empty pool, let purchase logic fill it
            if not known_ids:
                logger.info("No known stamps in state file, pool will be filled by purchase logic")
                # A genuinely empty state is a reliable answer, not a failure.
                self._last_sync_ok = True
                return 0

            all_stamps = await swarm_api.get_all_stamps_processed()
            stamp_map = {s.get("batchID"): s for s in all_stamps}
            synced_count = 0
            valid_ids = set()
            # IDs we could not parse this cycle. They are kept in the state file
            # so a transient unreadable field does not permanently lose a stamp.
            unreadable_ids = set()

            with self._lock:
                for batch_id in known_ids:
                    # Skip if already in pool
                    if batch_id in self._pool:
                        valid_ids.add(batch_id)
                        continue

                    stamp_data = stamp_map.get(batch_id)
                    if not stamp_data:
                        # Stamp no longer exists on Bee node (expired/removed)
                        logger.info(f"Known stamp {batch_id[:16]}... no longer on Bee node, removing from state")
                        continue

                    usable = stamp_data.get("usable", False)
                    ttl = coerce_int(stamp_data.get("batchTTL"), 0)

                    if not usable or ttl <= 0:
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
                        label=label or f"synced-{depth}"
                    )
                    self._pool[batch_id] = stamp
                    valid_ids.add(batch_id)
                    synced_count += 1
                    logger.info(f"Synced known stamp {batch_id[:16]}... to pool (depth={depth}, ttl={ttl}s)")

            # Save cleaned state (stamps still valid, plus any we could not read
            # this cycle — those are not in the pool but must not be forgotten).
            if valid_ids != known_ids:
                self._save_state(extra_ids=unreadable_ids)

            self._last_sync_ok = True
            return synced_count

        except Exception as e:
            logger.error(f"Error syncing stamps from Bee node: {e}")
            self._errors.append(f"Sync error: {str(e)}")
            # The pool's contents are now UNKNOWN, not known-to-be-empty. Record
            # that so replenishment does not read the empty pool as a real deficit.
            self._last_sync_ok = False
            return 0

    async def check_and_replenish(self) -> Dict[str, any]:
        """
        Check pool levels and replenish if needed.

        This is the main maintenance function called by the background task.

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
                msg = ("Skipping replenishment: could not read stamp state from the "
                       "Bee node, so the pool's current contents are unknown. "
                       "Will retry on the next check.")
                logger.warning(msg)
                results["errors"].append(msg)
                results["skipped"] = True
                return results

            # Update TTL information for pool stamps
            await self._update_stamp_ttls()

            # Check levels for each depth
            for depth, target_count in reserve_config.items():
                current_count = len([
                    s for s in self._pool.values()
                    if s.depth == depth and s.status == PoolStampStatus.AVAILABLE
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
                            batch_id = await self._purchase_stamp(depth)
                            if batch_id:
                                results["stamps_purchased"] += 1
                                # Working again: forget the previous failure.
                                self._backoff.pop(depth, None)
                                self._backoff_count.pop(depth, None)
                        except Exception as e:
                            detail = _bee_error_message(e)
                            error_msg = f"Failed to purchase depth-{depth} stamp: {e}"
                            if detail:
                                error_msg += f" — Bee said: {detail}"
                            logger.error(error_msg)
                            results["errors"].append(error_msg)
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

            # Top up stamps with low TTL
            min_ttl_seconds = settings.STAMP_POOL_MIN_TTL_HOURS * 3600
            with self._lock:
                stamps_to_topup = [
                    s for s in self._pool.values()
                    if s.status == PoolStampStatus.AVAILABLE
                ]

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
                    try:
                        await self._topup_stamp(stamp.batch_id)
                        results["stamps_topped_up"] += 1
                        debug["result"] = "topped_up"
                    except Exception as e:
                        error_msg = f"Failed to top up stamp {stamp.batch_id[:16]}...: {e}"
                        logger.error(error_msg)
                        results["errors"].append(error_msg)
                        debug["result"] = f"error: {e}"
                elif current_ttl is None:
                    debug["result"] = "skipped: ttl_lookup_returned_none"
                else:
                    debug["result"] = "skipped: above_threshold"
                results["topup_debug"].append(debug)

            self._errors = results["errors"]

        except Exception as e:
            error_msg = f"Pool check failed: {e}"
            logger.error(error_msg)
            results["errors"].append(error_msg)
            self._errors = [error_msg]

        return results

    async def _purchase_stamp(self, depth: int, max_retries: int = 3) -> Optional[str]:
        """Purchase a new stamp for the pool. Retries on 429 rate limiting."""
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

            # Purchase the stamp with retry on 429
            batch_id = None
            for attempt in range(max_retries):
                try:
                    batch_id = await swarm_api.purchase_postage_stamp(amount, depth, label)
                    break
                except Exception as e:
                    if "429" in str(e) and attempt < max_retries - 1:
                        wait_time = 15 * (attempt + 1)
                        logger.warning(f"Bee node rate limited (429), retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})")
                        await asyncio.sleep(wait_time)
                    else:
                        raise

            if not batch_id:
                return None

            # Wait for stamp to become usable (up to 90 seconds)
            usable = await self._wait_for_stamp_usable(batch_id, timeout=90)

            if usable:
                # Get stamp info and add to pool
                stamps = await swarm_api.get_all_stamps_processed()
                stamp_data = next((s for s in stamps if s.get("batchID") == batch_id), None)
                if stamp_data:
                    self.add_stamp_to_pool(
                        batch_id=batch_id,
                        depth=depth,
                        amount=amount,
                        ttl=stamp_data.get("batchTTL", duration_hours * 3600),
                        label=label
                    )
                    return batch_id
            else:
                logger.warning(f"Stamp {batch_id[:16]}... purchased but not yet usable")
                # Add anyway, it will become usable soon
                self.add_stamp_to_pool(
                    batch_id=batch_id,
                    depth=depth,
                    amount=amount,
                    ttl=duration_hours * 3600,
                    label=label
                )
                return batch_id

        except Exception as e:
            # Surface Bee's own message. httpx's str(e) is only the status line
            # ("Client error '400 Bad Request' for url ..."), so the actual cause
            # — "out of funds", "insufficient amount for 24h minimum validity" —
            # was discarded and had to be obtained by calling Bee by hand.
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
        """Update TTL information for all pool stamps."""
        try:
            stamps = await swarm_api.get_all_stamps_processed()
            stamp_map = {s.get("batchID"): s for s in stamps}

            with self._lock:
                to_remove = []
                for batch_id, pool_stamp in self._pool.items():
                    stamp_data = stamp_map.get(batch_id)
                    if stamp_data:
                        # Update TTL
                        current_ttl = stamp_data.get("batchTTL", 0)
                        usable = stamp_data.get("usable", False)

                        # Remove expired or unusable stamps
                        if current_ttl <= 0 or not usable:
                            logger.warning(f"Removing expired/unusable stamp {batch_id[:16]}... from pool")
                            to_remove.append(batch_id)
                    else:
                        # Stamp no longer exists on node
                        logger.warning(f"Stamp {batch_id[:16]}... no longer found on node, removing from pool")
                        to_remove.append(batch_id)

                for batch_id in to_remove:
                    del self._pool[batch_id]
                    stamp_ownership_manager.remove_stamp(batch_id)

                if to_remove:
                    self._save_state()

        except Exception as e:
            logger.warning(f"Error updating stamp TTLs: {e}")

    async def _topup_stamp(self, batch_id: str):
        """Top up a stamp with additional TTL."""
        try:
            # Get current price (Bee API returns currentPrice as a string)
            chainstate = await swarm_api.get_chainstate()
            current_price = int(chainstate.get("currentPrice", 0))

            # Calculate amount for configured top-up duration
            topup_hours = settings.STAMP_POOL_TOPUP_HOURS
            amount = swarm_api.calculate_stamp_amount(topup_hours, current_price)

            logger.info(f"Topping up stamp {batch_id[:16]}... with {topup_hours}h ({amount} PLUR)")

            await swarm_api.extend_postage_stamp(batch_id, amount)

        except Exception as e:
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
                await asyncio.sleep(settings.STAMP_POOL_CHECK_INTERVAL_SECONDS)
                await self.check_and_replenish()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in stamp pool background task: {e}")
                # Continue running despite errors


# Global singleton instance
stamp_pool_manager = StampPoolManager()
