"""The pool never writes over an unreadable state file (#547).

When pool_state.json cannot be read, the sync fails and pool spending is paused
until the file is repaired or restored (#461). Releasing a stamp used to call
_save_state, which wrote the in-memory list over the corrupt file. The next sync
then read that file and succeeded, so the pause ended without anyone repairing
the file, and batch ids that were only in the corrupt file were gone from the
live file.

Every writer goes through _save_state, so these tests drive each writer and the
operator's recovery paths: repair, restore from backup, or move the file away.
"""
import glob
import json
import logging
import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.services.stamp_ownership import POOL_OWNER, stamp_ownership_manager
from app.services.stamp_pool import PoolStamp, StampPoolManager

A = "a" * 64
B = "b" * 64
C = "c" * 64
WALLET = "0xa1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1"
CORRUPT = b'["' + A.encode() + b'", "trunc'


def _bee(*batch_ids):
    return [{"batchID": b, "depth": 17, "usable": True, "batchTTL": 604800,
             "amount": "1", "label": ""} for b in batch_ids]


async def _sync(mgr, *on_bee):
    with patch("app.services.stamp_pool.swarm_api.get_all_stamps_processed",
               new=AsyncMock(return_value=_bee(*on_bee))):
        return await mgr.sync_from_bee_node()


def _path(mgr):
    return mgr._get_state_file_path()


def _read(mgr):
    with open(_path(mgr), "rb") as f:
        return f.read()


def _state(mgr):
    return json.loads(_read(mgr))


def _corrupt(mgr):
    with open(_path(mgr), "wb") as f:
        f.write(CORRUPT)


def _write(mgr, ids):
    with open(_path(mgr), "w") as f:
        json.dump(ids, f)


async def _good_pool(tmp_path, monkeypatch, *ids):
    """A pool that has had a good sync importing `ids`."""
    for b in ids:
        monkeypatch.setitem(stamp_ownership_manager._registry, b, {"owner": POOL_OWNER, "mode": "pool"})
    mgr = StampPoolManager(state_file=str(tmp_path / "pool_state.json"))
    _write(mgr, list(ids))
    assert await _sync(mgr, *ids) == len(ids)
    assert mgr._last_sync_ok
    return mgr


def _assert_paused_for_file(mgr):
    assert mgr._last_sync_ok is False
    assert mgr._last_sync_failure == "state_file"


# The issue's scenario: good sync, corrupt file, failed sync, release.

@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["release_stamp", "release_reserved_stamp"])
async def test_release_during_pause_does_not_overwrite_corrupt_file(tmp_path, monkeypatch, how):
    mgr = await _good_pool(tmp_path, monkeypatch, A, B)
    _corrupt(mgr)
    assert await _sync(mgr, A, B) == 0
    _assert_paused_for_file(mgr)

    if how == "release_stamp":
        assert mgr.release_stamp(A) is not None
    else:
        assert mgr.reserve_stamp(A) is not None
        assert mgr.release_reserved_stamp(A) is not None

    assert _read(mgr) == CORRUPT
    _assert_paused_for_file(mgr)
    # Nobody repaired the file, so the next sync still fails.
    assert await _sync(mgr, A, B) == 0
    _assert_paused_for_file(mgr)
    assert _read(mgr) == CORRUPT


@pytest.mark.asyncio
async def test_release_after_file_breaks_between_syncs_pauses_and_keeps_file(tmp_path, monkeypatch):
    """No sync between the corruption and the release: the save itself notices."""
    mgr = await _good_pool(tmp_path, monkeypatch, A, B)
    _corrupt(mgr)

    assert mgr.release_stamp(A) is not None

    assert _read(mgr) == CORRUPT
    _assert_paused_for_file(mgr)
    assert glob.glob(f"{glob.escape(_path(mgr))}.corrupt-*"), "the #440 copy is kept"
    # Base name only: /pool/status is unauthenticated.
    assert mgr._last_sync_error and str(tmp_path) not in mgr._last_sync_error
    # Immediate replenishment honours the pause (#533).
    assert mgr._immediate_replenish_paused(17) is True


@pytest.mark.asyncio
async def test_add_stamp_during_pause_does_not_overwrite_corrupt_file(tmp_path, monkeypatch):
    """A purchase already in flight when the pause starts."""
    mgr = await _good_pool(tmp_path, monkeypatch, A)
    _corrupt(mgr)
    assert await _sync(mgr, A) == 0
    monkeypatch.setattr(stamp_ownership_manager, "register_stamp", lambda **kw: None)

    mgr.add_stamp_to_pool(C, depth=17, amount=1, ttl=3600)

    assert _read(mgr) == CORRUPT
    assert C in mgr._pool


