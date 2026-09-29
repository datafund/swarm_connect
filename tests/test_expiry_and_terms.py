"""Clients are told when their data can expire, and where the terms are (#383)."""
import asyncio
import io
import tarfile
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.services import swarm_api

BATCH = "a" * 64
FUNDS_OK = {"sufficient": True, "wallet_balance_bzz": 10.0, "required_bzz": 0.1, "shortfall_bzz": 0.0}


def _hours_until(iso):
    assert iso.endswith("Z") and "." not in iso, iso
    return (datetime.fromisoformat(iso.replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds() / 3600


def test_expiry_from_amount_is_duration_at_todays_price():
    # 17280 blocks of 5 s = 24 h
    assert 23.9 < _hours_until(swarm_api.expiry_from_amount(24000 * 17280, 24000)) < 24.1
    assert swarm_api.expiry_from_amount(1, 0) is None


def _run_expiry(handler, **kw):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch("app.services.swarm_api.get_client", return_value=client):
                return await swarm_api.get_batch_expiry(BATCH, **kw)
    return asyncio.run(go())


def test_batch_expiry_from_the_nodes_ttl():
    iso = _run_expiry(lambda r: httpx.Response(200, json={"batchTTL": 7200}))
    assert 1.9 < _hours_until(iso) < 2.1


@pytest.mark.parametrize("body,status", [
    ({}, 404), ({"batchTTL": -1}, 200), ({"batchTTL": 0}, 200), ({"batchTTL": "soon"}, 200),
])
def test_batch_expiry_unknown_is_none(body, status):
    assert _run_expiry(lambda r: httpx.Response(status, json=body)) is None


def test_batch_expiry_never_holds_up_the_response():
    async def slow(request):
        await asyncio.sleep(2)
        return httpx.Response(200, json={"batchTTL": 60})
    started = datetime.now()
    assert _run_expiry(slow, timeout=0.2) is None
    assert (datetime.now() - started).total_seconds() < 1.5


def _chainstate(**value):
    return patch("app.services.swarm_api.get_chainstate", new=AsyncMock(**value))


def test_purchase_response_carries_expiry():
    with _chainstate(return_value={"currentPrice": "24000", "minimumValidityBlocks": 17280}), \
         patch("app.services.swarm_api.check_sufficient_funds", new=AsyncMock(return_value=FUNDS_OK)), \
         patch("app.services.swarm_api.purchase_postage_stamp", new=AsyncMock(return_value=BATCH)):
        r = TestClient(app).post("/api/v1/stamps/", json={"duration_hours": 48})
    assert r.status_code == 201
    assert 47 < _hours_until(r.json()["expires_at"]) < 51   # 48 h plus the 5% margin


def test_legacy_amount_purchase_succeeds_when_the_price_is_unavailable_afterwards():
    with _chainstate(side_effect=RuntimeError("bee busy")), \
         patch("app.services.swarm_api.check_sufficient_funds", new=AsyncMock(return_value=FUNDS_OK)), \
         patch("app.services.swarm_api.purchase_postage_stamp", new=AsyncMock(return_value=BATCH)):
        r = TestClient(app).post("/api/v1/stamps/", json={"amount": 500000000})
    assert r.status_code == 201
    assert r.json()["expires_at"] is None


@pytest.fixture
def owned(monkeypatch):
    from app.services.stamp_ownership import stamp_ownership_manager
    monkeypatch.setitem(stamp_ownership_manager._registry, BATCH, {"owner": "shared", "mode": "free"})


@pytest.mark.parametrize("expiry", ["2026-10-01T00:00:00Z", None])
def test_upload_reports_the_stamps_expiry(owned, expiry):
    with patch("app.api.endpoints.data.upload_data_to_swarm", new=AsyncMock(return_value="e" * 64)), \
         patch("app.api.endpoints.data.get_batch_expiry", new=AsyncMock(return_value=expiry)):
        r = TestClient(app).post(f"/api/v1/data/?stamp_id={BATCH}",
                                 files={"file": ("a.json", b'{"a":1}', "application/json")})
    assert r.status_code == 200
    assert r.json()["expires_at"] == expiry


def test_manifest_upload_reports_the_stamps_expiry(owned):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo("index.html")
        data = b"<p>x</p>"
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    with patch("app.api.endpoints.data.upload_collection_to_swarm", new=AsyncMock(return_value="e" * 64)), \
         patch("app.api.endpoints.data.get_batch_expiry", new=AsyncMock(return_value="2026-10-01T00:00:00Z")):
        r = TestClient(app).post(f"/api/v1/data/manifest?stamp_id={BATCH}",
                                 files={"file": ("site.tar", buf.getvalue(), "application/x-tar")})
    assert r.status_code == 200, r.text
    assert r.json()["expires_at"] == "2026-10-01T00:00:00Z"


def test_pool_acquire_reports_the_stamps_expiry(monkeypatch):
    from types import SimpleNamespace
    import app.api.endpoints.pool as pool_ep
    monkeypatch.setattr(settings, "STAMP_POOL_ENABLED", True)
    stamp = SimpleNamespace(batch_id=BATCH, depth=17)
    mgr = pool_ep.stamp_pool_manager
    with patch.object(mgr, "get_available_stamp", return_value=stamp), \
         patch.object(mgr, "release_stamp", return_value=stamp), \
         patch.object(mgr, "trigger_replenishment_if_needed", return_value=False), \
         patch.object(pool_ep.stamp_ownership_manager, "register_stamp"), \
         patch("app.api.endpoints.pool.get_batch_expiry", new=AsyncMock(return_value="2026-10-02T00:00:00Z")):
        r = TestClient(app).post("/api/v1/pool/acquire", json={"size": "small"})
    assert r.status_code == 200
    assert r.json()["expires_at"] == "2026-10-02T00:00:00Z"


def _root():
    with patch("app.services.swarm_api.get_node_status_summary",
               new=AsyncMock(return_value={"healthy": True, "warnings": []})):
        return TestClient(app).get("/").json()


def test_terms_are_not_linked_until_configured():
    assert _root()["terms_url"] is None


def test_root_links_the_configured_terms(monkeypatch):
    monkeypatch.setattr(settings, "TERMS_URL", "https://example.org/terms-v1")
    assert _root()["terms_url"] == "https://example.org/terms-v1"
