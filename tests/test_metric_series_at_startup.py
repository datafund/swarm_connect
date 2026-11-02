# tests/test_metric_series_at_startup.py
"""
KPI counter series exist from startup, and notary success is counted only once
the signed document is stored (#518).

A counter series created on its first event first appears at 1, and rate() and
increase() have no earlier sample to measure from, so they count that event as
0. For a rare event such as a paid pool acquire that could not be delivered,
that means a rate panel misses the occurrence entirely. Creating the series at 0
at startup gives Prometheus the earlier sample.
"""
import io
import json
import os
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.models.stamp import SIZE_PRESETS
from app.services.provenance import ProvenanceService

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

POOL_STATUSES = ("success", "error", "settled_not_delivered")

EXPECTED_SERIES = (
    [f'gateway_pool_acquires_total{{size="{size}",status="{status}"}}'
     for size in SIZE_PRESETS for status in POOL_STATUSES]
    + [f'gateway_uploads_total{{status="{s}"}}' for s in ("success", "error")]
    + [f'gateway_downloads_total{{status="{s}"}}' for s in ("success", "error", "too_large")]
    + [f'gateway_notary_signatures_total{{status="{s}"}}' for s in ("success", "error")]
)


def _fresh_process_exposition() -> str:
    """Render the metrics registry in a new interpreter, before any event."""
    code = (
        "import app.services.metrics\n"
        "from prometheus_client import REGISTRY, generate_latest\n"
        "print(generate_latest(REGISTRY).decode())\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


class TestSeriesExistAtStartup:

    def test_pool_settled_not_delivered_series_exist_at_zero(self):
        text = _fresh_process_exposition()
        for size in SIZE_PRESETS:
            line = f'gateway_pool_acquires_total{{size="{size}",status="settled_not_delivered"}} 0.0'
            assert line in text.splitlines(), f"missing at startup: {line}"

    def test_kpi_series_exist_at_zero(self):
        lines = _fresh_process_exposition().splitlines()
        missing = [s for s in EXPECTED_SERIES if f"{s} 0.0" not in lines]
        assert not missing, f"series not created at 0 at startup: {missing}"

    def test_series_are_served_on_metrics_endpoint(self):
        from app.main import app
        text = TestClient(app).get("/metrics").text
        served = {line.rsplit(" ", 1)[0] for line in text.splitlines() if not line.startswith("#")}
        missing = [s for s in EXPECTED_SERIES if s not in served]
        assert not missing, f"not on /metrics: {missing}"

    def test_stamp_purchase_series_are_not_precreated(self):
        # Direct purchases take any depth, so the size label can be depth-N and
        # has no bounded set to create in advance.
        assert "gateway_stamp_purchases_total{" not in _fresh_process_exposition()


def _signing_service():
    service = MagicMock(spec=ProvenanceService)
    signed = MagicMock()
    signed.raw_json = json.dumps({"data": "test", "signatures": [{"sig": "x"}]})
    signed.signatures = [{"sig": "x"}]
    service.sign_document.return_value = signed
    return service


def _count(counter, status):
    return counter.labels(status=status)._value.get()


@patch("app.api.endpoints.data.stamp_ownership_manager")
@patch("app.api.endpoints.data.upload_data_to_swarm", new_callable=AsyncMock)
class TestNotarySuccessCountedAfterUpload:

    def _post_signed(self):
        from app.main import app
        with patch("app.api.endpoints.data.get_provenance_service", return_value=_signing_service()):
            return TestClient(app).post(
                f"/api/v1/data/?stamp_id={'ab' * 32}&sign=notary",
                files={"file": ("doc.json", io.BytesIO(b'{"data": "test"}'), "application/json")},
            )

    def test_not_counted_when_upload_fails(self, mock_upload, mock_ownership):
        from app.services.metrics import notary_signatures_total, uploads_total
        mock_ownership.check_access.return_value = (True, "ok")
        mock_upload.side_effect = httpx.HTTPError("Bee node down")
        notary_before = _count(notary_signatures_total, "success")
        notary_err_before = _count(notary_signatures_total, "error")
        upload_err_before = _count(uploads_total, "error")

        response = self._post_signed()

        assert response.status_code == 502
        assert _count(notary_signatures_total, "success") == notary_before
        # A storage failure is not a signing failure.
        assert _count(notary_signatures_total, "error") == notary_err_before
        assert _count(uploads_total, "error") == upload_err_before + 1

    def test_counted_once_after_successful_upload(self, mock_upload, mock_ownership):
        from app.services.metrics import notary_signatures_total, uploads_total
        mock_ownership.check_access.return_value = (True, "ok")
        mock_upload.return_value = "c" * 64
        notary_before = _count(notary_signatures_total, "success")
        upload_before = _count(uploads_total, "success")

        response = self._post_signed()

        assert response.status_code == 200
        assert _count(notary_signatures_total, "success") == notary_before + 1
        assert _count(uploads_total, "success") == upload_before + 1

    def test_notarized_never_exceeds_stored(self, mock_upload, mock_ownership):
        from app.services.metrics import notary_signatures_total, uploads_total
        mock_ownership.check_access.return_value = (True, "ok")
        notary_before = _count(notary_signatures_total, "success")
        upload_before = _count(uploads_total, "success")

        mock_upload.side_effect = [httpx.HTTPError("down"), "d" * 64, httpx.HTTPError("down")]
        for _ in range(3):
            self._post_signed()

        notarized = _count(notary_signatures_total, "success") - notary_before
        stored = _count(uploads_total, "success") - upload_before
        assert stored == 1
        assert notarized <= stored


class TestDashboardText:

    def _panel(self, panel_id):
        path = os.path.join(REPO_ROOT, "monitoring/provisioning/dashboards/gateway-overview.json")
        with open(path) as f:
            dashboard = json.load(f)["dashboard"]

        def walk(panels):
            for p in panels:
                yield p
                yield from walk(p.get("panels", []))
        return next(p for p in walk(dashboard["panels"]) if p.get("id") == panel_id)

    def test_notarized_share_no_longer_claims_it_can_exceed_100(self):
        description = self._panel(112)["description"]
        assert "can therefore exceed 100%" not in description
        assert "counted before the Swarm upload" not in description
        assert "only once the signed document is stored" in description
