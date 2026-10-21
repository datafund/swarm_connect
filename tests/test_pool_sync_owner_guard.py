"""Pool sync does not re-import a batch registered to another owner (#477).

_save_state swallows write errors. When the save after a release fails, the
state file still lists the sold batch, and the next sync used to re-import it
as AVAILABLE. The ownership guard kept the owner record, but the pool would
sell the batch a second time.
"""
import json
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.services.stamp_ownership import POOL_OWNER, stamp_ownership_manager
from app.services.stamp_pool import PoolStamp, PoolStampStatus, StampPoolManager

BATCH = "a" * 64
WALLET = "0xa1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1"


def _manager(tmp_path, *batch_ids):
    mgr = StampPoolManager(state_file=str(tmp_path / "pool_state.json"))
    for b in batch_ids:
        mgr._pool[b] = PoolStamp(batch_id=b, depth=17, amount=1,
                                 created_at=datetime.now(timezone.utc), ttl_at_creation=3600)
    mgr._save_state()
    return mgr


def _state(mgr):
    with open(mgr._get_state_file_path()) as f:
        return json.load(f)


def _bee(*batch_ids):
    return [{"batchID": b, "depth": 17, "usable": True, "batchTTL": 604800,
             "amount": "1", "label": ""} for b in batch_ids]


async def _sync(mgr, *on_bee):
    with patch("app.services.stamp_pool.swarm_api.get_all_stamps_processed",
               new=AsyncMock(return_value=_bee(*on_bee))):
        return await mgr.sync_from_bee_node()


def _release_with_failed_save(mgr, how):
    """Release BATCH while the state file cannot be written."""
    with patch("app.services.stamp_pool.atomic_write_json", side_effect=OSError("disk full")):
        if how == "release_stamp":
            assert mgr.release_stamp(BATCH) is not None
        else:
            assert mgr.reserve_stamp(BATCH) is not None
            assert mgr.release_reserved_stamp(BATCH) is not None
    # The save failed, so the state file still lists the sold batch.
    assert _state(mgr) == [BATCH]


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["release_stamp", "release_reserved_stamp"])
@pytest.mark.parametrize("owner,mode", [(WALLET, "paid"), ("shared", "free")])
async def test_sold_batch_is_not_resold_after_a_failed_state_save(tmp_path, monkeypatch, caplog, how, owner, mode):
    mgr = _manager(tmp_path, BATCH)
    monkeypatch.setitem(stamp_ownership_manager._registry, BATCH, {"owner": POOL_OWNER, "mode": "pool"})
    _release_with_failed_save(mgr, how)
    # The acquire handler registers the caller once the batch is released.
    monkeypatch.setitem(stamp_ownership_manager._registry, BATCH, {"owner": owner, "mode": mode})

    with caplog.at_level(logging.ERROR, logger="app.services.stamp_pool"):
        assert await _sync(mgr, BATCH) == 0

    assert BATCH not in mgr._pool
    assert mgr.get_available_stamp(17) is None
    assert stamp_ownership_manager.get_stamp_info(BATCH)["owner"] == owner
    # Saving works again, so the cleanup drops the batch from state.
    assert _state(mgr) == []
    assert any(BATCH[:16] in r.getMessage() and r.levelno == logging.ERROR for r in caplog.records)


@pytest.mark.asyncio
async def test_pool_owned_batch_is_still_imported(tmp_path, monkeypatch):
    mgr = _manager(tmp_path, BATCH)
    monkeypatch.setitem(stamp_ownership_manager._registry, BATCH, {"owner": POOL_OWNER, "mode": "pool"})
    after = StampPoolManager(state_file=mgr._get_state_file_path())
    assert await _sync(after, BATCH) == 1
    assert after.get_available_stamp(17).batch_id == BATCH
    assert _state(after) == [BATCH]


