# app/services/health_snapshot.py
"""
The upstream half of /health, cached (#588).

/ and /health used to read the Bee node summary, the Bee wallet and
chequebook, and the Base RPC on every request: about five upstream calls per
anonymous request, which a flood multiplies onto Bee. Those reads now go into
one snapshot, rebuilt at most once per HEALTH_SNAPSHOT_TTL_SECONDS by a single
refresh (single-flight). Requests are answered from the snapshot they find;
when it is stale they start the refresh and still get the stale one, so they
never wait on Bee. Only before the first snapshot exists does a request wait,
for at most HEALTH_SNAPSHOT_FIRST_WAIT_SECONDS.

The refresh runs as background work for the Bee call gate, so it uses the
reserved slots and a flood of requests cannot starve it.

The snapshot keeps the raw results (node summary, preflight balances, Base
balance). The response, including `status`, is built from them by
app.main.build_health_response.
"""
import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from app.core.config import settings
from app.services.http_client import exc_text

logger = logging.getLogger(__name__)


@dataclass
class HealthSnapshot:
    node: Optional[Dict[str, Any]]       # None: the summary could not be built
    gnosis: Optional[Dict[str, Any]]     # None when x402 is off
    base_eth: Optional[Dict[str, Any]]   # None when x402 is off
    x402: bool                           # built with x402 enabled
    taken_at: float                      # time.monotonic()

    def age(self) -> float:
        return max(0.0, time.monotonic() - self.taken_at)


_current: Optional[HealthSnapshot] = None
_task: Optional[asyncio.Task] = None
_task_loop: Optional[asyncio.AbstractEventLoop] = None
_last_failure: Optional[str] = None
# When the last refresh failed (monotonic). A failed refresh is retried no
# sooner than one TTL later, so a hung Bee is not asked back to back.
_failed_at: Optional[float] = None


def reset() -> None:
    """Forget the snapshot and any refresh (tests)."""
    global _current, _task, _task_loop, _last_failure, _failed_at
    task = _task
    if task is not None and not task.done():
        try:
            task.cancel()
        except RuntimeError:  # its loop is closed
            pass
    _current = None
    _task = None
    _task_loop = None
    _last_failure = None
    _failed_at = None


def snapshot_age() -> Optional[float]:
    return None if _current is None else _current.age()


def last_failure() -> Optional[str]:
    """Why the last refresh failed, as a fixed short text, or None."""
    return _last_failure


def max_trusted_age() -> float:
    """Seconds after which a snapshot whose refresh has failed is not trusted (#589).

    Never less than one TTL plus one refresh timeout: a snapshot is that old
    in normal operation just before a slow refresh lands.
    """
    return max(settings.HEALTH_SNAPSHOT_MAX_TRUSTED_AGE_SECONDS,
               settings.HEALTH_SNAPSHOT_TTL_SECONDS + settings.HEALTH_SNAPSHOT_REFRESH_TIMEOUT_SECONDS)


def too_old(snapshot: Optional["HealthSnapshot"]) -> bool:
    """The snapshot is too old to trust (#589).

    True when the last refresh failed and the snapshot is older than
    max_trusted_age(). An old snapshot whose refreshes did not fail is not too
    old: refreshes are started by requests, so on an idle gateway the first
    request finds an old snapshot and starts the refresh itself.
    """
    if snapshot is None or _last_failure is None:
        return False
    return snapshot.age() > max_trusted_age()


def peek(x402: bool) -> Optional["HealthSnapshot"]:
    """The current snapshot without waiting, or None if there is none (#589).

    For per-request checks: never waits and never calls Bee. A stale snapshot
    starts the single-flight refresh in the background, as get_snapshot does.
    With none at all it starts nothing: startup and /health build the first.
    """
    snap = _current if (_current is not None and _current.x402 == x402) else None
    if snap is not None and snap.age() >= settings.HEALTH_SNAPSHOT_TTL_SECONDS:
        try:
            _start_refresh(x402)
        except RuntimeError:  # no running event loop
            pass
    return snap


