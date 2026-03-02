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
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional, Set
from threading import Lock

from app.core.config import settings
from app.services import swarm_api

logger = logging.getLogger(__name__)


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

    def _save_state(self):
        """Persist current pool batch IDs to state file."""
        state_file = self._get_state_file_path()
        try:
            state_dir = os.path.dirname(state_file)
            if state_dir:
                os.makedirs(state_dir, exist_ok=True)
            batch_ids = list(self._pool.keys())
            with open(state_file, 'w') as f:
                json.dump(batch_ids, f)
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
                return 0

            all_stamps = swarm_api.get_all_stamps_processed()
            stamp_map = {s.get("batchID"): s for s in all_stamps}
            synced_count = 0
            valid_ids = set()

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
                    ttl = stamp_data.get("batchTTL", 0)

                    if not usable or ttl <= 0:
                        logger.info(f"Known stamp {batch_id[:16]}... is expired/unusable, removing from state")
                        continue

                    # Re-import this known stamp
                    depth = stamp_data.get("depth")
                    amount = int(stamp_data.get("amount", 0))
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

            # Save cleaned state (only stamps that are still valid)
            if valid_ids != known_ids:
                self._save_state()

            return synced_count

        except Exception as e:
            logger.error(f"Error syncing stamps from Bee node: {e}")
            self._errors.append(f"Sync error: {str(e)}")
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
                if needed > 0:
                    logger.info(f"Pool depth {depth}: need {needed} stamps (have {current_count}, target {target_count})")
                    for _ in range(needed):
                        try:
                            batch_id = await self._purchase_stamp(depth)
                            if batch_id:
                                results["stamps_purchased"] += 1
                        except Exception as e:
                            error_msg = f"Failed to purchase depth-{depth} stamp: {e}"
                            logger.error(error_msg)
                            results["errors"].append(error_msg)

            # Top up stamps with low TTL
            min_ttl_seconds = settings.STAMP_POOL_MIN_TTL_HOURS * 3600
            with self._lock:
                stamps_to_topup = [
                    s for s in self._pool.values()
                    if s.status == PoolStampStatus.AVAILABLE
                ]

            for stamp in stamps_to_topup:
                # Get current TTL from Bee node
                current_ttl = await self._get_stamp_ttl(stamp.batch_id)
                if current_ttl is not None and current_ttl < min_ttl_seconds:
                    try:
                        await self._topup_stamp(stamp.batch_id)
                        results["stamps_topped_up"] += 1
                    except Exception as e:
                        error_msg = f"Failed to top up stamp {stamp.batch_id[:16]}...: {e}"
                        logger.error(error_msg)
                        results["errors"].append(error_msg)

            self._errors = results["errors"]

        except Exception as e:
            error_msg = f"Pool check failed: {e}"
            logger.error(error_msg)
            results["errors"].append(error_msg)
            self._errors = [error_msg]

        return results

    async def _purchase_stamp(self, depth: int) -> Optional[str]:
        """Purchase a new stamp for the pool."""
        try:
            # Get current price
            chainstate = swarm_api.get_chainstate()
            current_price = chainstate.get("currentPrice", 0)

            # Calculate amount for configured duration + 1 hour buffer
            # The extra hour ensures the stamp meets minimum TTL requirements
            duration_hours = settings.STAMP_POOL_DEFAULT_DURATION_HOURS + 1
            amount = swarm_api.calculate_stamp_amount(duration_hours, current_price)

            # Generate pool label
            label = f"pool-{depth}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"

            logger.info(f"Purchasing stamp for pool: depth={depth}, amount={amount}, duration={duration_hours}h")

            # Purchase the stamp
            batch_id = swarm_api.purchase_postage_stamp(amount, depth, label)

            # Wait for stamp to become usable (up to 90 seconds)
            usable = await self._wait_for_stamp_usable(batch_id, timeout=90)

            if usable:
                # Get stamp info and add to pool
                stamps = swarm_api.get_all_stamps_processed()
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
            logger.error(f"Failed to purchase stamp for pool (depth={depth}): {e}")
            raise

    async def _wait_for_stamp_usable(self, batch_id: str, timeout: int = 90) -> bool:
        """Wait for a stamp to become usable."""
        start = datetime.now(timezone.utc)
        while (datetime.now(timezone.utc) - start).total_seconds() < timeout:
            try:
                stamps = swarm_api.get_all_stamps_processed()
                stamp = next((s for s in stamps if s.get("batchID") == batch_id), None)
                if stamp and stamp.get("usable"):
                    return True
            except Exception as e:
                logger.warning(f"Error checking stamp usability: {e}")

            await asyncio.sleep(5)  # Check every 5 seconds

        return False

    async def _get_stamp_ttl(self, batch_id: str) -> Optional[int]:
        """Get current TTL for a stamp."""
        try:
            stamps = swarm_api.get_all_stamps_processed()
            stamp = next((s for s in stamps if s.get("batchID") == batch_id), None)
            if stamp:
                return stamp.get("batchTTL", 0)
        except Exception as e:
            logger.warning(f"Error getting stamp TTL: {e}")
        return None

    async def _update_stamp_ttls(self):
        """Update TTL information for all pool stamps."""
        try:
            stamps = swarm_api.get_all_stamps_processed()
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

                if to_remove:
                    self._save_state()

        except Exception as e:
            logger.warning(f"Error updating stamp TTLs: {e}")

    async def _topup_stamp(self, batch_id: str):
        """Top up a stamp with additional TTL."""
        try:
            # Get current price
            chainstate = swarm_api.get_chainstate()
            current_price = chainstate.get("currentPrice", 0)

            # Calculate amount for configured top-up duration
            topup_hours = settings.STAMP_POOL_TOPUP_HOURS
            amount = swarm_api.calculate_stamp_amount(topup_hours, current_price)

            logger.info(f"Topping up stamp {batch_id[:16]}... with {topup_hours}h ({amount} PLUR)")

            swarm_api.extend_postage_stamp(batch_id, amount)

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
