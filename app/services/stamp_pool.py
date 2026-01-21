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
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional
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

    def __init__(self):
        self._pool: Dict[str, PoolStamp] = {}  # batch_id -> PoolStamp
        self._lock = Lock()
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_check: Optional[datetime] = None
        self._errors: List[str] = []

    @property
    def is_enabled(self) -> bool:
        """Check if stamp pool feature is enabled."""
        return settings.STAMP_POOL_ENABLED

    def get_reserve_config(self) -> Dict[int, int]:
        """Get the configured reserve levels by depth."""
        return settings.get_stamp_pool_reserve_config()

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

            return stamp

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
            return stamp

    async def sync_from_bee_node(self) -> int:
        """
        Sync pool with stamps from the Bee node.

        This finds existing stamps that match pool criteria and adds them.
        Useful for initial population or recovery.

        Returns:
            Number of stamps synced
        """
        try:
            all_stamps = swarm_api.get_all_stamps_processed()
            synced_count = 0
            reserve_config = self.get_reserve_config()
            target_depths = set(reserve_config.keys())

            with self._lock:
                for stamp_data in all_stamps:
                    batch_id = stamp_data.get("batchID")
                    depth = stamp_data.get("depth")
                    is_local = stamp_data.get("local", False)
                    usable = stamp_data.get("usable", False)
                    ttl = stamp_data.get("batchTTL", 0)

                    # Skip if already in pool
                    if batch_id in self._pool:
                        continue

                    # Only add local, usable stamps with matching depth
                    if not is_local or not usable:
                        continue

                    if depth not in target_depths:
                        continue

                    # Skip stamps with low TTL
                    min_ttl_seconds = settings.STAMP_POOL_MIN_TTL_HOURS * 3600
                    if ttl < min_ttl_seconds:
                        continue

                    # Check if pool label suggests it's a pool stamp
                    label = stamp_data.get("label", "")

                    # Add to pool
                    amount = int(stamp_data.get("amount", 0))
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
                    synced_count += 1
                    logger.info(f"Synced existing stamp {batch_id[:16]}... to pool (depth={depth}, ttl={ttl}s)")

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
