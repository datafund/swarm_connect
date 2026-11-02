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


# A pause set mid-cycle by a refused save stops the rest of that cycle.

def _cycle_patches(mgr, monkeypatch, target):
    monkeypatch.setattr("app.services.stamp_pool.settings.STAMP_POOL_ENABLED", True)
    monkeypatch.setattr(mgr, "get_reserve_config", lambda: {17: target})
    monkeypatch.setattr(mgr, "_update_stamp_ttls", AsyncMock())
    monkeypatch.setattr("app.services.stamp_pool.asyncio.sleep", AsyncMock())
    monkeypatch.setattr(stamp_ownership_manager, "register_stamp", lambda **kw: None)


@pytest.mark.asyncio
async def test_purchases_stop_when_a_save_pauses_the_pool_mid_cycle(tmp_path, monkeypatch):
    mgr = await _good_pool(tmp_path, monkeypatch, A)
    _cycle_patches(mgr, monkeypatch, target=4)
    bought = []

    async def purchase(depth, max_retries=3):
        batch_id = format(len(bought) + 1, "064x")
        if not bought:
            _corrupt(mgr)  # breaks while the first purchase is in flight
        bought.append(batch_id)
        mgr.add_stamp_to_pool(batch_id, depth=depth, amount=1, ttl=3600)
        return batch_id

    monkeypatch.setattr(mgr, "_purchase_stamp", purchase)
    topup = AsyncMock(return_value=True)
    monkeypatch.setattr(mgr, "_topup_stamp", topup)
    monkeypatch.setattr(mgr, "_get_stamp_ttl", AsyncMock(return_value=1))

    with patch("app.services.stamp_pool.swarm_api.get_all_stamps_processed",
               new=AsyncMock(return_value=_bee(A))):
        results = await mgr.check_and_replenish()

    assert len(bought) == 1
    topup.assert_not_called()
    _assert_paused_for_file(mgr)
    assert _read(mgr) == CORRUPT
    assert results.get("skipped") is True
    assert any("Skipping replenishment" in e and "pool state file" in e for e in results["errors"])
    assert any("Skipping replenishment" in e for e in mgr._errors)


@pytest.mark.asyncio
async def test_topups_stop_when_a_save_pauses_the_pool_mid_cycle(tmp_path, monkeypatch):
    mgr = await _good_pool(tmp_path, monkeypatch, A, B, C)
    _cycle_patches(mgr, monkeypatch, target=3)
    monkeypatch.setattr(mgr, "_purchase_stamp", AsyncMock(side_effect=AssertionError("no purchase")))
    monkeypatch.setattr(mgr, "_get_stamp_ttl", AsyncMock(return_value=1))
    calls = []

    async def topup(batch_id):
        calls.append(batch_id)
        if len(calls) == 1:
            # The file breaks, and a release during this top-up notices it.
            _corrupt(mgr)
            other = next(b for b in (A, B, C) if b != batch_id)
            assert mgr.release_stamp(other) is not None
        return True

    monkeypatch.setattr(mgr, "_topup_stamp", topup)
    with patch("app.services.stamp_pool.swarm_api.get_all_stamps_processed",
               new=AsyncMock(return_value=_bee(A, B, C))):
        results = await mgr.check_and_replenish()

    assert len(calls) == 1
    _assert_paused_for_file(mgr)
    assert results.get("skipped") is True


# A restored file is not written over until a sync completes fully.

