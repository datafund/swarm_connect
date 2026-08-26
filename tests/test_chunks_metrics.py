# tests/test_chunks_metrics.py
"""
Tests for chunk-forwarding / bandwidth-credit metrics (issue #223).
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.metrics import (
    bandwidth_credit_accounts,
    bandwidth_credit_bytes_total,
    bandwidth_topup_bytes_total,
    bandwidth_topups_total,
    chunk_upload_bytes_total,
    chunk_uploads_total,
)

VALID_STAMP = "a" * 226


@pytest.fixture
def client():
    return TestClient(app)


def _settings(x402=False):
    ms = MagicMock()
    ms.CHUNK_UPLOAD_ENABLED = True
    ms.CHUNK_UPLOAD_MAX_BYTES_PER_REQUEST = 4104
    ms.X402_ENABLED = x402
    ms.BANDWIDTH_CREDIT_MIN_TOPUP_MB = 100
    # Explicit: an unset MagicMock attribute compares truthy, so the
    # ceiling check would refuse every top-up.
    ms.BANDWIDTH_CREDIT_MAX_TOPUP_MB = 1_000_000
    ms.X402_BANDWIDTH_USD_PER_GB = 0.10
    return ms


class TestChunkMetrics:
    def test_upload_increments_success_counter_and_bytes(self, client):
        before = chunk_uploads_total.labels(status="success", mode="none")._value.get()
        before_bytes = chunk_upload_bytes_total._value.get()

        with patch("app.api.endpoints.chunks.settings", _settings(x402=False)):
            with patch("app.api.endpoints.chunks.upload_chunk_to_swarm",
                       new=AsyncMock(return_value="ref")):
                r = client.post(
                    "/api/v1/chunks/",
                    content=b"x" * 8,
                    headers={"Swarm-Postage-Stamp": VALID_STAMP},
                )
        assert r.status_code == 201
        after = chunk_uploads_total.labels(status="success", mode="none")._value.get()
        after_bytes = chunk_upload_bytes_total._value.get()
        assert after == before + 1
        assert after_bytes == before_bytes + 8

    def test_upload_error_increments_error_counter(self, client):
        import httpx
        before = chunk_uploads_total.labels(status="error", mode="none")._value.get()
        with patch("app.api.endpoints.chunks.settings", _settings(x402=False)):
            with patch("app.api.endpoints.chunks.upload_chunk_to_swarm",
                       new=AsyncMock(side_effect=httpx.HTTPError("down"))):
                r = client.post(
                    "/api/v1/chunks/",
                    content=b"x" * 8,
                    headers={"Swarm-Postage-Stamp": VALID_STAMP},
                )
        assert r.status_code == 502
        after = chunk_uploads_total.labels(status="error", mode="none")._value.get()
        assert after == before + 1

    @pytest.mark.asyncio
    async def test_topup_increments_counters(self):
        from types import SimpleNamespace
        from app.api.endpoints import chunks

        before = bandwidth_topups_total.labels(status="success")._value.get()
        before_bytes = bandwidth_topup_bytes_total._value.get()

        req = SimpleNamespace(state=SimpleNamespace(x402_mode="paid", x402_payer="0xP"))
        mock_mgr = MagicMock()
        mock_mgr.credit.return_value = 100_000_000
        mock_mgr.issue_token.return_value = "tok"

        with patch("app.api.endpoints.chunks.settings", _settings(x402=True)):
            with patch("app.api.endpoints.chunks.bandwidth_credit_manager", mock_mgr):
                await chunks.top_up_credit(req, mb=100)

        assert bandwidth_topups_total.labels(status="success")._value.get() == before + 1
        assert bandwidth_topup_bytes_total._value.get() == before_bytes + 100_000_000

    def test_gateway_info_exposes_chunk_upload_flag(self):
        # Lifespan (run by the context manager) sets gateway_info; scrape /metrics.
        with TestClient(app) as c:
            body = c.get("/metrics").text
        assert "chunk_upload_enabled" in body

    def test_credit_gauges_registered(self):
        # Gauges exist and are settable (poller updates them in production).
        bandwidth_credit_accounts.set(3)
        bandwidth_credit_bytes_total.set(12345)
        assert bandwidth_credit_accounts._value.get() == 3
        assert bandwidth_credit_bytes_total._value.get() == 12345
