# tests/test_metrics.py
"""
Tests for Prometheus metrics endpoint and custom metrics (Issue #180-#183).
"""
import os
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

# Ensure test environment settings
os.environ.setdefault("SWARM_BEE_API_URL", "http://localhost:1633")
os.environ.setdefault("METRICS_ENABLED", "true")
os.environ.setdefault("GATEWAY_ENVIRONMENT", "test")

from fastapi.testclient import TestClient
from prometheus_client import REGISTRY


class TestMetricsEndpoint:
    """Tests for the /metrics endpoint."""

    def test_metrics_endpoint_returns_200(self):
        """GET /metrics returns 200 with Prometheus text format."""
        from app.main import app
        client = TestClient(app)
        response = client.get("/metrics")
        assert response.status_code == 200
        # Prometheus text format contains HELP and TYPE lines
        assert "# HELP" in response.text
        assert "# TYPE" in response.text

    def test_metrics_contains_http_request_metrics(self):
        """Auto-instrumented HTTP metrics are present."""
        from app.main import app
        client = TestClient(app)
        # Make a request first to generate metrics
        client.get("/")
        response = client.get("/metrics")
        assert response.status_code == 200
        # Check for auto-instrumented metrics from prometheus-fastapi-instrumentator
        assert "http_request" in response.text

    def test_metrics_contains_gateway_info(self):
        """gateway_info metric is registered and can be set with environment label."""
        from app.services.metrics import gateway_info
        # Set info directly (lifespan does this at startup, but may not run in test context)
        gateway_info.info({
            "version": "test",
            "environment": "test",
            "x402_enabled": "False",
            "pool_enabled": "False",
            "notary_enabled": "False",
        })
        from app.main import app
        client = TestClient(app)
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "gateway_info" in response.text
        assert 'environment="test"' in response.text

    def test_metrics_contains_custom_counters(self):
        """Custom counter definitions are registered."""
        from app.main import app
        client = TestClient(app)
        response = client.get("/metrics")
        text = response.text
        # Check counter definitions exist
        assert "gateway_uploads_total" in text
        assert "gateway_downloads_total" in text
        assert "gateway_stamp_purchases_total" in text
        assert "gateway_pool_acquires_total" in text
        assert "gateway_rate_limit_hits_total" in text

    def test_metrics_contains_custom_gauges(self):
        """Custom gauge definitions are registered."""
        from app.main import app
        client = TestClient(app)
        response = client.get("/metrics")
        text = response.text
        assert "gateway_uptime_seconds" in text
        assert "gateway_stamps_total" in text


class TestMetricsExemptFromRateLimit:
    """Tests that /metrics is exempt from rate limiting."""

    def test_metrics_in_exempt_paths(self):
        """The /metrics path is in the exempt paths set."""
        from app.middleware.rate_limit import EXEMPT_PATHS
        assert "/metrics" in EXEMPT_PATHS

    def test_is_exempt_path_returns_true(self):
        """_is_exempt_path returns True for /metrics."""
        from app.middleware.rate_limit import _is_exempt_path
        assert _is_exempt_path("/metrics") is True


class TestUploadCounters:
    """Tests that upload operations increment counters."""

    @patch("app.api.endpoints.data.upload_data_to_swarm", new_callable=AsyncMock)
    @patch("app.api.endpoints.data.stamp_ownership_manager")
    def test_upload_success_increments_counter(self, mock_ownership, mock_upload):
        """Successful upload increments gateway_uploads_total{status=success}."""
        from app.services.metrics import uploads_total, upload_bytes_total
        mock_upload.return_value = "a" * 64
        mock_ownership.check_access.return_value = (True, "ok")

        before = uploads_total.labels(status="success")._value.get()

        from app.main import app
        client = TestClient(app)
        import io
        response = client.post(
            f"/api/v1/data/?stamp_id={'ab' * 32}",
            files={"file": ("test.json", io.BytesIO(b'{"test": true}'), "application/json")},
        )
        assert response.status_code == 200

        after = uploads_total.labels(status="success")._value.get()
        assert after > before

    @patch("app.api.endpoints.data.upload_data_to_swarm", new_callable=AsyncMock)
    @patch("app.api.endpoints.data.stamp_ownership_manager")
    def test_upload_error_increments_counter(self, mock_ownership, mock_upload):
        """Failed upload increments gateway_uploads_total{status=error}."""
        import httpx
        from app.services.metrics import uploads_total
        mock_upload.side_effect = httpx.HTTPError("Bee node down")
        mock_ownership.check_access.return_value = (True, "ok")

        before = uploads_total.labels(status="error")._value.get()

        from app.main import app
        client = TestClient(app)
        import io
        response = client.post(
            f"/api/v1/data/?stamp_id={'ab' * 32}",
            files={"file": ("test.json", io.BytesIO(b'{"test": true}'), "application/json")},
        )
        assert response.status_code == 502

        after = uploads_total.labels(status="error")._value.get()
        assert after > before