@pytest.mark.asyncio
async def test_restore_then_bee_failing_sync_still_refuses_saves(tmp_path, monkeypatch):
    D = "d" * 64
    mgr = await _good_pool(tmp_path, monkeypatch, A, B)
    monkeypatch.setitem(stamp_ownership_manager._registry, D, {"owner": POOL_OWNER, "mode": "pool"})
    _corrupt(mgr)
    assert await _sync(mgr, A, B) == 0

    _write(mgr, [A, B, D])  # operator restores a backup that also lists D
    with patch("app.services.stamp_pool.swarm_api.get_all_stamps_processed",
               new=AsyncMock(side_effect=RuntimeError("bee down"))):
        assert await mgr.sync_from_bee_node() == 0
    assert mgr._last_sync_ok is False

    assert mgr.release_stamp(A) is not None
    assert _state(mgr) == [A, B, D]
    assert mgr._state_dirty is True
    assert A in mgr._removed_unsaved

    # A full sync reconciles: D imported, A not re-imported, file rewritten.
    await _sync(mgr, A, B, D)
    assert mgr._last_sync_ok is True
    assert _state(mgr) == sorted([B, D])
    assert A not in mgr._pool


@pytest.mark.asyncio
async def test_restore_without_refused_save_then_bee_failing_sync_keeps_file(tmp_path, monkeypatch):
    """Nothing was released during the pause, so nothing is dirty yet; the
    restored file still must not be overwritten before a full sync."""
    D = "d" * 64
    mgr = await _good_pool(tmp_path, monkeypatch, A, B)
    _corrupt(mgr)
    assert await _sync(mgr, A, B) == 0
    _write(mgr, [A, B, D])
    with patch("app.services.stamp_pool.swarm_api.get_all_stamps_processed",
               new=AsyncMock(side_effect=RuntimeError("bee down"))):
        await mgr.sync_from_bee_node()

    assert mgr.release_stamp(A) is not None
    assert _state(mgr) == [A, B, D]


# Ids kept as unreadable by a sync survive later saves.

@pytest.mark.asyncio
async def test_unreadable_ids_survive_a_release(tmp_path, monkeypatch):
    mgr = await _good_pool(tmp_path, monkeypatch, A, B)
    monkeypatch.setitem(stamp_ownership_manager._registry, C, {"owner": POOL_OWNER, "mode": "pool"})
    _write(mgr, [A, B, C])
    bee = _bee(A, B, C)
    bee[2]["depth"] = None
    with patch("app.services.stamp_pool.swarm_api.get_all_stamps_processed",
               new=AsyncMock(return_value=bee)):
        await mgr.sync_from_bee_node()
    assert C not in mgr._pool
    assert _state(mgr) == [A, B, C]

    assert mgr.release_stamp(A) is not None
    assert _state(mgr) == [B, C]

    # C reads fine next time: imported, and no longer carried separately.
    await _sync(mgr, B, C)
    assert C in mgr._pool
    assert mgr.release_stamp(C) is not None
    assert _state(mgr) == [B]


@pytest.mark.asyncio
async def test_unreadable_id_gone_from_bee_is_dropped(tmp_path, monkeypatch):
    mgr = await _good_pool(tmp_path, monkeypatch, A)
    monkeypatch.setitem(stamp_ownership_manager._registry, C, {"owner": POOL_OWNER, "mode": "pool"})
    _write(mgr, [A, C])
    bee = _bee(A, C)
    bee[1]["depth"] = None
    with patch("app.services.stamp_pool.swarm_api.get_all_stamps_processed",
               new=AsyncMock(return_value=bee)):
        await mgr.sync_from_bee_node()
    assert _state(mgr) == [A, C]

    await _sync(mgr, A)  # C expired
    assert _state(mgr) == [A]
    mgr._save_state()
    assert _state(mgr) == [A]


# The pause is checked again right before Bee is asked to spend, after the
# awaits inside _purchase_stamp and _topup_stamp.

class _RateLimited(Exception):
    def __init__(self):
        super().__init__("429 Too Many Requests")
        self.response = type("R", (), {"status_code": 429})()


def _break_file_and_release(mgr, batch_id):
    """The file breaks; a release during the await notices and pauses the pool."""
    _corrupt(mgr)
    assert mgr.release_stamp(batch_id) is not None
    _assert_paused_for_file(mgr)


