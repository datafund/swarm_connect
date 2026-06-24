# tests/test_chunks_billing.py
"""
Tests for x402 bandwidth pricing, credit top-up, and debit-on-upload (issue #221).
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app

VALID_STAMP = "a" * 226


@pytest.fixture
def client():
    return TestClient(app)


def _enabled_settings(max_bytes=4104, x402=True, min_topup_mb=100):
    ms = MagicMock()
    ms.CHUNK_UPLOAD_ENABLED = True
    ms.CHUNK_UPLOAD_MAX_BYTES_PER_REQUEST = max_bytes
    ms.X402_ENABLED = x402
    ms.BANDWIDTH_CREDIT_MIN_TOPUP_MB = min_topup_mb
    ms.X402_BANDWIDTH_USD_PER_GB = 0.10
    return ms


# --------------------------------------------------------------------------- #
# Pricing
# --------------------------------------------------------------------------- #
class TestBandwidthPricing:
    def test_price_scales_with_size(self):
        from app.x402 import pricing

        with patch("app.x402.pricing.settings") as ms:
            ms.X402_BANDWIDTH_USD_PER_GB = 1.0
            ms.X402_MARKUP_PERCENT = 0.0
            ms.X402_MIN_PRICE_USD = 0.0
            # 0.5 GB at $1/GB, no markup/floor -> $0.50
            result = pricing.calculate_bandwidth_price_usd(500_000_000)
            assert result["price_usd"] == 0.5

    def test_markup_applied(self):
        from app.x402 import pricing

        with patch("app.x402.pricing.settings") as ms:
            ms.X402_BANDWIDTH_USD_PER_GB = 1.0
            ms.X402_MARKUP_PERCENT = 50.0
            ms.X402_MIN_PRICE_USD = 0.0
            result = pricing.calculate_bandwidth_price_usd(1_000_000_000)  # 1 GB
            assert result["price_usd"] == 1.5

    def test_minimum_price_floor(self):
        from app.x402 import pricing

        with patch("app.x402.pricing.settings") as ms:
            ms.X402_BANDWIDTH_USD_PER_GB = 0.10
            ms.X402_MARKUP_PERCENT = 0.0
            ms.X402_MIN_PRICE_USD = 0.01
            # 1000 bytes is negligible -> floored to the minimum
            result = pricing.calculate_bandwidth_price_usd(1000)
            assert result["price_usd"] == 0.01
            assert result["minimum_applied"] is True

    @pytest.mark.asyncio
    async def test_get_price_quote_bandwidth(self):
        from app.x402 import pricing

        with patch("app.x402.pricing.settings") as ms:
            ms.X402_BANDWIDTH_USD_PER_GB = 1.0
            ms.X402_MARKUP_PERCENT = 0.0
            ms.X402_MIN_PRICE_USD = 0.0
            ms.X402_NETWORK = "base-sepolia"
            ms.X402_PAY_TO_ADDRESS = "0xPayTo"
            quote = await pricing.get_price_quote(operation="bandwidth", size_bytes=1_000_000_000)
            assert quote["price_usd"] == 1.0
            assert quote["currency"] == "USDC"


# --------------------------------------------------------------------------- #
# Top-up endpoint (handler logic; x402 state simulated)
# --------------------------------------------------------------------------- #
class TestTopUp:
    @pytest.mark.asyncio
    async def test_topup_credits_payer_and_returns_token(self):
        from app.api.endpoints import chunks

        req = SimpleNamespace(state=SimpleNamespace(x402_mode="paid", x402_payer="0xPAYER"))
        mock_mgr = MagicMock()
        mock_mgr.credit.return_value = 100_000_000
        mock_mgr.issue_token.return_value = "tok_abc"

        with patch("app.api.endpoints.chunks.settings", _enabled_settings()):
            with patch("app.api.endpoints.chunks.bandwidth_credit_manager", mock_mgr):
                resp = await chunks.top_up_credit(req, mb=100)

        assert resp.address == "0xPAYER"
        assert resp.token == "tok_abc"
        assert resp.credited_bytes == 100_000_000  # 100 MB * 10^6
        assert resp.balance_bytes == 100_000_000
        mock_mgr.credit.assert_called_once_with("0xPAYER", 100_000_000)

    @pytest.mark.asyncio
    async def test_topup_requires_paid_mode(self):
        from app.api.endpoints import chunks
        from fastapi import HTTPException

        req = SimpleNamespace(state=SimpleNamespace(x402_mode="free-tier", x402_payer=None))
        with patch("app.api.endpoints.chunks.settings", _enabled_settings()):
            with patch("app.api.endpoints.chunks.bandwidth_credit_manager", MagicMock()):
                with pytest.raises(HTTPException) as exc:
                    await chunks.top_up_credit(req, mb=100)
        assert exc.value.status_code == 402

    @pytest.mark.asyncio
    async def test_topup_below_minimum_rejected(self):
        from app.api.endpoints import chunks
        from fastapi import HTTPException

        req = SimpleNamespace(state=SimpleNamespace(x402_mode="paid", x402_payer="0xP"))
        with patch("app.api.endpoints.chunks.settings", _enabled_settings(min_topup_mb=100)):
            with patch("app.api.endpoints.chunks.bandwidth_credit_manager", MagicMock()):
                with pytest.raises(HTTPException) as exc:
                    await chunks.top_up_credit(req, mb=10)
        assert exc.value.status_code == 400
        assert exc.value.detail["code"] == "TOPUP_TOO_SMALL"

    @pytest.mark.asyncio
    async def test_topup_billing_disabled(self):
        from app.api.endpoints import chunks
        from fastapi import HTTPException

        req = SimpleNamespace(state=SimpleNamespace(x402_mode="paid", x402_payer="0xP"))
        with patch("app.api.endpoints.chunks.settings", _enabled_settings(x402=False)):
            with pytest.raises(HTTPException) as exc:
                await chunks.top_up_credit(req, mb=100)
        assert exc.value.status_code == 400
        assert exc.value.detail["code"] == "BILLING_DISABLED"


# --------------------------------------------------------------------------- #
# Debit-on-upload
# --------------------------------------------------------------------------- #
class TestDebitOnUpload:
    def _mgr(self, address="0xowner", debit_ok=True, remaining=999):
        m = MagicMock()
        m.resolve_token.return_value = address
        m.debit.return_value = (debit_ok, remaining)
        return m

    def test_valid_token_debits_and_uploads(self, client):
        mgr = self._mgr(remaining=4096)
        with patch("app.api.endpoints.chunks.settings", _enabled_settings()):
            with patch("app.api.endpoints.chunks.bandwidth_credit_manager", mgr):
                with patch("app.api.endpoints.chunks.upload_chunk_to_swarm",
                           new=AsyncMock(return_value="ref123")):
                    r = client.post(
                        "/api/v1/chunks/",
                        content=b"x" * 8,
                        headers={"Swarm-Postage-Stamp": VALID_STAMP,
                                 "X-Bandwidth-Credit-Token": "tok"},
                    )
        assert r.status_code == 201
        body = r.json()
        assert body["reference"] == "ref123"
        assert body["bytes_charged"] == 8
        mgr.debit.assert_called_once_with("0xowner", 8)

    def test_missing_token_402(self, client):
        with patch("app.api.endpoints.chunks.settings", _enabled_settings()):
            with patch("app.api.endpoints.chunks.bandwidth_credit_manager", self._mgr()):
                r = client.post(
                    "/api/v1/chunks/",
                    content=b"data",
                    headers={"Swarm-Postage-Stamp": VALID_STAMP},
                )
        assert r.status_code == 402
        assert r.json()["detail"]["code"] == "CREDIT_REQUIRED"

    def test_invalid_token_402(self, client):
        mgr = MagicMock()
        mgr.resolve_token.return_value = None
        with patch("app.api.endpoints.chunks.settings", _enabled_settings()):
            with patch("app.api.endpoints.chunks.bandwidth_credit_manager", mgr):
                r = client.post(
                    "/api/v1/chunks/",
                    content=b"data",
                    headers={"Swarm-Postage-Stamp": VALID_STAMP,
                             "X-Bandwidth-Credit-Token": "bad"},
                )
        assert r.status_code == 402
        assert r.json()["detail"]["code"] == "INVALID_CREDIT_TOKEN"

    def test_insufficient_credit_402(self, client):
        mgr = self._mgr(debit_ok=False, remaining=2)
        with patch("app.api.endpoints.chunks.settings", _enabled_settings()):
            with patch("app.api.endpoints.chunks.bandwidth_credit_manager", mgr):
                r = client.post(
                    "/api/v1/chunks/",
                    content=b"x" * 100,
                    headers={"Swarm-Postage-Stamp": VALID_STAMP,
                             "X-Bandwidth-Credit-Token": "tok"},
                )
        assert r.status_code == 402
        assert r.json()["detail"]["code"] == "INSUFFICIENT_CREDIT"

    def test_bee_failure_refunds_credit(self, client):
        mgr = self._mgr(remaining=4096)
        with patch("app.api.endpoints.chunks.settings", _enabled_settings()):
            with patch("app.api.endpoints.chunks.bandwidth_credit_manager", mgr):
                with patch("app.api.endpoints.chunks.upload_chunk_to_swarm",
                           new=AsyncMock(side_effect=httpx.HTTPError("bee down"))):
                    r = client.post(
                        "/api/v1/chunks/",
                        content=b"x" * 8,
                        headers={"Swarm-Postage-Stamp": VALID_STAMP,
                                 "X-Bandwidth-Credit-Token": "tok"},
                    )
        assert r.status_code == 502
        # Debited then refunded the same byte count
        mgr.debit.assert_called_once_with("0xowner", 8)
        mgr.credit.assert_called_once_with("0xowner", 8)

    def test_x402_disabled_no_debit(self, client):
        mgr = MagicMock()
        with patch("app.api.endpoints.chunks.settings", _enabled_settings(x402=False)):
            with patch("app.api.endpoints.chunks.bandwidth_credit_manager", mgr):
                with patch("app.api.endpoints.chunks.upload_chunk_to_swarm",
                           new=AsyncMock(return_value="ref")):
                    r = client.post(
                        "/api/v1/chunks/",
                        content=b"data",
                        headers={"Swarm-Postage-Stamp": VALID_STAMP},
                    )
        assert r.status_code == 201
        assert r.json()["bytes_charged"] is None
        mgr.debit.assert_not_called()
