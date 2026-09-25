"""A pool batch stays in the pool as RESERVED while its payment settles (#403).

Since #398 a paid acquire claims its batch before settling. Claiming used to
mean removing it from the pool, so for the length of the settlement a replenish
check saw one batch fewer and bought an extra, and a crash lost the batch from
pool state. Now the batch is reserved: still in the pool and the state file,
counted toward the target, but never selected or sold.
"""
import asyncio
import json
import threading
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI

from app.services.stamp_pool import PoolStamp, PoolStampStatus, StampPoolManager


def _stamp(batch_id, depth=17):
    return PoolStamp(batch_id=batch_id, depth=depth, amount=1,
                     created_at=datetime.now(timezone.utc), ttl_at_creation=3600)


def _manager(tmp_path, *batch_ids):
    mgr = StampPoolManager(state_file=str(tmp_path / "pool_state.json"))
    for b in batch_ids:
        mgr._pool[b] = _stamp(b)
    mgr._save_state()
    return mgr


def _state(mgr):
    with open(mgr._get_state_file_path()) as f:
        return json.load(f)


def _bee(*batch_ids):
    return [{"batchID": b, "depth": 17, "usable": True, "batchTTL": 604800,
             "amount": "1", "label": ""} for b in batch_ids]


def test_reserved_batch_stays_in_pool_and_state_but_is_not_selectable(tmp_path):
    mgr = _manager(tmp_path, "a" * 64)
    assert mgr.reserve_stamp("a" * 64, reserved_for="1.2.3.4") is not None
    assert mgr._pool["a" * 64].status == PoolStampStatus.RESERVED
    assert "a" * 64 in _state(mgr)
    assert mgr.get_available_stamp(17) is None
    assert mgr.get_available_stamp_any_size(17) is None
    # Neither a second reservation nor a direct release can take it.
    assert mgr.reserve_stamp("a" * 64) is None
    assert mgr.release_stamp("a" * 64) is None
    assert mgr.get_status().total_stamps == 0


def test_successful_settlement_releases_it(tmp_path):
    mgr = _manager(tmp_path, "a" * 64)
    mgr.reserve_stamp("a" * 64, reserved_for="1.2.3.4")
    released = mgr.release_reserved_stamp("a" * 64)
    assert released.status == PoolStampStatus.RELEASED
    assert released.released_to == "1.2.3.4"
    assert released.released_at is not None
    assert "a" * 64 not in mgr._pool
    assert _state(mgr) == []
    # Once released it is gone: nothing to release or unreserve again.
    assert mgr.release_reserved_stamp("a" * 64) is None
    mgr.unreserve_stamp("a" * 64)
    assert "a" * 64 not in mgr._pool


def test_failed_settlement_makes_it_available_again(tmp_path):
    mgr = _manager(tmp_path, "a" * 64)
    mgr.reserve_stamp("a" * 64, reserved_for="1.2.3.4")
    mgr.unreserve_stamp("a" * 64)
    stamp = mgr.get_available_stamp(17)
    assert stamp.batch_id == "a" * 64
    assert stamp.status == PoolStampStatus.AVAILABLE
    assert stamp.released_to is None
    assert "a" * 64 in _state(mgr)


def test_only_a_reserved_batch_can_be_released_that_way(tmp_path):
    mgr = _manager(tmp_path, "a" * 64)
    assert mgr.release_reserved_stamp("a" * 64) is None
    assert mgr.get_available_stamp(17) is not None


