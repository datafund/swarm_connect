"""Pool acquire: only preset depths, allowance charged for what is handed out,
and a paid acquire priced from the same parse it is served from (#351, #362).
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.services.pool_allowance import PoolAllowanceTracker


@pytest.fixture
def pool(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STAMP_POOL_ENABLED", True)
    monkeypatch.setattr(settings, "POOL_DEFAULT_DAILY_ALLOWANCE", 1)
    monkeypatch.setattr(settings, "POOL_DAILY_ALLOWANCES", "")
    tracker = PoolAllowanceTracker(state_file=str(tmp_path / "allow.json"))
    import app.api.endpoints.pool as pool_ep
    monkeypatch.setattr(pool_ep, "pool_allowance_tracker", tracker)
    inventory = {"available": [20]}  # depths currently in the pool
    handed = []

    def exact(depth):
        return SimpleNamespace(batch_id=f"{len(handed):064x}", depth=depth) if depth in inventory["available"] else None

    def any_size(min_depth=17):
        bigger = sorted(d for d in inventory["available"] if d >= min_depth)
        return SimpleNamespace(batch_id=f"{len(handed):064x}", depth=bigger[0]) if bigger else None

    def release(batch_id, released_to=None):
        d = next(d for d in inventory["available"])
        handed.append(d)
        return SimpleNamespace(batch_id=batch_id, depth=d)

    mgr = pool_ep.stamp_pool_manager
    with patch.object(mgr, "get_available_stamp", side_effect=exact), \
         patch.object(mgr, "get_available_stamp_any_size", side_effect=any_size), \
         patch.object(mgr, "release_stamp", side_effect=release), \
         patch.object(mgr, "trigger_replenishment_if_needed", return_value=False), \
         patch.object(pool_ep.stamp_ownership_manager, "register_stamp"):
        yield SimpleNamespace(tracker=tracker, handed=handed, inventory=inventory)


@pytest.mark.parametrize("depth", [-5, 0, 1, 16, 18, 19, 21, 23, 32])
def test_depths_outside_the_pool_sizes_are_rejected(pool, depth):
    r = TestClient(app).post("/api/v1/pool/acquire", json={"depth": depth})
    assert r.status_code == 422
    assert pool.handed == []


def test_allowance_is_charged_for_the_size_handed_out(pool):
    client = TestClient(app)
    # Only a medium batch is available: a small request is served with it and
    # charged to the medium bucket.
    r = client.post("/api/v1/pool/acquire", json={"size": "small"})
    assert r.status_code == 200 and r.json()["fallback_used"] is True
    assert pool.tracker.snapshot()["used"] == {"(unlisted)|medium": 1}
    # The medium allowance (1) is spent, so the next small request that would
    # again be served a medium batch is refused rather than drawing on "small".
    r = client.post("/api/v1/pool/acquire", json={"size": "small"})
    assert r.status_code == 429
    assert r.json()["detail"]["size"] == "medium"


def test_paid_acquire_is_not_served_a_larger_batch(pool, monkeypatch):
    import app.api.endpoints.pool as pool_ep
    from app.api.endpoints.pool import AcquireStampRequest, acquire_stamp
    req = SimpleNamespace(headers={}, client=None,
                          state=SimpleNamespace(x402_mode="paid", x402_payer="0xp"))
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(acquire_stamp(AcquireStampRequest(size="small"), req))
    assert exc.value.status_code == 409
    assert pool.handed == []


def test_pool_price_uses_the_same_parse_as_the_handler(monkeypatch):
    """A string depth is coerced by the model, so it must be priced as that depth."""
    from app.x402 import dependency

    async def quote(operation, duration_hours, depth):
        return {"price_usd": float(2 ** depth)}

    class Req:
        url = SimpleNamespace(path="/api/v1/pool/acquire")
        method = "POST"
        async def json(self):
            return {"depth": "22"}

    monkeypatch.setattr(dependency, "get_price_quote", quote)
    monkeypatch.setattr(settings, "X402_POOL_MARKUP_PERCENT", 0)
    price = asyncio.run(dependency._calculate_price_for_request(Req()))
    assert price["price_usd"] == float(2 ** 22)
