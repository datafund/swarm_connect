# tests/test_for_owner_pricing.py
"""
The x402 price for POST /api/v1/stamps/for-owner must reflect the ACTUAL requested
batch (depth + duration from the body), not the fixed /stamps/ default (#229).
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.x402 import dependency


def _req(path, body):
    async def _json():
        return body
    return SimpleNamespace(url=SimpleNamespace(path=path), json=_json,
                           query_params={}, headers={})


@pytest.mark.asyncio
async def test_for_owner_prices_requested_depth_and_duration():
    captured = {}

    async def _quote(operation, duration_hours=None, depth=None, **kw):
        captured.update(operation=operation, duration_hours=duration_hours, depth=depth)
        return {"price_usd": 0.42}

    with patch.object(dependency, "get_price_quote", AsyncMock(side_effect=_quote)):
        res = await dependency._calculate_price_for_request(
            _req("/api/v1/stamps/for-owner", {"owner": "0x" + "1" * 40, "size": "medium", "duration_hours": 72})
        )
    assert res["price_usd"] == 0.42
    assert captured == {"operation": "stamp_purchase", "duration_hours": 72, "depth": 20}  # medium=20


@pytest.mark.asyncio
async def test_for_owner_explicit_depth_overrides_default_duration():
    captured = {}

    async def _quote(operation, duration_hours=None, depth=None, **kw):
        captured.update(duration_hours=duration_hours, depth=depth)
        return {"price_usd": 1.0}

    with patch.object(dependency, "get_price_quote", AsyncMock(side_effect=_quote)):
        await dependency._calculate_price_for_request(
            _req("/api/v1/stamps/for-owner", {"owner": "0x" + "1" * 40, "depth": 22})
        )
    assert captured == {"duration_hours": 24, "depth": 22}  # depth from body, duration defaulted


@pytest.mark.asyncio
async def test_for_owner_falls_back_on_unreadable_body():
    async def _bad_json():
        raise ValueError("no body")

    req = SimpleNamespace(url=SimpleNamespace(path="/api/v1/stamps/for-owner"),
                          json=_bad_json, query_params={}, headers={})
    with patch.object(dependency, "get_price_quote", AsyncMock(return_value={"price_usd": 0.1})):
        res = await dependency._calculate_price_for_request(req)
    assert res["price_usd"] == 0.1  # default depth 17 / 24h, no crash
