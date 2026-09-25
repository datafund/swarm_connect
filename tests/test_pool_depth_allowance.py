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
    # The medium allowance (1) is spent, so the next small request, which could
    # only be served a medium batch, is told small is unavailable (not charged
    # to "small", and not blamed on a medium allowance it never asked for).
    r = client.post("/api/v1/pool/acquire", json={"size": "small"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "REQUESTED_SIZE_UNAVAILABLE"
    assert r.json()["detail"]["size"] == "small"
    # An explicit medium request is the one that hits the medium allowance.
    r = client.post("/api/v1/pool/acquire", json={"size": "medium"})
    assert r.status_code == 429 and r.json()["detail"]["size"] == "medium"


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


def test_paid_acquire_of_an_available_size_uses_no_allowance(pool, monkeypatch):
    from app.api.endpoints.pool import AcquireStampRequest, acquire_stamp
    monkeypatch.setattr(settings, "X402_NETWORK", "base")  # paid bypass honoured
    req = SimpleNamespace(headers={}, client=None,
                          state=SimpleNamespace(x402_mode="paid", x402_payer="0xp"))
    resp = asyncio.run(acquire_stamp(AcquireStampRequest(size="medium"), req))
    assert resp.depth == 20
    assert pool.tracker.snapshot()["used"] == {}


def test_refused_paid_acquire_uses_no_allowance(pool):
    from app.api.endpoints.pool import AcquireStampRequest, acquire_stamp
    from fastapi import HTTPException
    req = SimpleNamespace(headers={}, client=None,
                          state=SimpleNamespace(x402_mode="paid", x402_payer="0xp"))
    with pytest.raises(HTTPException):
        asyncio.run(acquire_stamp(AcquireStampRequest(size="small"), req))
    assert pool.tracker.snapshot()["used"] == {}


def test_one_client_cannot_take_a_whole_origin_bucket(pool, monkeypatch):
    """#366: the per-address sub-limit inside an origin's allowance."""
    monkeypatch.setattr(settings, "POOL_DEFAULT_DAILY_ALLOWANCE", 10)
    monkeypatch.setattr(settings, "POOL_ALLOWANCE_PER_IP", 2)
    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)
    pool.inventory["available"] = [17]
    client = TestClient(app)

    def take(ip):
        return client.post("/api/v1/pool/acquire", json={"size": "small"},
                           headers={"X-Forwarded-For": ip, "Origin": "https://partner.example"}).status_code

    assert [take("198.51.100.1") for _ in range(3)] == [200, 200, 429]
    assert take("198.51.100.2") == 200   # another client of the same origin still can


def test_exhausted_allowance_offers_payment_when_it_would_bypass(pool, monkeypatch):
    """#374: 402 with accepts, so x402 clients can pay instead of seeing a 429."""
    from unittest.mock import AsyncMock
    monkeypatch.setattr(settings, "X402_ENABLED", True)
    monkeypatch.setattr(settings, "X402_NETWORK", "base")   # mainnet: payment bypasses the allowance
    monkeypatch.setattr(settings, "X402_PAY_TO_ADDRESS", "0xc87688A40CE2ff1765BA54497c7471c892755488")
    pool.inventory["available"] = [17]
    pool.tracker.consume(None, "small")                       # allowance (1) already used
    with patch("app.x402.dependency._calculate_price_for_request",
               new=AsyncMock(return_value={"price_usd": 0.05, "description": "Pooled stamp"})):
        r = TestClient(app).post("/api/v1/pool/acquire", json={"size": "small"})
    assert r.status_code == 402
    body = r.json()["detail"]
    assert body["x402Version"] == 1 and body["accepts"][0]["maxAmountRequired"] == "50000"
    assert body["code"] == "DAILY_STAMP_ALLOWANCE_EXHAUSTED"


def test_exhausted_allowance_on_a_testnet_stays_429(pool, monkeypatch):
    monkeypatch.setattr(settings, "X402_ENABLED", True)
    monkeypatch.setattr(settings, "X402_NETWORK", "base-sepolia")
    monkeypatch.setattr(settings, "X402_ALLOW_TESTNET_PAID_BYPASS", False)
    pool.inventory["available"] = [17]
    pool.tracker.consume(None, "small")
    assert TestClient(app).post("/api/v1/pool/acquire", json={"size": "small"}).status_code == 429


def _mainnet_x402(monkeypatch):
    monkeypatch.setattr(settings, "X402_ENABLED", True)
    monkeypatch.setattr(settings, "X402_NETWORK", "base")
    monkeypatch.setattr(settings, "X402_PAY_TO_ADDRESS", "0xc87688A40CE2ff1765BA54497c7471c892755488")


def test_a_pricing_failure_still_answers_429(pool, monkeypatch):
    """The 402 needs a price; if pricing fails the caller still gets the 429, not a 500."""
    from unittest.mock import AsyncMock
    _mainnet_x402(monkeypatch)
    pool.inventory["available"] = [17]
    pool.tracker.consume(None, "small")
    with patch("app.x402.dependency._calculate_price_for_request",
               new=AsyncMock(side_effect=RuntimeError("price feed down"))):
        r = TestClient(app).post("/api/v1/pool/acquire", json={"size": "small"})
    assert r.status_code == 429
    assert r.json()["detail"]["code"] == "DAILY_STAMP_ALLOWANCE_EXHAUSTED"


def test_per_client_exhaustion_offers_payment_too(pool, monkeypatch):
    """Both kinds of exhaustion are bypassed by paying, so both answer 402."""
    from unittest.mock import AsyncMock
    _mainnet_x402(monkeypatch)
    monkeypatch.setattr(settings, "POOL_DEFAULT_DAILY_ALLOWANCE", 10)
    monkeypatch.setattr(settings, "POOL_ALLOWANCE_PER_IP", 1)
    pool.inventory["available"] = [17]
    client = TestClient(app)
    with patch("app.x402.dependency._calculate_price_for_request",
               new=AsyncMock(return_value={"price_usd": 0.05, "description": "Pooled stamp"})):
        assert client.post("/api/v1/pool/acquire", json={"size": "small"}).status_code == 200
        r = client.post("/api/v1/pool/acquire", json={"size": "small"})
    assert r.status_code == 402
    body = r.json()["detail"]
    assert body["code"] == "DAILY_STAMP_ALLOWANCE_PER_CLIENT_EXHAUSTED"
    assert body["alternative"]["header"] == "X-PAYMENT"


def test_per_client_exhaustion_on_a_fallback_reports_the_size_unavailable(pool, monkeypatch):
    monkeypatch.setattr(settings, "POOL_DEFAULT_DAILY_ALLOWANCE", 10)
    monkeypatch.setattr(settings, "POOL_ALLOWANCE_PER_IP", 1)
    pool.inventory["available"] = [20]           # only medium: a small request falls back
    client = TestClient(app)
    assert client.post("/api/v1/pool/acquire", json={"size": "small"}).status_code == 200
    r = client.post("/api/v1/pool/acquire", json={"size": "small"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "REQUESTED_SIZE_UNAVAILABLE"


def test_an_empty_pool_is_reported_before_a_spent_allowance(pool, monkeypatch):
    """No 402 for a size that is not in stock: paying would not help."""
    _mainnet_x402(monkeypatch)
    pool.inventory["available"] = []
    pool.tracker.consume(None, "small")
    r = TestClient(app).post("/api/v1/pool/acquire", json={"size": "small"})
    assert r.status_code == 409


def test_a_paid_acquire_leaves_the_per_client_counter_alone(pool, monkeypatch):
    monkeypatch.setattr(settings, "X402_NETWORK", "base")
    monkeypatch.setattr(settings, "POOL_ALLOWANCE_PER_IP", 1)
    pool.inventory["available"] = [17]
    from app.api.endpoints.pool import acquire_stamp, AcquireStampRequest
    req = SimpleNamespace(headers={}, client=SimpleNamespace(host="198.51.100.7"),
                          state=SimpleNamespace(x402_mode="paid", x402_payer="0xp"))
    asyncio.run(acquire_stamp(AcquireStampRequest(size="small"), req))
    assert pool.tracker.snapshot()["used"] == {}


def test_no_per_client_keys_are_written_when_the_limit_is_off(pool, monkeypatch):
    monkeypatch.setattr(settings, "POOL_ALLOWANCE_PER_IP", -1)
    pool.inventory["available"] = [17]
    assert TestClient(app).post("/api/v1/pool/acquire", json={"size": "small"}).status_code == 200
    assert pool.tracker.snapshot()["used"] == {"(unlisted)|small": 1}
