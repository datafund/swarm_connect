# tests/test_chunks_free_tier.py
"""
Tests for the chunk-upload free tier: per-IP daily byte quota (issue #222).
"""
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.bandwidth_free_tier import BandwidthFreeTierTracker

VALID_STAMP = "a" * 226


@pytest.fixture
def client():
    return TestClient(app)


def _settings(free_enabled=True, mb_per_day=1):
    ms = MagicMock()
    ms.CHUNK_UPLOAD_ENABLED = True
    ms.CHUNK_UPLOAD_MAX_BYTES_PER_REQUEST = 4104
    ms.X402_ENABLED = True
    ms.CHUNK_UPLOAD_FREE_TIER_ENABLED = free_enabled
    ms.CHUNK_UPLOAD_FREE_TIER_MB_PER_DAY = mb_per_day
    # Used by _topup_info() in 402/429 responses; must be JSON-serializable.
    ms.BANDWIDTH_CREDIT_MIN_TOPUP_MB = 100
    ms.X402_BANDWIDTH_USD_PER_GB = 0.10
    return ms


# --------------------------------------------------------------------------- #
# Tracker unit
# --------------------------------------------------------------------------- #
class TestFreeTierTracker:
    def test_consume_within_limit(self):
        t = BandwidthFreeTierTracker()
        ok, remaining = t.try_consume("1.1.1.1", 100, 1000)
        assert ok is True
        assert remaining == 900

    def test_consume_over_limit_unchanged(self):
        t = BandwidthFreeTierTracker()
        t.try_consume("1.1.1.1", 900, 1000)
        ok, remaining = t.try_consume("1.1.1.1", 200, 1000)  # would exceed
        assert ok is False
        assert remaining == 100
        # usage unchanged: a 100-byte upload still fits
        ok2, _ = t.try_consume("1.1.1.1", 100, 1000)
        assert ok2 is True

    def test_refund_restores(self):
        t = BandwidthFreeTierTracker()
        t.try_consume("1.1.1.1", 500, 1000)
        t.refund("1.1.1.1", 500)
        ok, remaining = t.try_consume("1.1.1.1", 1000, 1000)
        assert ok is True
        assert remaining == 0

    def test_resets_across_utc_day(self):
        t = BandwidthFreeTierTracker()
        t._today = lambda: "2026-01-01"
        t.try_consume("1.1.1.1", 1000, 1000)  # full for day 1
        ok_same, _ = t.try_consume("1.1.1.1", 1, 1000)
        assert ok_same is False
        # New day -> quota resets
        t._today = lambda: "2026-01-02"
        ok_new, remaining = t.try_consume("1.1.1.1", 1, 1000)
        assert ok_new is True
        assert remaining == 999

    def test_isolated_per_ip(self):
        t = BandwidthFreeTierTracker()
        t.try_consume("1.1.1.1", 1000, 1000)
        ok, remaining = t.try_consume("2.2.2.2", 500, 1000)
        assert ok is True
        assert remaining == 500


# --------------------------------------------------------------------------- #
# Endpoint
# --------------------------------------------------------------------------- #
class TestFreeTierEndpoint:
    def test_free_upload_within_quota(self, client):
        tracker = BandwidthFreeTierTracker()
        with patch("app.api.endpoints.chunks.settings", _settings(mb_per_day=1)):
            with patch("app.api.endpoints.chunks.free_tier_tracker", tracker):
                with patch("app.api.endpoints.chunks.upload_chunk_to_swarm",
                           new=AsyncMock(return_value="ref")):
                    r = client.post(
                        "/api/v1/chunks/",
                        content=b"x" * 8,
                        headers={"Swarm-Postage-Stamp": VALID_STAMP, "X-Payment-Mode": "free"},
                    )
        assert r.status_code == 201
        assert r.json()["bytes_charged"] == 8

    def test_free_over_quota_429(self, client):
        tracker = BandwidthFreeTierTracker()
        with patch("app.api.endpoints.chunks.settings", _settings(mb_per_day=0)):  # zero quota
            with patch("app.api.endpoints.chunks.free_tier_tracker", tracker):
                r = client.post(
                    "/api/v1/chunks/",
                    content=b"x" * 8,
                    headers={"Swarm-Postage-Stamp": VALID_STAMP, "X-Payment-Mode": "free"},
                )
        assert r.status_code == 429
        assert r.json()["detail"]["code"] == "FREE_QUOTA_EXCEEDED"

    def test_free_tier_disabled_402(self, client):
        with patch("app.api.endpoints.chunks.settings", _settings(free_enabled=False)):
            with patch("app.api.endpoints.chunks.free_tier_tracker", BandwidthFreeTierTracker()):
                r = client.post(
                    "/api/v1/chunks/",
                    content=b"x" * 8,
                    headers={"Swarm-Postage-Stamp": VALID_STAMP, "X-Payment-Mode": "free"},
                )
        assert r.status_code == 402
        assert r.json()["detail"]["code"] == "FREE_TIER_DISABLED"

    def test_free_bee_failure_refunds_quota(self, client):
        tracker = BandwidthFreeTierTracker()
        with patch("app.api.endpoints.chunks.settings", _settings(mb_per_day=1)):
            with patch("app.api.endpoints.chunks.free_tier_tracker", tracker):
                with patch("app.api.endpoints.chunks.upload_chunk_to_swarm",
                           new=AsyncMock(side_effect=httpx.HTTPError("bee down"))):
                    r = client.post(
                        "/api/v1/chunks/",
                        content=b"x" * 8,
                        headers={"Swarm-Postage-Stamp": VALID_STAMP, "X-Payment-Mode": "free"},
                    )
        assert r.status_code == 502
        # Quota refunded: no bytes counted against the (testclient) IP
        used = sum(e.get("bytes_used", 0) for e in tracker._usage.values())
        assert used == 0
