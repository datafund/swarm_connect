# app/services/stamp_tracker.py
"""
In-memory tracker for stamps purchased through this gateway.

Records purchase timestamps so we can calculate propagation timing signals
(secondsSincePurchase, estimatedReadyAt, propagationStatus) for consumers
polling stamp status after purchase.

Only tracks stamps bought via our gateway — external stamps show "unknown"
propagation status, which is correct since we don't know when they were purchased.
"""
import datetime
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Maximum age before entries are pruned (10 minutes)
_MAX_TRACKER_AGE_SECONDS = 600

# In-memory store: batchID -> purchase timestamp (UTC)
_purchase_times: Dict[str, datetime.datetime] = {}


def record_purchase(batch_id: str) -> None:
    """Record the purchase timestamp for a stamp bought through this gateway."""
    _prune_old_entries()
    _purchase_times[batch_id] = datetime.datetime.now(datetime.timezone.utc)
    logger.info(f"Recorded purchase time for stamp {batch_id[:16]}...")


def get_purchase_time(batch_id: str) -> Optional[datetime.datetime]:
    """Get the purchase timestamp for a gateway-purchased stamp, or None if unknown."""
    return _purchase_times.get(batch_id)


def clear_tracker() -> None:
    """Clear all tracked purchases. Used for testing."""
    _purchase_times.clear()


def _prune_old_entries() -> None:
    """Remove entries older than _MAX_TRACKER_AGE_SECONDS to prevent unbounded growth."""
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(seconds=_MAX_TRACKER_AGE_SECONDS)
    stale_ids = [bid for bid, ts in _purchase_times.items() if ts < cutoff]
    for bid in stale_ids:
        del _purchase_times[bid]
    if stale_ids:
        logger.debug(f"Pruned {len(stale_ids)} old stamp tracker entries")