@pytest.mark.asyncio
async def test_replenish_during_a_reservation_does_not_over_buy(tmp_path):
    mgr = _manager(tmp_path, "a" * 64, "b" * 64)
    mgr.reserve_stamp("a" * 64)
    buy = AsyncMock(return_value="c" * 64)
    with patch.object(mgr, "_purchase_stamp", new=buy), \
         patch.object(mgr, "get_reserve_config", return_value={17: 2}), \
         patch("app.services.stamp_pool.swarm_api.get_all_stamps_processed",
               new=AsyncMock(return_value=_bee("a" * 64, "b" * 64))), \
         patch.object(mgr, "_get_stamp_ttl", new=AsyncMock(return_value=604800)), \
         patch("app.services.stamp_pool.settings") as s:
        s.STAMP_POOL_ENABLED = True
        s.STAMP_POOL_MIN_TTL_HOURS = 24
        await mgr.check_and_replenish()
    buy.assert_not_called()
    # The TTL sweep leaves the reserved batch to the settling request.
    assert mgr._pool["a" * 64].status == PoolStampStatus.RESERVED


def test_immediate_replenish_counts_reserved_batches(tmp_path):
    mgr = _manager(tmp_path, "a" * 64, "b" * 64)
    mgr.reserve_stamp("a" * 64)
    with patch.object(mgr, "get_reserve_config", return_value={17: 2}), \
         patch("app.services.stamp_pool.settings") as s, \
         patch("asyncio.create_task") as spawn:
        s.STAMP_POOL_IMMEDIATE_REPLENISH = True
        assert mgr.trigger_replenishment_if_needed(17) is False
    spawn.assert_not_called()


@pytest.mark.asyncio
async def test_restart_with_a_reserved_batch_makes_it_available(tmp_path):
    """Statuses are not persisted, only batch IDs: a batch reserved when the
    process died is still in the state file and comes back available."""
    before = _manager(tmp_path, "a" * 64)
    before.reserve_stamp("a" * 64)
    assert _state(before) == ["a" * 64]

    after = StampPoolManager(state_file=before._get_state_file_path())
    with patch("app.services.stamp_pool.swarm_api.get_all_stamps_processed",
               new=AsyncMock(return_value=_bee("a" * 64))):
        assert await after.sync_from_bee_node() == 1
    assert after.get_available_stamp(17).status == PoolStampStatus.AVAILABLE


def test_concurrent_reservations_never_share_a_batch(tmp_path):
    ids = [f"{i:064x}" for i in range(20)]
    mgr = _manager(tmp_path, *ids)
    got, start = [], threading.Barrier(40)

    def worker():
        start.wait()
        stamp = mgr.get_available_stamp_any_size(17)
        if stamp and mgr.reserve_stamp(stamp.batch_id):
            got.append(stamp.batch_id)

    threads = [threading.Thread(target=worker) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(got) == len(set(got))


@pytest.mark.asyncio
async def test_concurrent_acquires_never_get_the_same_batch(tmp_path, monkeypatch):
    """Through the real handler, with settlement taking time: each caller gets
    a different batch, the one left over is refused, and mid-settlement the
    claimed batches are reserved in the pool rather than gone from it."""
    from app.api.endpoints import pool
    from app.core.config import settings
    monkeypatch.setattr(settings, "STAMP_POOL_ENABLED", True)
    mgr = _manager(tmp_path, "a" * 64, "b" * 64)
    monkeypatch.setattr(pool, "stamp_pool_manager", mgr)
    monkeypatch.setattr(mgr, "trigger_replenishment_if_needed", lambda d: False)
    monkeypatch.setattr(pool.stamp_ownership_manager, "register_stamp", lambda **kw: None)
    during = []

    async def slow_settle(request):
        await asyncio.sleep(0.05)
        during.append(sorted(s.status.value for s in mgr._pool.values()))

    monkeypatch.setattr(pool, "settle_payment", slow_settle)
    app = FastAPI()
    app.include_router(pool.router, prefix="/api/v1/pool")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        rs = await asyncio.gather(*[c.post("/api/v1/pool/acquire", json={"size": "small"})
                                    for _ in range(3)])

    ok = [r.json()["batch_id"] for r in rs if r.status_code == 200]
    assert sorted(ok) == ["a" * 64, "b" * 64]
    assert [r.status_code for r in rs].count(409) == 1
    assert ["reserved", "reserved"] in during
    assert mgr._pool == {} and _state(mgr) == []
