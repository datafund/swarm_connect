# tests/test_performance_timing.py
"""Tests for performance timing instrumentation in upload endpoints."""
import pytest
import io
import tarfile
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from app.main import app


class TestDataUploadTiming:
    """Tests for timing in data upload endpoint."""

    @patch('app.api.endpoints.data.upload_data_to_swarm')
    def test_upload_without_timing_by_default(self, mock_upload):
        """Test that timing is not included by default."""
        mock_upload.return_value = "abc123reference"

        client = TestClient(app)
        response = client.post(
            "/api/v1/data/?stamp_id=teststamp",
            files={"file": ("test.json", io.BytesIO(b'{"test": true}'), "application/json")}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["timing"] is None

    @patch('app.api.endpoints.data.upload_data_to_swarm')
    def test_upload_with_timing_enabled(self, mock_upload):
        """Test that timing is included when include_timing=true."""
        mock_upload.return_value = "abc123reference"

        client = TestClient(app)
        response = client.post(
            "/api/v1/data/?stamp_id=teststamp&include_timing=true",
            files={"file": ("test.json", io.BytesIO(b'{"test": true}'), "application/json")}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["timing"] is not None
        timing = data["timing"]
        assert "file_read_ms" in timing
        assert "bee_upload_ms" in timing
        assert "total_ms" in timing
        assert timing["stamp_validate_ms"] is None  # Not validated

    @patch('app.api.endpoints.data.upload_data_to_swarm')
    @patch('app.api.endpoints.data.validate_stamp_for_upload')
    def test_upload_timing_with_stamp_validation(self, mock_validate, mock_upload):
        """Test timing includes stamp validation time when enabled."""
        mock_upload.return_value = "abc123reference"
        mock_validate.return_value = None

        client = TestClient(app)
        response = client.post(
            "/api/v1/data/?stamp_id=teststamp&include_timing=true&validate_stamp=true",
            files={"file": ("test.json", io.BytesIO(b'{"test": true}'), "application/json")}
        )

        assert response.status_code == 200
        data = response.json()
        timing = data["timing"]
        assert timing["stamp_validate_ms"] is not None
        assert timing["stamp_validate_ms"] >= 0

    @patch('app.api.endpoints.data.upload_data_to_swarm')
    def test_server_timing_header_always_present(self, mock_upload):
        """Test Server-Timing header is always added, even without include_timing."""
        mock_upload.return_value = "abc123reference"

        client = TestClient(app)
        response = client.post(
            "/api/v1/data/?stamp_id=teststamp",
            files={"file": ("test.json", io.BytesIO(b'{"test": true}'), "application/json")}
        )

        assert response.status_code == 200
        assert "Server-Timing" in response.headers
        server_timing = response.headers["Server-Timing"]
        assert "file-read-ms" in server_timing
        assert "bee-upload-ms" in server_timing
        assert "total-ms" in server_timing

    @patch('app.api.endpoints.data.upload_data_to_swarm')
    def test_timing_values_are_positive(self, mock_upload):
        """Test that all timing values are positive numbers."""
        mock_upload.return_value = "abc123reference"

        client = TestClient(app)
        response = client.post(
            "/api/v1/data/?stamp_id=teststamp&include_timing=true",
            files={"file": ("test.json", io.BytesIO(b'{"test": true}'), "application/json")}
        )

        assert response.status_code == 200
        timing = response.json()["timing"]
        assert timing["file_read_ms"] >= 0
        assert timing["bee_upload_ms"] >= 0
        assert timing["total_ms"] >= 0
        assert timing["total_ms"] >= timing["file_read_ms"]
        assert timing["total_ms"] >= timing["bee_upload_ms"]


class TestManifestUploadTiming:
    """Tests for timing in manifest upload endpoint."""

    def _create_tar(self, file_count=3):
        """Create a TAR archive with the specified number of files."""
        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode='w') as tar:
            for i in range(file_count):
                data = f'{{"file": {i}}}'.encode()
                info = tarfile.TarInfo(name=f"file{i}.json")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return tar_buffer.getvalue()

    @patch('app.api.endpoints.data.upload_collection_to_swarm')
    @patch('app.api.endpoints.data.validate_tar')
    @patch('app.api.endpoints.data.count_tar_files')
    def test_manifest_without_timing_by_default(self, mock_count, mock_validate, mock_upload):
        """Test that timing is not included by default in manifest uploads."""
        mock_validate.return_value = None
        mock_count.return_value = 3
        mock_upload.return_value = "manifest123reference"

        tar_bytes = self._create_tar(3)

        client = TestClient(app)
        response = client.post(
            "/api/v1/data/manifest?stamp_id=teststamp",
            files={"file": ("test.tar", io.BytesIO(tar_bytes), "application/x-tar")}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["timing"] is None

    @patch('app.api.endpoints.data.upload_collection_to_swarm')
    @patch('app.api.endpoints.data.validate_tar')
    @patch('app.api.endpoints.data.count_tar_files')
    def test_manifest_with_timing_enabled(self, mock_count, mock_validate, mock_upload):
        """Test that timing is included when include_timing=true."""
        mock_validate.return_value = None
        mock_count.return_value = 3
        mock_upload.return_value = "manifest123reference"

        tar_bytes = self._create_tar(3)

        client = TestClient(app)
        response = client.post(
            "/api/v1/data/manifest?stamp_id=teststamp&include_timing=true",
            files={"file": ("test.tar", io.BytesIO(tar_bytes), "application/x-tar")}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["timing"] is not None
        timing = data["timing"]

        # Check all manifest-specific timing fields
        assert "file_read_ms" in timing
        assert "tar_validate_ms" in timing
        assert "tar_count_ms" in timing
        assert "bee_upload_ms" in timing
        assert "total_ms" in timing
        assert "file_count" in timing
        assert "ms_per_file" in timing
        assert "files_per_second" in timing

    @patch('app.api.endpoints.data.upload_collection_to_swarm')
    @patch('app.api.endpoints.data.validate_tar')
    @patch('app.api.endpoints.data.count_tar_files')
    def test_manifest_timing_derived_metrics(self, mock_count, mock_validate, mock_upload):
        """Test that derived metrics (ms_per_file, files_per_second) are calculated correctly."""
        mock_validate.return_value = None
        mock_count.return_value = 5
        mock_upload.return_value = "manifest123reference"

        tar_bytes = self._create_tar(5)

        client = TestClient(app)
        response = client.post(
            "/api/v1/data/manifest?stamp_id=teststamp&include_timing=true",
            files={"file": ("test.tar", io.BytesIO(tar_bytes), "application/x-tar")}
        )

        assert response.status_code == 200
        timing = response.json()["timing"]

        assert timing["file_count"] == 5
        assert timing["ms_per_file"] > 0
        assert timing["files_per_second"] > 0

        # Verify ms_per_file calculation
        expected_ms_per_file = timing["total_ms"] / timing["file_count"]
        assert abs(timing["ms_per_file"] - expected_ms_per_file) < 0.01

    @patch('app.api.endpoints.data.upload_collection_to_swarm')
    @patch('app.api.endpoints.data.validate_tar')
    @patch('app.api.endpoints.data.count_tar_files')
    def test_manifest_server_timing_header(self, mock_count, mock_validate, mock_upload):
        """Test Server-Timing header in manifest uploads."""
        mock_validate.return_value = None
        mock_count.return_value = 3
        mock_upload.return_value = "manifest123reference"

        tar_bytes = self._create_tar(3)

        client = TestClient(app)
        response = client.post(
            "/api/v1/data/manifest?stamp_id=teststamp",
            files={"file": ("test.tar", io.BytesIO(tar_bytes), "application/x-tar")}
        )

        assert response.status_code == 200
        assert "Server-Timing" in response.headers
        server_timing = response.headers["Server-Timing"]

        # Check manifest-specific metrics in header
        assert "tar-validate-ms" in server_timing
        assert "tar-count-ms" in server_timing
        assert "bee-upload-ms" in server_timing
        assert "ms-per-file" in server_timing
        assert "files-per-second" in server_timing

    @patch('app.api.endpoints.data.upload_collection_to_swarm')
    @patch('app.api.endpoints.data.validate_tar')
    @patch('app.api.endpoints.data.count_tar_files')
    @patch('app.api.endpoints.data.validate_stamp_for_upload')
    def test_manifest_timing_with_stamp_validation(self, mock_stamp, mock_count, mock_validate, mock_upload):
        """Test manifest timing includes stamp validation when enabled."""
        mock_stamp.return_value = None
        mock_validate.return_value = None
        mock_count.return_value = 3
        mock_upload.return_value = "manifest123reference"

        tar_bytes = self._create_tar(3)

        client = TestClient(app)
        response = client.post(
            "/api/v1/data/manifest?stamp_id=teststamp&include_timing=true&validate_stamp=true",
            files={"file": ("test.tar", io.BytesIO(tar_bytes), "application/x-tar")}
        )

        assert response.status_code == 200
        timing = response.json()["timing"]
        assert timing["stamp_validate_ms"] is not None
        assert timing["stamp_validate_ms"] >= 0


class TestServerTimingHeaderFormat:
    """Tests for Server-Timing header format compliance."""

    @patch('app.api.endpoints.data.upload_data_to_swarm')
    def test_header_format_w3c_compliant(self, mock_upload):
        """Test that Server-Timing header follows W3C format."""
        mock_upload.return_value = "abc123reference"

        client = TestClient(app)
        response = client.post(
            "/api/v1/data/?stamp_id=teststamp",
            files={"file": ("test.json", io.BytesIO(b'{"test": true}'), "application/json")}
        )

        assert response.status_code == 200
        server_timing = response.headers["Server-Timing"]

        # W3C format: metric;dur=value, metric2;dur=value2
        # Each metric should have format: name;dur=number
        metrics = server_timing.split(", ")
        for metric in metrics:
            assert ";dur=" in metric
            name, dur_part = metric.split(";dur=")
            assert name.replace("-", "").isalnum()
            float(dur_part)  # Should not raise

    @patch('app.api.endpoints.data.upload_data_to_swarm')
    def test_header_excludes_none_values(self, mock_upload):
        """Test that None timing values are excluded from header."""
        mock_upload.return_value = "abc123reference"

        client = TestClient(app)
        response = client.post(
            "/api/v1/data/?stamp_id=teststamp",
            files={"file": ("test.json", io.BytesIO(b'{"test": true}'), "application/json")}
        )

        assert response.status_code == 200
        server_timing = response.headers["Server-Timing"]

        # stamp_validate_ms should not be in header since validate_stamp=false (default)
        assert "stamp-validate-ms" not in server_timing