class TestDownloadCounters:
    """Tests that download operations increment counters."""

    @patch("app.api.endpoints.data.download_data_from_swarm", new_callable=AsyncMock)
    def test_download_success_increments_counter(self, mock_download):
        """Successful download increments gateway_downloads_total{status=success}."""
        from app.services.metrics import downloads_total
        mock_download.return_value = b'{"data": "test"}'

        before = downloads_total.labels(status="success")._value.get()

        from app.main import app
        client = TestClient(app)
        response = client.get(f"/api/v1/data/{'ab' * 32}")
        assert response.status_code == 200

        after = downloads_total.labels(status="success")._value.get()
        assert after > before

    @patch("app.api.endpoints.data.download_data_from_swarm", new_callable=AsyncMock)
    def test_download_not_found_increments_error(self, mock_download):
        """404 download increments gateway_downloads_total{status=error}."""
        from app.services.metrics import downloads_total
        mock_download.side_effect = FileNotFoundError("not found")

        before = downloads_total.labels(status="error")._value.get()

        from app.main import app
        client = TestClient(app)
        response = client.get(f"/api/v1/data/{'ab' * 32}")
        assert response.status_code == 404

        after = downloads_total.labels(status="error")._value.get()
        assert after > before


class TestRateLimitCounter:
    """Tests that rate limit hits increment the counter."""

    def test_rate_limit_counter_registered(self):
        """rate_limit_hits_total counter is registered."""
        from app.services.metrics import rate_limit_hits_total
        # Simply verify the counter object exists and is callable
        assert rate_limit_hits_total is not None
        # Incrementing should not raise
        rate_limit_hits_total.inc(0)


class TestStampPurchaseCounter:
    """Tests that stamp purchase operations increment counters."""

    @patch("app.api.endpoints.stamps.swarm_api.check_sufficient_funds", new_callable=AsyncMock)
    @patch("app.api.endpoints.stamps.swarm_api.calculate_stamp_total_cost")
    @patch("app.api.endpoints.stamps.swarm_api.calculate_stamp_amount")
    @patch("app.api.endpoints.stamps.swarm_api.get_chainstate", new_callable=AsyncMock)
    @patch("app.api.endpoints.stamps.swarm_api.purchase_postage_stamp", new_callable=AsyncMock)
    def test_stamp_purchase_increments_counter(
        self, mock_purchase, mock_chainstate, mock_calc_amount, mock_calc_cost, mock_funds
    ):
        """Successful stamp purchase increments gateway_stamp_purchases_total."""
        from app.services.metrics import stamp_purchases_total
        mock_purchase.return_value = "ab" * 32
        mock_chainstate.return_value = {"currentPrice": "100"}
        mock_calc_amount.return_value = 10000
        mock_calc_cost.return_value = 50000
        mock_funds.return_value = {"sufficient": True}

        before = stamp_purchases_total.labels(size="small", status="success")._value.get()

        from app.main import app
        client = TestClient(app)
        response = client.post(
            "/api/v1/stamps/",
            json={"size": "small"},
        )
        assert response.status_code == 201

        after = stamp_purchases_total.labels(size="small", status="success")._value.get()
        assert after > before


class TestX402PaymentCounter:
    """Tests that x402 payment mode counters are defined."""

    def test_x402_payment_counter_registered(self):
        """x402_payments_total counter exists with expected labels."""
        from app.services.metrics import x402_payments_total
        assert x402_payments_total is not None
        # Labels should not raise
        x402_payments_total.labels(mode="paid").inc(0)
        x402_payments_total.labels(mode="free").inc(0)
        x402_payments_total.labels(mode="rejected").inc(0)


class TestMetricsBackgroundTask:
    """Tests for the background balance polling task."""

    @pytest.mark.asyncio
    async def test_start_stop_background_task(self):
        """Background task can be started and stopped without error."""
        from app.services.metrics import (
            start_metrics_background_task,
            stop_metrics_background_task,
        )
        # Patch the poll function to avoid actual API calls
        with patch("app.services.metrics._poll_balances", new_callable=AsyncMock):
            await start_metrics_background_task()
            await stop_metrics_background_task()
