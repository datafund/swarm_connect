"""State that must survive a restart is loaded by the app itself (#349).

The ownership registry was persisted on every change but never loaded: each
restart started empty, owners were denied their own batches, and the pool's
startup sync rewrote the file with only its own entries. These tests start the
real application lifespan against a populated state file.
"""
import json

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.services.stamp_ownership import stamp_ownership_manager

OWNER = "0x1111111111111111111111111111111111111111"
BATCH = "a" * 64


@pytest.fixture
def ownership_file(tmp_path, monkeypatch):
    path = str(tmp_path / "stamp_owners.json")
    monkeypatch.setattr(settings, "STAMP_OWNERSHIP_FILE", path)
    monkeypatch.setattr(settings, "STAMP_POOL_ENABLED", False)
    monkeypatch.setattr(settings, "METRICS_ENABLED", False)
    saved = dict(stamp_ownership_manager._registry)
    stamp_ownership_manager._registry = {}
    yield path
    stamp_ownership_manager._registry = saved


def test_registry_is_loaded_when_the_app_starts(ownership_file, monkeypatch):
    monkeypatch.setattr(settings, "X402_ENABLED", True)
    with open(ownership_file, "w") as f:
        json.dump({BATCH: {"owner": OWNER, "mode": "paid", "acquired_at": "x", "source": "direct_purchase"}}, f)

    with TestClient(app):
        allowed, reason = stamp_ownership_manager.check_access(BATCH, OWNER, "paid")

    assert allowed, reason


def test_a_registration_after_start_keeps_earlier_records(ownership_file):
    with open(ownership_file, "w") as f:
        json.dump({BATCH: {"owner": OWNER, "mode": "paid", "acquired_at": "x", "source": "direct_purchase"}}, f)

    with TestClient(app):
        stamp_ownership_manager.register_stamp("b" * 64, owner="pool", mode="pool", source="pool_sync")

    on_disk = json.load(open(ownership_file))
    assert set(on_disk) == {BATCH, "b" * 64}


def test_an_unreadable_registry_stops_startup(ownership_file):
    from app.core.atomic_io import StateLoadError
    with open(ownership_file, "w") as f:
        f.write("not json")

    with pytest.raises(StateLoadError):
        with TestClient(app):
            pass
    assert open(ownership_file).read() == "not json"


def test_pool_sync_does_not_reclaim_an_owned_batch(ownership_file):
    """A batch still listed in pool state but registered to a caller stays theirs."""
    from app.services.stamp_pool import stamp_pool_manager
    with open(ownership_file, "w") as f:
        json.dump({BATCH: {"owner": OWNER, "mode": "paid", "acquired_at": "x", "source": "pool_acquire"}}, f)

    with TestClient(app):
        stamp_pool_manager._register_pool_ownership({BATCH, "c" * 64})
        assert stamp_ownership_manager.get_stamp_info(BATCH)["owner"] == OWNER
        assert stamp_ownership_manager.get_stamp_info("c" * 64)["owner"] == "pool"
