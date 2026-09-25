"""POST /stamps/ is priced from what it buys (#361).

The quote used to be a fixed 24h depth-17 batch whatever the body asked for,
while the handler bought the requested depth and duration or legacy amount.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.core.config import settings
from app.services import swarm_api
from app.x402 import dependency
from app.x402.pricing import _price_from_cost_bzz

CHAINSTATE = {"currentPrice": "24000", "minimumValidityBlocks": 17280}


def _quote(body):
    class Req:
        url = SimpleNamespace(path="/api/v1/stamps/")
        method = "POST"
        scope = {"method": "POST"}

        async def json(self):
            if isinstance(body, Exception):
                raise body
            return body

    with patch("app.x402.pricing.get_chainstate", new=AsyncMock(return_value=CHAINSTATE)):
        return asyncio.run(dependency._calculate_price_for_request(Req()))


def _bought(depth, duration_hours=None, amount=None):
    """What purchase_stamp spends for these parameters, priced the same way."""
    if amount is None:
        amount = swarm_api.calculate_stamp_amount(duration_hours or 25, 24000, minimum_validity_blocks=17280)
    return _price_from_cost_bzz(swarm_api.plur_to_bzz(swarm_api.calculate_stamp_total_cost(amount, depth)))["price_usd"]


@pytest.mark.parametrize("body,depth,duration,amount", [
    ({}, 17, None, None),
    ({"size": "medium"}, 20, None, None),
    ({"depth": 22, "duration_hours": 168}, 22, 168, None),
    ({"depth": "22", "duration_hours": "48"}, 22, 48, None),   # coerced by the model
    ({"size": "large", "depth": 18}, 22, None, None),           # size wins, as in the model
    ({"depth": 20, "amount": 10_000_000_000}, 20, None, 10_000_000_000),
])
def test_quote_matches_the_batch_bought(monkeypatch, body, depth, duration, amount):
    monkeypatch.setattr(settings, "X402_MIN_PRICE_USD", 0.0)
    assert _quote(body)["price_usd"] == _bought(depth, duration, amount)
    assert f"depth {depth}" in _quote(body)["description"]


def test_larger_batches_cost_more(monkeypatch):
    monkeypatch.setattr(settings, "X402_MIN_PRICE_USD", 0.0)
    small = _quote({"depth": 17, "duration_hours": 24})["price_usd"]
    big = _quote({"depth": 22, "duration_hours": 168})["price_usd"]
    assert big >= small * 32 * 6


@pytest.mark.parametrize("body", [ValueError("not json"), {"depth": 99}, {"duration_hours": 1}])
def test_unacceptable_bodies_price_at_the_defaults(body):
    # The endpoint rejects these with 422, so this quote is never charged.
    assert "depth 17" in _quote(body)["description"]
