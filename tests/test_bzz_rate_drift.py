"""The BZZ/USD pricing rate is watched against the market (#364)."""
import asyncio
import json
import os
from unittest.mock import patch

import httpx
import pytest

from app.core.config import settings
from app.services import metrics

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.mark.parametrize("doc,expected", [
    ({"swarm-bzz": {"usd": 0.041}}, 0.041),       # CoinGecko simple/price
    ({"data": [{"quote": {"usd": 0.05}}]}, 0.05),
    ({"price": {"eur": 1}}, None),
    ({"usd": True}, None),
])
def test_first_usd_value(doc, expected):
    assert metrics._first_usd_value(doc) == expected


def _feed(price_doc):
    def handler(request):
        return httpx.Response(200, json=price_doc)
    real = httpx.AsyncClient
    return patch("httpx.AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def test_rates_are_exported(monkeypatch):
    monkeypatch.setattr(settings, "X402_BZZ_USD_RATE", 0.5)
    monkeypatch.setattr(settings, "X402_BZZ_PRICE_FEED_URL", "https://feed.example/price")
    monkeypatch.setattr(metrics, "_last_price_fetch", 0.0)
    with _feed({"swarm-bzz": {"usd": 0.041}}):
        asyncio.run(metrics._update_bzz_rates())
    assert metrics.bzz_usd_rate_configured._value.get() == 0.5
    assert metrics.bzz_usd_rate_market._value.get() == 0.041


def test_no_feed_means_no_request(monkeypatch):
    monkeypatch.setattr(settings, "X402_BZZ_PRICE_FEED_URL", "")
    with patch("httpx.AsyncClient", side_effect=AssertionError("fetched")):
        asyncio.run(metrics._update_bzz_rates())


def test_a_failing_feed_is_harmless(monkeypatch):
    monkeypatch.setattr(settings, "X402_BZZ_PRICE_FEED_URL", "https://feed.example/price")
    monkeypatch.setattr(metrics, "_last_price_fetch", 0.0)
    with patch("httpx.AsyncClient", side_effect=RuntimeError("down")):
        asyncio.run(metrics._update_bzz_rates())


def test_drift_alert_rule_is_defined():
    rules = json.load(open(os.path.join(REPO, "monitoring/alerting/alert-rules.json")))
    rule = next(r for r in rules if r["title"] == "BZZ pricing rate off market")
    assert rule["data"][0]["model"]["expr"] == "gateway_bzz_usd_rate_configured / gateway_bzz_usd_rate_market"
    assert rule["data"][2]["model"]["conditions"][0]["evaluator"] == {"params": [0.5, 2], "type": "outside_range"}
