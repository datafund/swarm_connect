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


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    # None, not 0.0: the throttle compares against time.monotonic(), which is
    # time since boot, so 0.0 reads as "fetched at boot" and suppresses the
    # first read on any host with under 900s of uptime — every CI runner. This
    # suite passed on a developer machine with days of uptime and failed in CI.
    monkeypatch.setattr(metrics, "_last_price_fetch", None)
    monkeypatch.setattr(metrics, "_price_feed_failures", 0)
    monkeypatch.setattr(settings, "X402_BZZ_PRICE_FEED_INTERVAL_SECONDS", 900)
    metrics.bzz_usd_rate_market.set(0)
    yield
    metrics.bzz_usd_rate_market.set(0)


def _feed(price_doc):
    def handler(request):
        return httpx.Response(200, json=price_doc)
    real = httpx.AsyncClient
    return patch("httpx.AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def test_rates_are_exported(monkeypatch):
    monkeypatch.setattr(settings, "X402_BZZ_USD_RATE", 0.5)
    monkeypatch.setattr(settings, "X402_BZZ_PRICE_FEED_URL", "https://feed.example/price")
    monkeypatch.setattr(metrics, "_last_price_fetch", None)
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
    monkeypatch.setattr(metrics, "_last_price_fetch", None)
    with patch("httpx.AsyncClient", side_effect=RuntimeError("down")):
        asyncio.run(metrics._update_bzz_rates())


def test_drift_alert_rule_is_defined():
    rules = json.load(open(os.path.join(REPO, "monitoring/alerting/alert-rules.json")))
    rule = next(r for r in rules if r["title"] == "BZZ pricing rate off market")
    # "> 0" filters out the market gauge when there is no (working) feed, so the
    # rule has no data instead of dividing by zero and firing on +Inf.
    assert rule["data"][0]["model"]["expr"] == "gateway_bzz_usd_rate_configured / (gateway_bzz_usd_rate_market > 0)"
    assert rule["noDataState"] == "OK"
    assert rule["data"][2]["model"]["conditions"][0]["evaluator"] == {"params": [0.5, 2], "type": "outside_range"}



def test_without_a_feed_the_market_gauge_stays_zero(monkeypatch):
    monkeypatch.setattr(settings, "X402_BZZ_PRICE_FEED_URL", "")
    asyncio.run(metrics._update_bzz_rates())
    assert metrics.bzz_usd_rate_market._value.get() == 0


def test_reads_are_throttled(monkeypatch):
    monkeypatch.setattr(settings, "X402_BZZ_PRICE_FEED_URL", "https://feed.example/price")
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"usd": 0.04})
    real = httpx.AsyncClient
    with patch("httpx.AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)):
        asyncio.run(metrics._update_bzz_rates())
        asyncio.run(metrics._update_bzz_rates())
    assert len(calls) == 1


def test_drift_is_logged_as_a_warning(monkeypatch, caplog):
    monkeypatch.setattr(settings, "X402_BZZ_USD_RATE", 0.5)
    monkeypatch.setattr(settings, "X402_BZZ_PRICE_FEED_URL", "https://feed.example/price")
    with _feed({"usd": 0.041}), caplog.at_level("WARNING"):
        asyncio.run(metrics._update_bzz_rates())
    assert any("review X402_BZZ_USD_RATE" in r.message for r in caplog.records)


@pytest.mark.parametrize("doc", [{"usd": 0}, {"usd": -1}, {"nothing": 1}])
def test_unusable_prices_are_rejected(monkeypatch, caplog, doc):
    monkeypatch.setattr(settings, "X402_BZZ_PRICE_FEED_URL", "https://feed.example/price")
    with _feed(doc), caplog.at_level("WARNING"):
        asyncio.run(metrics._update_bzz_rates())
    assert metrics.bzz_usd_rate_market._value.get() == 0
    assert any("price feed read failed" in r.message for r in caplog.records)


def test_non_finite_price_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "X402_BZZ_PRICE_FEED_URL", "https://feed.example/price")

    def handler(request):
        return httpx.Response(200, content=b'{"usd": Infinity}', headers={"content-type": "application/json"})
    real = httpx.AsyncClient
    with patch("httpx.AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)):
        asyncio.run(metrics._update_bzz_rates())
    assert metrics.bzz_usd_rate_market._value.get() == 0


def test_a_dead_feed_stops_reporting_a_stale_price(monkeypatch):
    monkeypatch.setattr(settings, "X402_BZZ_PRICE_FEED_URL", "https://feed.example/price")
    with _feed({"usd": 0.04}):
        asyncio.run(metrics._update_bzz_rates())
    assert metrics.bzz_usd_rate_market._value.get() == 0.04
    with patch("httpx.AsyncClient", side_effect=RuntimeError("down")):
        for _ in range(metrics.PRICE_FEED_MAX_FAILURES):
            monkeypatch.setattr(metrics, "_last_price_fetch", None)
            asyncio.run(metrics._update_bzz_rates())
    assert metrics.bzz_usd_rate_market._value.get() == 0


def test_the_first_read_is_not_throttled_on_a_freshly_booted_host(monkeypatch):
    """A sentinel of 0.0 for "never fetched" is wrong against a monotonic clock.

    time.monotonic() is time since boot, so `monotonic() - 0.0 < interval` is true
    on any host whose uptime is under the interval, and the first read never
    happens. That is every freshly booted machine and every CI runner: this whole
    suite passed on a developer machine with days of uptime and failed in CI,
    where the gauge stayed 0 and the feed was never called.

    Simulated by making monotonic() report a small uptime rather than by waiting.
    """
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(200, json={"swarm-bzz": {"usd": 0.041}})

    monkeypatch.setattr(settings, "X402_BZZ_PRICE_FEED_URL", "https://feed.example/p")
    monkeypatch.setattr(settings, "X402_BZZ_PRICE_FEED_INTERVAL_SECONDS", 900)
    monkeypatch.setattr(metrics.time, "monotonic", lambda: 42.0)  # 42s since boot

    real = httpx.AsyncClient
    with patch("httpx.AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)):
        asyncio.run(metrics._update_bzz_rates())

    assert calls, "the first read was throttled on a host with 42s of uptime"
    assert metrics.bzz_usd_rate_market._value.get() == 0.041


def test_the_second_read_is_still_throttled(monkeypatch):
    """The control: fixing the sentinel must not disable throttling."""
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(200, json={"swarm-bzz": {"usd": 0.041}})

    monkeypatch.setattr(settings, "X402_BZZ_PRICE_FEED_URL", "https://feed.example/p")
    monkeypatch.setattr(settings, "X402_BZZ_PRICE_FEED_INTERVAL_SECONDS", 900)
    monkeypatch.setattr(metrics.time, "monotonic", lambda: 42.0)

    real = httpx.AsyncClient
    with patch("httpx.AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)):
        asyncio.run(metrics._update_bzz_rates())
        asyncio.run(metrics._update_bzz_rates())

    assert len(calls) == 1, f"throttling stopped working: {len(calls)} reads"
