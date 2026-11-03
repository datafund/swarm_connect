# app/x402/state_retry.py
"""Retry a failed or stalled payment-state write without paid traffic (#585).

The settlement-unknown registry and the idempotency store refuse paid
requests while a write failed or stalled, and only a later write clears that.
Their retry runs when something reads `unavailable`: a paid request, or a
/health probe of a stall. With no paid traffic, a disk that has recovered
would therefore keep refusing, and keep the alert firing, until the next paid
request happened to be refused and start the retry.

This task reads `unavailable` on each store that reports a failed or stalled
write, every WRITE_RETRY_SECONDS, which starts the same rate-limited
background retry (or probe) a request starts. It never writes on the event
loop. While everything is healthy it only checks two flags per interval.
"""
import asyncio
import logging
from typing import Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

_task: Optional[asyncio.Task] = None


def _stores():
    from app.x402.idempotency import idempotency_store
    from app.x402.settlement import outcome_unknown
    return (outcome_unknown, idempotency_store)


def retry_blocked_writes() -> int:
    """One pass: start the store's own retry for each store whose writes are
    failing or stalled. Returns how many stores were blocked."""
    if not settings.X402_ENABLED:
        return 0
    blocked = 0
    for store in _stores():
        try:
            if store.write_failed() or store.write_stalled():
                blocked += 1
                store.unavailable   # starts the rate-limited background retry or probe
        except Exception as e:      # never let one bad pass end the task
            logger.warning("x402: state write retry check failed: %s", e)
    return blocked


async def _run(interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        retry_blocked_writes()


async def start_state_write_retry_task(interval: Optional[float] = None) -> None:
    """Start the task (once). interval defaults to WRITE_RETRY_SECONDS."""
    global _task
    if _task is not None and not _task.done():
        return
    if interval is None:
        from app.x402.settlement import WRITE_RETRY_SECONDS
        interval = WRITE_RETRY_SECONDS
    _task = asyncio.create_task(_run(interval), name="x402-state-write-retry")
    logger.info("x402: state write retry task started (every %ss)", interval)


async def stop_state_write_retry_task() -> None:
    """Cancel the task and wait for it to end."""
    global _task
    task, _task = _task, None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    logger.info("x402: state write retry task stopped")