def _spend_mocks(mgr, monkeypatch):
    # Without the fix the purchase goes ahead; keep that path off the network.
    monkeypatch.setattr(mgr, "_wait_for_stamp_usable", AsyncMock(return_value=True))
    monkeypatch.setattr("app.services.stamp_pool.swarm_api.get_all_stamps_processed",
                        AsyncMock(return_value=_bee(C)))
    monkeypatch.setattr(stamp_ownership_manager, "register_stamp", lambda **kw: None)
    hold = object()
    released = []
    monkeypatch.setattr(mgr, "_reserve_gateway_spend", lambda *a, **kw: hold)
    monkeypatch.setattr("app.services.spend_budget.spend_budget_tracker.release_hold",
                        lambda h: released.append(h))
    return hold, released


@pytest.mark.asyncio
async def test_purchase_does_not_spend_when_paused_during_price_lookup(tmp_path, monkeypatch):
    mgr = await _good_pool(tmp_path, monkeypatch, A, B)
    hold, released = _spend_mocks(mgr, monkeypatch)
    budget = mgr._spend_budget_remaining()

    async def chainstate():
        _break_file_and_release(mgr, A)
        return {"currentPrice": "24000"}

    buy = AsyncMock(return_value=C)
    with patch("app.services.stamp_pool.swarm_api.get_chainstate", new=chainstate), \
         patch("app.services.stamp_pool.swarm_api.purchase_postage_stamp", new=buy):
        assert await mgr._purchase_stamp(17) is None

    buy.assert_not_called()
    assert released == [hold]
    assert mgr._spend_budget_remaining() == budget
    assert _read(mgr) == CORRUPT


@pytest.mark.asyncio
async def test_purchase_retry_does_not_spend_when_paused_during_429_wait(tmp_path, monkeypatch):
    mgr = await _good_pool(tmp_path, monkeypatch, A, B)
    hold, released = _spend_mocks(mgr, monkeypatch)
    budget = mgr._spend_budget_remaining()

    async def wait(seconds):
        _break_file_and_release(mgr, A)

    monkeypatch.setattr("app.services.stamp_pool.asyncio.sleep", wait)
    buy = AsyncMock(side_effect=[_RateLimited(), C])
    with patch("app.services.stamp_pool.swarm_api.get_chainstate",
               new=AsyncMock(return_value={"currentPrice": "24000"})), \
         patch("app.services.stamp_pool.swarm_api.purchase_postage_stamp", new=buy):
        assert await mgr._purchase_stamp(17) is None

    assert buy.call_count == 1
    assert released == [hold]
    assert mgr._spend_budget_remaining() == budget


