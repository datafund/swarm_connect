"""Both replenishment paths count purchases still in flight (#428 f).

An immediate replenishment after a release buys in the background. Scheduled
maintenance used to count only the batches in the pool, so a run that started
while such a purchase was in flight bought the same batch again: on a local
stack the pool ended at 4 against a target of 2, and 5 against 4. The reverse
also held: an acquire during a maintenance purchase started an immediate one
for the same shortfall. Now both paths compare held + in flight with the
target, and a purchase stops counting as in flight in the same step that puts
its batch in the pool, so it is never counted twice.
"""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.services.stamp_pool import PoolStamp, PoolStampStatus, StampPoolManager, _HELD

DEPTH = 17


def _stamp(batch_id, depth=DEPTH):
    return PoolStamp(batch_id=batch_id, depth=depth, amount=1,
                     created_at=datetime.now(timezone.utc), ttl_at_creation=3600)


def _manager(tmp_path, *batch_ids):
    mgr = StampPoolManager(state_file=str(tmp_path / "pool_state.json"))
    for b in batch_ids:
        mgr._pool[b] = _stamp(b)
    mgr._save_state()
    # Synced, so the immediate path is not paused by #533.
    mgr._last_sync_ok = True
    return mgr


def _held(mgr):
    return len([s for s in mgr._pool.values() if s.depth == DEPTH and s.status in _HELD])


class FakeBuyer:
    """Stands in for _purchase_stamp. Each call lands a new batch in the pool
    through add_stamp_to_pool, as the real one does, unless told to fail.
    A call can be held at a gate to keep it in flight."""

    def __init__(self, mgr):
        self.mgr = mgr
        self.calls = 0
        self.gate = None          # asyncio.Event: calls wait on it when set
        self.outcome = "land"     # "land", "none" or "raise"
        self.started = asyncio.Event()

    async def __call__(self, depth, max_retries=3):
        self.calls += 1
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.outcome == "raise":
            raise RuntimeError("Bee refused the purchase")
        if self.outcome == "none":
            return None
        batch_id = f"{self.calls:064x}"
        self.mgr.add_stamp_to_pool(batch_id=batch_id, depth=depth, amount=1, ttl=604800)
        return batch_id


@contextmanager
def _env(mgr, buyer, target):
    """Patch Bee, settings and ownership so maintenance and the immediate path
    run against the in-memory pool. Bee lists whatever the pool holds."""

    async def bee_listing():
        return [{"batchID": b, "depth": DEPTH, "usable": True, "batchTTL": 604800,
                 "amount": "1", "label": ""} for b in list(mgr._pool)]

    with patch.object(mgr, "_purchase_stamp", new=buyer), \
         patch.object(mgr, "get_reserve_config", return_value={DEPTH: target}), \
         patch("app.services.stamp_pool.swarm_api.get_all_stamps_processed",
               new=AsyncMock(side_effect=bee_listing)), \
         patch.object(mgr, "_get_stamp_ttl", new=AsyncMock(return_value=604800)), \
         patch("app.services.stamp_ownership.stamp_ownership_manager.register_stamp"), \
         patch("app.services.stamp_pool.stamp_ownership_manager.get_stamp_info",
               return_value=None), \
         patch("app.services.stamp_pool.settings") as s:
        s.STAMP_POOL_ENABLED = True
        s.STAMP_POOL_IMMEDIATE_REPLENISH = True
        s.STAMP_POOL_MIN_TTL_HOURS = 24
        yield


async def _until(predicate, steps=200):
    for _ in range(steps):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not reached")


@pytest.mark.asyncio
async def test_maintenance_counts_an_in_flight_immediate_replenishment(tmp_path):
    """The reproduction: target 2, 1 held, 1 immediate purchase in flight.
    A maintenance run must buy nothing."""
    mgr = _manager(tmp_path, "a" * 64)
    mgr._pending_replenishments[DEPTH] = 1
    buyer = FakeBuyer(mgr)
    with _env(mgr, buyer, target=2):
        results = await asyncio.wait_for(mgr.check_and_replenish(), 2)
    assert buyer.calls == 0
    assert results["stamps_purchased"] == 0
    # Maintenance does not touch the immediate path's claim.
    assert mgr._pending_replenishments == {DEPTH: 1}


@pytest.mark.asyncio
async def test_release_then_maintenance_ends_at_target_not_over_it(tmp_path):
    """End to end: a release triggers an immediate purchase that is still in
    flight when a maintenance run starts. The pool ends at the target."""
    mgr = _manager(tmp_path, "a" * 64)
    buyer = FakeBuyer(mgr)
    buyer.gate = asyncio.Event()
    with _env(mgr, buyer, target=2):
        assert mgr.trigger_replenishment_if_needed(DEPTH) is True
        await asyncio.wait_for(buyer.started.wait(), 1)
        results = await asyncio.wait_for(mgr.check_and_replenish(), 2)
        assert buyer.calls == 1
        assert results["stamps_purchased"] == 0

        buyer.gate.set()
        await _until(lambda: DEPTH not in mgr._pending_replenishments)
    assert _held(mgr) == 2
    assert buyer.calls == 1