@pytest.mark.asyncio
async def test_ttl_removal_during_pause_does_not_overwrite_corrupt_file(tmp_path, monkeypatch):
    mgr = await _good_pool(tmp_path, monkeypatch, A, B)
    _corrupt(mgr)
    assert await _sync(mgr, A, B) == 0
    monkeypatch.setattr(stamp_ownership_manager, "remove_stamp", lambda batch_id: None)

    # B has expired on the node.
    with patch("app.services.stamp_pool.swarm_api.get_all_stamps_processed",
               new=AsyncMock(return_value=_bee(A))):
        await mgr._update_stamp_ttls()

    assert B not in mgr._pool
    assert _read(mgr) == CORRUPT


@pytest.mark.asyncio
async def test_refused_saves_log_the_batch_list_once_per_change(tmp_path, monkeypatch, caplog):
    """The operator can rebuild the file from the log, without a flood."""
    mgr = await _good_pool(tmp_path, monkeypatch, A, B, C)
    _corrupt(mgr)
    assert await _sync(mgr, A, B, C) == 0

    def listings():
        return [r.getMessage() for r in caplog.records
                if r.levelno == logging.ERROR and "not saving pool state" in r.getMessage()]

    def held(msg):
        return json.loads(msg.split("batch(es): ", 1)[1].split("; released", 1)[0])

    def released(msg):
        return json.loads(msg.split("since the last save: ", 1)[1])

    with caplog.at_level(logging.ERROR, logger="app.services.stamp_pool"):
        mgr.release_stamp(A)
        assert len(listings()) == 1
        assert held(listings()[0]) == [B, C]
        assert released(listings()[0]) == [A]
        # Same content: not repeated.
        mgr._save_state()
        mgr._save_state()
        assert len(listings()) == 1
        # Changed content: logged again.
        mgr.release_stamp(B)
        assert len(listings()) == 2
        assert held(listings()[1]) == [C]
        assert released(listings()[1]) == [A, B]


# Recovery.

@pytest.mark.asyncio
async def test_restored_backup_does_not_resell_batch_sold_during_pause(tmp_path, monkeypatch):
    """The restored file still lists the sold batch, and the buyer's ownership
    registration failed, so the #477 guard cannot catch it either."""
    mgr = await _good_pool(tmp_path, monkeypatch, A, B)
    _corrupt(mgr)
    assert await _sync(mgr, A, B) == 0
    assert mgr.reserve_stamp(A) is not None
    assert mgr.release_reserved_stamp(A) is not None
    assert _read(mgr) == CORRUPT

    # Operator restores the pre-pause backup.
    _write(mgr, [A, B])
    assert await _sync(mgr, A, B) == 0

    assert mgr._last_sync_ok is True
    assert mgr._last_sync_failure is None
    assert A not in mgr._pool
    assert mgr.get_available_stamp(17).batch_id == B
    assert _state(mgr) == [B]


@pytest.mark.asyncio
async def test_batch_added_during_pause_is_saved_after_repair(tmp_path, monkeypatch):
    mgr = await _good_pool(tmp_path, monkeypatch, A)
    _corrupt(mgr)
    assert await _sync(mgr, A) == 0
    monkeypatch.setattr(stamp_ownership_manager, "register_stamp", lambda **kw: None)
    mgr.add_stamp_to_pool(C, depth=17, amount=1, ttl=3600)
    assert _read(mgr) == CORRUPT

    # Repaired to its pre-pause contents: the sync has nothing to clean up.
    _write(mgr, [A])
    await _sync(mgr, A, C)

    assert mgr._last_sync_ok is True
    assert _state(mgr) == sorted([A, C])
    # Saves are back to normal.
    assert mgr.release_stamp(A) is not None
    assert _state(mgr) == [C]


@pytest.mark.asyncio
async def test_file_moved_away_saves_in_memory_pool(tmp_path, monkeypatch):
    """Moving the file away is the documented deliberate fresh start."""
    mgr = await _good_pool(tmp_path, monkeypatch, A, B)
    _corrupt(mgr)
    assert await _sync(mgr, A, B) == 0
    assert mgr.release_stamp(A) is not None

    os.rename(_path(mgr), _path(mgr) + ".moved")
    await _sync(mgr, B)

    assert mgr._last_sync_ok is True
    assert _state(mgr) == [B]


def test_missing_file_is_still_written(tmp_path):
    mgr = StampPoolManager(state_file=str(tmp_path / "pool_state.json"))
    mgr._pool[A] = PoolStamp(batch_id=A, depth=17, amount=1,
                             created_at=datetime.now(timezone.utc), ttl_at_creation=3600)
    mgr._save_state()
    assert _state(mgr) == [A]
    assert mgr._last_sync_failure is None