@pytest.mark.asyncio
async def test_unregistered_batch_is_imported_and_claimed_by_the_pool(tmp_path, monkeypatch):
    mgr = _manager(tmp_path, BATCH)
    monkeypatch.delitem(stamp_ownership_manager._registry, BATCH, raising=False)
    monkeypatch.setattr(stamp_ownership_manager, "_save_state", lambda: None)
    after = StampPoolManager(state_file=mgr._get_state_file_path())
    try:
        assert await _sync(after, BATCH) == 1
        assert after.get_available_stamp(17).batch_id == BATCH
        assert stamp_ownership_manager.get_stamp_info(BATCH)["owner"] == POOL_OWNER
    finally:
        stamp_ownership_manager._registry.pop(BATCH, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner,mode", [(WALLET, "paid"), ("shared", "free")])
async def test_foreign_owned_batch_already_in_pool_is_taken_off_sale(tmp_path, monkeypatch, caplog, owner, mode):
    """Not reachable through the acquire handler today; defence in depth."""
    mgr = _manager(tmp_path, BATCH)
    monkeypatch.setitem(stamp_ownership_manager._registry, BATCH, {"owner": owner, "mode": mode})
    with caplog.at_level(logging.ERROR, logger="app.services.stamp_pool"):
        assert await _sync(mgr, BATCH) == 0
    assert BATCH not in mgr._pool
    assert _state(mgr) == []
    assert stamp_ownership_manager.get_stamp_info(BATCH)["owner"] == owner
    assert any("dropping it from pool state" in r.getMessage() for r in caplog.records)
    assert not any("left for that acquire" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_unreadable_ownership_record_fails_safe(tmp_path, monkeypatch, caplog):
    """If the registry lookup raises, the batch is neither imported nor
    forgotten: it stays in state for the next sync to try again."""
    other = "b" * 64
    mgr = _manager(tmp_path, BATCH, other)
    monkeypatch.setitem(stamp_ownership_manager._registry, other, {"owner": POOL_OWNER, "mode": "pool"})
    after = StampPoolManager(state_file=mgr._get_state_file_path())
    real = stamp_ownership_manager.get_stamp_info

    def flaky(batch_id):
        if batch_id == BATCH:
            raise RuntimeError("registry unavailable")
        return real(batch_id)

    monkeypatch.setattr(stamp_ownership_manager, "get_stamp_info", flaky)
    with caplog.at_level(logging.ERROR, logger="app.services.stamp_pool"):
        assert await _sync(after, BATCH, other) == 1

    assert BATCH not in after._pool
    assert after._pool[other].status == PoolStampStatus.AVAILABLE
    assert sorted(_state(after)) == sorted([BATCH, other])
    assert after._last_sync_ok is True
    assert any(BATCH[:16] in r.getMessage() and r.levelno == logging.ERROR for r in caplog.records)

    # Next sync, with the registry readable and the batch the pool's, imports it.
    monkeypatch.setattr(stamp_ownership_manager, "get_stamp_info", real)
    monkeypatch.setitem(stamp_ownership_manager._registry, BATCH, {"owner": POOL_OWNER, "mode": "pool"})
    assert await _sync(after, BATCH, other) == 1
    assert BATCH in after._pool


@pytest.mark.asyncio
async def test_reserved_batch_is_left_for_its_acquire(tmp_path, monkeypatch, caplog):
    """A batch mid-acquire must stay reserved so release_reserved_stamp finds
    it after settlement; evicting it would leave a paid caller with nothing."""
    mgr = _manager(tmp_path, BATCH)
    mgr.reserve_stamp(BATCH)
    monkeypatch.setitem(stamp_ownership_manager._registry, BATCH, {"owner": WALLET, "mode": "paid"})
    with caplog.at_level(logging.ERROR, logger="app.services.stamp_pool"):
        assert await _sync(mgr, BATCH) == 0
    assert any("RESERVED for an acquire in progress; left for that acquire" in r.getMessage()
               for r in caplog.records)
    assert not any("dropping it from pool state" in r.getMessage() for r in caplog.records)
    assert _state(mgr) == [BATCH]
    assert mgr._pool[BATCH].status == PoolStampStatus.RESERVED
    assert mgr.release_reserved_stamp(BATCH) is not None