@pytest.mark.asyncio
async def test_immediate_replenishment_does_not_spend_when_paused_during_price_lookup(tmp_path, monkeypatch, caplog):
    mgr = await _good_pool(tmp_path, monkeypatch, A, B)
    hold, released = _spend_mocks(mgr, monkeypatch)
    budget = mgr._spend_budget_remaining()

    async def chainstate():
        _break_file_and_release(mgr, A)
        return {"currentPrice": "24000"}

    buy = AsyncMock(return_value=C)
    with caplog.at_level(logging.WARNING, logger="app.services.stamp_pool"), \
         patch("app.services.stamp_pool.swarm_api.get_chainstate", new=chainstate), \
         patch("app.services.stamp_pool.swarm_api.purchase_postage_stamp", new=buy):
        await mgr._async_replenish_one(17)

    buy.assert_not_called()
    # Positive evidence that the pause stopped it, rather than an error inside
    # the mock that _async_replenish_one would swallow.
    assert released == [hold]
    assert mgr._spend_budget_remaining() == budget
    assert any("Not going ahead to buy" in r.getMessage() for r in caplog.records)
    assert not any("Immediate replenishment failed" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_topup_does_not_spend_when_paused_during_price_lookup(tmp_path, monkeypatch):
    mgr = await _good_pool(tmp_path, monkeypatch, A, B)
    _spend_mocks(mgr, monkeypatch)
    budget = mgr._spend_budget_remaining()

    async def chainstate():
        _break_file_and_release(mgr, B)
        return {"currentPrice": "24000"}

    extend = AsyncMock()
    with patch("app.services.stamp_pool.swarm_api.get_chainstate", new=chainstate), \
         patch("app.services.stamp_pool.swarm_api.extend_postage_stamp", new=extend):
        assert await mgr._topup_stamp(A) is None

    extend.assert_not_called()
    assert mgr._spend_budget_remaining() == budget
    assert A not in mgr._topup_in_flight


# Log hygiene.

def test_save_with_missing_file_does_not_log_first_run(tmp_path, caplog):
    mgr = StampPoolManager(state_file=str(tmp_path / "pool_state.json"))
    mgr._pool[A] = PoolStamp(batch_id=A, depth=17, amount=1,
                             created_at=datetime.now(timezone.utc), ttl_at_creation=3600)
    with caplog.at_level(logging.INFO, logger="app.services.stamp_pool"):
        mgr._save_state()
    assert not any("first run" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_failed_write_on_recovery_sync_is_logged_once(tmp_path, monkeypatch, caplog):
    mgr = await _good_pool(tmp_path, monkeypatch, A, B)
    _corrupt(mgr)
    assert await _sync(mgr, A, B) == 0
    assert mgr.release_stamp(A) is not None
    _write(mgr, [A, B])

    with caplog.at_level(logging.ERROR, logger="app.services.stamp_pool"), \
         patch("app.services.stamp_pool.atomic_write_json", side_effect=OSError("disk full")):
        await _sync(mgr, A, B)

    failures = [r for r in caplog.records if "Failed to save pool state" in r.getMessage()]
    assert len(failures) == 1
    assert mgr._state_dirty is True


@pytest.mark.asyncio
async def test_file_breaking_before_the_sync_save_keeps_the_pause(tmp_path, monkeypatch):
    """The sync read the file, then it broke before the sync's own save."""
    D = "d" * 64
    mgr = await _good_pool(tmp_path, monkeypatch, A, B)
    _write(mgr, [A, B, D])  # D has gone from Bee, so the sync drops it and saves
    real_lookup = stamp_ownership_manager.get_stamp_info

    def lookup(batch_id):
        _corrupt(mgr)  # inside the sync's locked read, after its file read
        return real_lookup(batch_id)

    monkeypatch.setattr(stamp_ownership_manager, "get_stamp_info", lookup)
    await _sync(mgr, A, B)

    assert _read(mgr) == CORRUPT
    _assert_paused_for_file(mgr)
    assert mgr._needs_reconcile is True
    assert mgr._immediate_replenish_paused(17) is True


@pytest.mark.asyncio
async def test_refused_save_after_restore_and_bee_failure_says_not_reconciled(tmp_path, monkeypatch, caplog):
    mgr = await _good_pool(tmp_path, monkeypatch, A, B)
    _corrupt(mgr)
    assert await _sync(mgr, A, B) == 0
    _write(mgr, [A, B])
    with patch("app.services.stamp_pool.swarm_api.get_all_stamps_processed",
               new=AsyncMock(side_effect=RuntimeError("bee down"))):
        await mgr.sync_from_bee_node()

    with caplog.at_level(logging.ERROR, logger="app.services.stamp_pool"):
        assert mgr.release_stamp(A) is not None
    msgs = [r.getMessage() for r in caplog.records if "not saving pool state" in r.getMessage()]
    assert len(msgs) == 1
    assert "no sync has read it back" not in msgs[0]
    assert "has not been reconciled with a full sync since it was found unreadable" in msgs[0]


@pytest.mark.asyncio
async def test_topup_paused_mid_run_is_reported_as_paused(tmp_path, monkeypatch):
    mgr = await _good_pool(tmp_path, monkeypatch, A, B)
    _cycle_patches(mgr, monkeypatch, target=2)
    _spend_mocks(mgr, monkeypatch)
    monkeypatch.setattr(mgr, "_get_stamp_ttl", AsyncMock(return_value=1))

    async def chainstate():
        _break_file_and_release(mgr, B)
        return {"currentPrice": "24000"}

    extend = AsyncMock()
    with patch("app.services.stamp_pool.swarm_api.get_all_stamps_processed",
               new=AsyncMock(return_value=_bee(A, B))), \
         patch("app.services.stamp_pool.swarm_api.get_chainstate", new=chainstate), \
         patch("app.services.stamp_pool.swarm_api.extend_postage_stamp", new=extend):
        results = await mgr.check_and_replenish()

    extend.assert_not_called()
    assert results.get("skipped") is True
    [debug] = results["topup_debug"]
    assert "paused" in debug["result"] and "state file" in debug["result"]


# Ids kept as unreadable survive recovery while Bee still lists them.

X = "e" * 64


async def _pool_with_unreadable_x(tmp_path, monkeypatch):
    mgr = await _good_pool(tmp_path, monkeypatch, A, B)
    monkeypatch.setitem(stamp_ownership_manager._registry, X, {"owner": POOL_OWNER, "mode": "pool"})
    _write(mgr, [A, B, X])
    with patch("app.services.stamp_pool.swarm_api.get_all_stamps_processed",
               new=AsyncMock(return_value=_bee_x_unreadable())):
        await mgr.sync_from_bee_node()
    assert X not in mgr._pool and _state(mgr) == sorted([A, B, X])
    _corrupt(mgr)
    assert await _sync(mgr, A, B) == 0
    assert mgr.release_stamp(A) is not None
    return mgr


def _bee_x_unreadable(*ids):
    bee = _bee(*(ids or (A, B)), X)
    bee[-1]["depth"] = None
    return bee


@pytest.mark.asyncio
async def test_unreadable_id_survives_restore_of_older_backup(tmp_path, monkeypatch):
    mgr = await _pool_with_unreadable_x(tmp_path, monkeypatch)
    _write(mgr, [A, B])  # backup from before X was bought
    with patch("app.services.stamp_pool.swarm_api.get_all_stamps_processed",
               new=AsyncMock(return_value=_bee_x_unreadable())):
        await mgr.sync_from_bee_node()

    assert mgr._last_sync_ok is True
    assert _state(mgr) == sorted([B, X])


@pytest.mark.asyncio
async def test_unreadable_id_survives_file_moved_away(tmp_path, monkeypatch):
    mgr = await _pool_with_unreadable_x(tmp_path, monkeypatch)
    os.rename(_path(mgr), _path(mgr) + ".moved")
    await _sync(mgr)  # first-run path: Bee is not asked

    assert mgr._last_sync_ok is True
    assert _state(mgr) == sorted([B, X])


@pytest.mark.asyncio
async def test_carried_unreadable_id_dropped_once_bee_no_longer_lists_it(tmp_path, monkeypatch):
    mgr = await _pool_with_unreadable_x(tmp_path, monkeypatch)
    _write(mgr, [A, B])
    await _sync(mgr, A, B)  # X has expired
    assert _state(mgr) == [B]


@pytest.mark.asyncio
async def test_carried_unreadable_id_imported_once_readable(tmp_path, monkeypatch):
    mgr = await _pool_with_unreadable_x(tmp_path, monkeypatch)
    _write(mgr, [A, B])
    await _sync(mgr, A, B, X)  # X's record reads fine now
    assert X in mgr._pool
    assert _state(mgr) == sorted([B, X])


# Messages.

@pytest.mark.asyncio
async def test_immediate_replenishment_paused_does_not_also_warn_no_batch(tmp_path, monkeypatch, caplog):
    mgr = await _good_pool(tmp_path, monkeypatch, A, B)
    _spend_mocks(mgr, monkeypatch)

    async def chainstate():
        _break_file_and_release(mgr, A)
        return {"currentPrice": "24000"}

    with caplog.at_level(logging.WARNING, logger="app.services.stamp_pool"), \
         patch("app.services.stamp_pool.swarm_api.get_chainstate", new=chainstate), \
         patch("app.services.stamp_pool.swarm_api.purchase_postage_stamp", new=AsyncMock(return_value=C)):
        await mgr._async_replenish_one(17)

    assert any("Not going ahead to buy" in r.getMessage() for r in caplog.records)
    assert not any("returned no batch_id" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_bee_skip_while_unreconciled_says_saves_are_held(tmp_path, monkeypatch):
    mgr = await _good_pool(tmp_path, monkeypatch, A, B)
    _cycle_patches(mgr, monkeypatch, target=2)
    _corrupt(mgr)
    assert await _sync(mgr, A, B) == 0
    _write(mgr, [A, B])
    with patch("app.services.stamp_pool.swarm_api.get_all_stamps_processed",
               new=AsyncMock(side_effect=RuntimeError("bee down"))):
        results = await mgr.check_and_replenish()

    assert results.get("skipped") is True
    held = [e for e in results["errors"]
            if e.startswith("Skipping replenishment") and "saves stay held" in e]
    assert held, results["errors"]
    assert all(str(tmp_path) not in e for e in results["errors"])
    assert mgr._errors == results["errors"]


# Every recovery from a state-file pause writes the reconciled pool, even
# when nothing was saved (and refused) during the pause.

async def _paused_pool_no_saves(tmp_path, monkeypatch):
    mgr = await _good_pool(tmp_path, monkeypatch, A, B)
    _corrupt(mgr)
    assert await _sync(mgr, A, B) == 0
    _assert_paused_for_file(mgr)
    return mgr


def _recovered(caplog):
    return [r.getMessage() for r in caplog.records
            if r.levelno == logging.INFO and "pause lifted" in r.getMessage()]


@pytest.mark.asyncio
async def test_recovery_by_moving_file_away_writes_the_pool(tmp_path, monkeypatch, caplog):
    mgr = await _paused_pool_no_saves(tmp_path, monkeypatch)
    os.rename(_path(mgr), _path(mgr) + ".moved")
    with caplog.at_level(logging.INFO, logger="app.services.stamp_pool"):
        await _sync(mgr)

    assert mgr._last_sync_ok is True
    assert _state(mgr) == sorted(mgr._pool) == sorted([A, B])
    assert any("wrote 2 batch ids" in m for m in _recovered(caplog))
    # A restart now starts from the pool, not empty.
    fresh = StampPoolManager(state_file=_path(mgr))
    assert fresh._load_state() == {A, B}


@pytest.mark.asyncio
async def test_recovery_from_older_backup_writes_the_pool(tmp_path, monkeypatch, caplog):
    mgr = await _paused_pool_no_saves(tmp_path, monkeypatch)
    _write(mgr, [A])
    with caplog.at_level(logging.INFO, logger="app.services.stamp_pool"):
        await _sync(mgr, A, B)

    assert mgr._last_sync_ok is True
    assert _state(mgr) == sorted(mgr._pool) == sorted([A, B])
    assert any("wrote 2 batch ids" in m for m in _recovered(caplog))


@pytest.mark.asyncio
async def test_recovery_from_empty_list_writes_the_pool(tmp_path, monkeypatch, caplog):
    mgr = await _paused_pool_no_saves(tmp_path, monkeypatch)
    _write(mgr, [])
    with caplog.at_level(logging.INFO, logger="app.services.stamp_pool"):
        await _sync(mgr)

    assert mgr._last_sync_ok is True
    assert _state(mgr) == sorted(mgr._pool) == sorted([A, B])
    assert any("wrote 2 batch ids" in m for m in _recovered(caplog))


@pytest.mark.asyncio
async def test_sync_without_a_pause_does_not_rewrite_the_file(tmp_path, monkeypatch, caplog):
    mgr = await _good_pool(tmp_path, monkeypatch, A, B)
    with open(_path(mgr), "w") as f:
        json.dump([A, B], f, indent=2)  # a format our own save never writes
    before = _read(mgr)
    with caplog.at_level(logging.INFO, logger="app.services.stamp_pool"):
        await _sync(mgr, A, B)
    assert _read(mgr) == before
    assert mgr._state_dirty is False
    assert not _recovered(caplog)