def _count(result: str) -> None:
    try:
        from app.services.metrics import health_snapshot_refreshes_total
        health_snapshot_refreshes_total.labels(result=result).inc()
    except Exception as e:  # pragma: no cover - defensive
        logger.debug(f"Metrics: could not count a snapshot refresh: {e}")


async def _build(x402: bool) -> HealthSnapshot:
    # Looked up at call time, so tests that patch the module functions apply.
    from app.services import swarm_api

    async def node():
        try:
            return await swarm_api.get_node_status_summary()
        except Exception as e:
            logger.warning(f"Health: failed to summarize Bee node status: {exc_text(e)}")
            return None

    if not x402:
        return HealthSnapshot(node=await node(), gnosis=None, base_eth=None, x402=False,
                              taken_at=time.monotonic())
    from app.x402 import base_balance, preflight
    # The three reads are independent: one refresh takes as long as the slowest.
    node_summary, base_eth, gnosis = await asyncio.gather(
        node(), base_balance.check_base_eth_balance(), preflight.check_preflight_balances())
    return HealthSnapshot(node=node_summary, gnosis=gnosis, base_eth=base_eth, x402=True,
                          taken_at=time.monotonic())


async def _refresh(x402: bool) -> Optional[HealthSnapshot]:
    global _current, _last_failure, _failed_at
    from app.services import bee_gate
    bee_gate.enter_background()
    try:
        snap = await asyncio.wait_for(_build(x402), timeout=settings.HEALTH_SNAPSHOT_REFRESH_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        _last_failure = "refresh timed out"
        _failed_at = time.monotonic()
        _count("timeout")
        logger.warning(f"Health snapshot refresh took longer than "
                       f"{settings.HEALTH_SNAPSHOT_REFRESH_TIMEOUT_SECONDS:g} s; keeping the previous one")
        return None
    except Exception as e:
        _last_failure = "refresh failed"
        _failed_at = time.monotonic()
        _count("error")
        logger.warning(f"Health snapshot refresh failed; keeping the previous one: {exc_text(e)}")
        return None
    _current = snap
    _last_failure = None
    _failed_at = None
    _count("ok")
    return snap


def _start_refresh(x402: bool) -> Optional[asyncio.Task]:
    """The refresh in progress, or a new one (single-flight per event loop).

    None when the last refresh failed less than one TTL ago.
    """
    global _task, _task_loop
    loop = asyncio.get_running_loop()
    if _task is not None and not _task.done() and _task_loop is loop:
        return _task
    if _failed_at is not None and time.monotonic() - _failed_at < settings.HEALTH_SNAPSHOT_TTL_SECONDS:
        return None
    _task = loop.create_task(_refresh(x402))
    _task_loop = loop
    return _task


def start_refresh(x402: bool) -> None:
    """Prime the snapshot without waiting (startup)."""
    _start_refresh(x402)


async def get_snapshot(x402: bool) -> Optional[HealthSnapshot]:
    """The snapshot to answer with, or None if there is none yet.

    A snapshot built with the other x402 setting is not used.
    """
    snap = _current if (_current is not None and _current.x402 == x402) else None
    if snap is not None and snap.age() < settings.HEALTH_SNAPSHOT_TTL_SECONDS:
        return snap
    task = _start_refresh(x402)
    if snap is not None or task is None:
        return snap  # stale while it refreshes, or None while retries back off
    try:
        return await asyncio.wait_for(asyncio.shield(task),
                                      timeout=settings.HEALTH_SNAPSHOT_FIRST_WAIT_SECONDS)
    except asyncio.TimeoutError:
        return None


async def stop() -> None:
    """Stop a refresh in progress (shutdown)."""
    global _task
    task = _task
    _task = None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except BaseException:
            pass
