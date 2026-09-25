"""Clients are told when their data can expire, and where the terms are (#383)."""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from app.main import app
from app.services import swarm_api

BATCH = "a" * 64


def test_expiry_from_amount_is_duration_at_todays_price():
    # 17280 blocks of 5 s = 24 h
    iso = swarm_api.expiry_from_amount(24000 * 17280, 24000)
    hours = (datetime.fromisoformat(iso) - datetime.now(timezone.utc)).total_seconds() / 3600
    assert 23.9 < hours < 24.1
    assert swarm_api.expiry_from_amount(1, 0) is None


def _bee(status, body):
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(status, json=body)))


def test_batch_expiry_from_the_nodes_ttl():
    with patch("app.services.swarm_api.get_client", return_value=_bee(200, {"batchTTL": 7200})):
        iso = asyncio.run(swarm_api.get_batch_expiry(BATCH))
    hours = (datetime.fromisoformat(iso) - datetime.now(timezone.utc)).total_seconds() / 3600
    assert 1.9 < hours < 2.1


def test_batch_expiry_unknown_is_none():
    with patch("app.services.swarm_api.get_client", return_value=_bee(404, {})):
        assert asyncio.run(swarm_api.get_batch_expiry(BATCH)) is None


def test_purchase_response_carries_expiry():
    with patch("app.services.swarm_api.get_chainstate",
               new=AsyncMock(return_value={"currentPrice": "24000", "minimumValidityBlocks": 17280})), \
         patch("app.services.swarm_api.check_sufficient_funds",
               new=AsyncMock(return_value={"sufficient": True, "wallet_balance_bzz": 10.0,
                                           "required_bzz": 0.1, "shortfall_bzz": 0.0})), \
         patch("app.services.swarm_api.purchase_postage_stamp", new=AsyncMock(return_value=BATCH)):
        r = TestClient(app).post("/api/v1/stamps/", json={"duration_hours": 48})
    assert r.status_code == 201
    hours = (datetime.fromisoformat(r.json()["expires_at"]) - datetime.now(timezone.utc)).total_seconds() / 3600
    assert 47 < hours < 51   # 48 h plus the 5% margin


def test_root_links_the_terms():
    with patch("app.services.swarm_api.get_node_status_summary",
               new=AsyncMock(return_value={"healthy": True, "warnings": []})):
        body = TestClient(app).get("/").json()
    assert body["terms_url"].endswith("TERMS.md")