@pytest.mark.asyncio
async def test_pending_then_landed_is_counted_once(tmp_path):
    """Once the immediate purchase lands, its batch is held and no longer in
    flight: a later run neither buys it again nor treats it as two."""
    mgr = _manager(tmp_path, "a" * 64)
    buyer = FakeBuyer(mgr)
    buyer.gate = asyncio.Event()
    with _env(mgr, buyer, target=3):
        assert mgr.trigger_replenishment_if_needed(DEPTH) is True
        await asyncio.wait_for(buyer.started.wait(), 1)
        buyer.gate.set()
        await _until(lambda: DEPTH not in mgr._pending_replenishments)
        assert _held(mgr) == 2
        assert mgr._pending_replenishments == {}

        # One short of 3: exactly one more, not zero (no double count) and
        # not two (the landed batch is counted).
        buyer.gate = None
        results = await asyncio.wait_for(mgr.check_and_replenish(), 2)
    assert results["stamps_purchased"] == 1
    assert buyer.calls == 2
    assert _held(mgr) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["raise", "none"])
async def test_pending_then_failed_is_bought_by_maintenance(tmp_path, outcome):
    """A failed immediate purchase stops counting as in flight, so the next
    run fills the gap it left."""
    mgr = _manager(tmp_path, "a" * 64)
    buyer = FakeBuyer(mgr)
    buyer.gate = asyncio.Event()
    buyer.outcome = outcome
    with _env(mgr, buyer, target=2):
        assert mgr.trigger_replenishment_if_needed(DEPTH) is True
        await asyncio.wait_for(buyer.started.wait(), 1)
        buyer.gate.set()
        await _until(lambda: DEPTH not in mgr._pending_replenishments)
        assert _held(mgr) == 1

        buyer.gate = None
        buyer.outcome = "land"
        results = await asyncio.wait_for(mgr.check_and_replenish(), 2)
    assert results["stamps_purchased"] == 1
    assert _held(mgr) == 2
    assert mgr._pending_replenishments == {}


@pytest.mark.asyncio
async def test_maintenance_counts_again_before_each_purchase(tmp_path):
    """An immediate replenishment that starts during the wait between two
    maintenance purchases is counted: target 3 from empty buys 2, not 3."""
    mgr = _manager(tmp_path)
    buyer = FakeBuyer(mgr)

    waits = []

    async def wait_between_purchases(seconds):
        # Simulates an acquire plus immediate replenishment starting while
        # maintenance waits out Bee's rate limit, during the first wait only.
        waits.append(seconds)
        if len(waits) == 1:
            with mgr._lock:
                mgr._pending_replenishments[DEPTH] = 1

    with _env(mgr, buyer, target=3), \
         patch("app.services.stamp_pool.asyncio.sleep", new=wait_between_purchases):
        results = await asyncio.wait_for(mgr.check_and_replenish(), 2)
    assert results["stamps_purchased"] == 2
    assert buyer.calls == 2
    assert _held(mgr) == 2
    # Only the simulated immediate claim is left; maintenance released its own.
    assert mgr._pending_replenishments == {DEPTH: 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["land", "raise"])
async def test_immediate_counts_an_in_flight_maintenance_purchase(tmp_path, outcome):
    """The reverse direction: an acquire during a maintenance purchase does not
    start a second purchase for the same shortfall, and the maintenance claim
    is released whether its purchase lands or fails."""
    mgr = _manager(tmp_path)
    buyer = FakeBuyer(mgr)
    buyer.gate = asyncio.Event()
    buyer.outcome = outcome
    with _env(mgr, buyer, target=1):
        run = asyncio.create_task(mgr.check_and_replenish())
        await asyncio.wait_for(buyer.started.wait(), 1)
        assert mgr._pending_replenishments == {DEPTH: 1}
        assert mgr.trigger_replenishment_if_needed(DEPTH) is False

        buyer.gate.set()
        results = await asyncio.wait_for(run, 1)
    assert buyer.calls == 1
    assert mgr._pending_replenishments == {}
    if outcome == "land":
        assert results["stamps_purchased"] == 1
        assert _held(mgr) == 1
    else:
        assert results["stamps_purchased"] == 0
        assert any("Failed to purchase" in e for e in results["errors"])
        assert _held(mgr) == 0


def test_reserved_and_in_flight_both_count_for_the_immediate_path(tmp_path):
    """Target 2: one reserved batch plus one purchase in flight is enough."""
    mgr = _manager(tmp_path, "a" * 64)
    mgr.reserve_stamp("a" * 64)
    assert mgr._pool["a" * 64].status == PoolStampStatus.RESERVED
    mgr._pending_replenishments[DEPTH] = 1
    with patch.object(mgr, "get_reserve_config", return_value={DEPTH: 2}), \
         patch("app.services.stamp_pool.settings") as s, \
         patch("asyncio.create_task") as spawn:
        s.STAMP_POOL_IMMEDIATE_REPLENISH = True
        assert mgr.trigger_replenishment_if_needed(DEPTH) is False
    spawn.assert_not_called()
