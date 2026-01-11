# tests/test_redundancy_level.py
"""
Tests for configurable redundancy level (erasure coding) feature.
Tests valid and invalid redundancy levels for data and manifest uploads.
"""
import pytest
import json
import io
import tarfile
from unittest.mock import patch, call
from fastapi.testclient import TestClient

from app.main import app
from app.services.swarm_api import (
    REDUNDANCY_LEVELS,
    DEFAULT_REDUNDANCY_LEVEL,
    validate_redundancy_level
)

client = TestClient(app)


def create_tar_archive(files: dict[str, bytes]) -> bytes:
    """Create a TAR archive from a dictionary of filename -> content."""
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode='w') as tar:
        for filename, content in files.items():
            file_buffer = io.BytesIO(content)
            tarinfo = tarfile.TarInfo(name=filename)
            tarinfo.size = len(content)
            tar.addfile(tarinfo, file_buffer)
    tar_buffer.seek(0)
    return tar_buffer.read()


class TestValidateRedundancyLevelFunction:
    """Test the validate_redundancy_level helper function directly."""

    def test_valid_levels(self):
        """Test that all valid levels (0-4) pass validation."""
        for level in range(5):
            # Should not raise
            validate_redundancy_level(level)

    def test_invalid_level_negative(self):
        """Test that negative levels raise ValueError."""
        with pytest.raises(ValueError, match="Invalid redundancy level -1"):
            validate_redundancy_level(-1)

    def test_invalid_level_too_high(self):
        """Test that levels above 4 raise ValueError."""
        with pytest.raises(ValueError, match="Invalid redundancy level 5"):
            validate_redundancy_level(5)

        with pytest.raises(ValueError, match="Invalid redundancy level 10"):
            validate_redundancy_level(10)

    def test_error_message_includes_valid_options(self):
        """Test that error message lists all valid options."""
        with pytest.raises(ValueError) as exc_info:
            validate_redundancy_level(99)

        error_msg = str(exc_info.value)
        # Check that all valid levels are mentioned
        for level, name in REDUNDANCY_LEVELS.items():
            assert f"{level}={name}" in error_msg


class TestRedundancyConstants:
    """Test redundancy level constants."""

    def test_redundancy_levels_dict(self):
        """Test that REDUNDANCY_LEVELS contains expected values."""
        assert REDUNDANCY_LEVELS[0] == "none"
        assert REDUNDANCY_LEVELS[1] == "medium"
        assert REDUNDANCY_LEVELS[2] == "strong"
        assert REDUNDANCY_LEVELS[3] == "insane"
        assert REDUNDANCY_LEVELS[4] == "paranoid"
        assert len(REDUNDANCY_LEVELS) == 5

    def test_default_redundancy_level(self):
        """Test that default redundancy level is 2 (strong)."""
        assert DEFAULT_REDUNDANCY_LEVEL == 2


class TestDataUploadRedundancy:
    """Test redundancy parameter for /api/v1/data/ endpoint."""

    @patch('app.api.endpoints.data.upload_data_to_swarm')
    def test_upload_without_redundancy_uses_default(self, mock_upload):
        """Test that upload without redundancy parameter uses default level."""
        mock_upload.return_value = "test_reference"

        test_data = {"test": "data"}
        json_content = json.dumps(test_data).encode('utf-8')

        files = {"file": ("test.json", io.BytesIO(json_content), "application/json")}
        response = client.post(
            "/api/v1/data/?stamp_id=test_stamp",
            files=files
        )

        assert response.status_code == 200
        # Verify upload was called without explicit redundancy (uses default)
        mock_upload.assert_called_once()
        call_kwargs = mock_upload.call_args[1]
        assert call_kwargs.get('redundancy_level') is None

    @patch('app.api.endpoints.data.upload_data_to_swarm')
    def test_upload_with_redundancy_level_0(self, mock_upload):
        """Test upload with redundancy level 0 (none)."""
        mock_upload.return_value = "test_reference"

        test_data = {"test": "data"}
        json_content = json.dumps(test_data).encode('utf-8')

        files = {"file": ("test.json", io.BytesIO(json_content), "application/json")}
        response = client.post(
            "/api/v1/data/?stamp_id=test_stamp&redundancy=0",
            files=files
        )

        assert response.status_code == 200
        call_kwargs = mock_upload.call_args[1]
        assert call_kwargs.get('redundancy_level') == 0

    @patch('app.api.endpoints.data.upload_data_to_swarm')
    def test_upload_with_redundancy_level_4(self, mock_upload):
        """Test upload with redundancy level 4 (paranoid)."""
        mock_upload.return_value = "test_reference"

        test_data = {"test": "data"}
        json_content = json.dumps(test_data).encode('utf-8')

        files = {"file": ("test.json", io.BytesIO(json_content), "application/json")}
        response = client.post(
            "/api/v1/data/?stamp_id=test_stamp&redundancy=4",
            files=files
        )

        assert response.status_code == 200
        call_kwargs = mock_upload.call_args[1]
        assert call_kwargs.get('redundancy_level') == 4

    @patch('app.api.endpoints.data.upload_data_to_swarm')
    def test_upload_with_all_valid_redundancy_levels(self, mock_upload):
        """Test upload with each valid redundancy level (0-4)."""
        mock_upload.return_value = "test_reference"

        test_data = {"test": "data"}
        json_content = json.dumps(test_data).encode('utf-8')

        for level in range(5):
            mock_upload.reset_mock()
            files = {"file": ("test.json", io.BytesIO(json_content), "application/json")}
            response = client.post(
                f"/api/v1/data/?stamp_id=test_stamp&redundancy={level}",
                files=files
            )

            assert response.status_code == 200, f"Failed for redundancy level {level}"
            call_kwargs = mock_upload.call_args[1]
            assert call_kwargs.get('redundancy_level') == level

    def test_upload_with_invalid_redundancy_level_5(self):
        """Test upload with invalid redundancy level 5 returns 400."""
        test_data = {"test": "data"}
        json_content = json.dumps(test_data).encode('utf-8')

        files = {"file": ("test.json", io.BytesIO(json_content), "application/json")}
        response = client.post(
            "/api/v1/data/?stamp_id=test_stamp&redundancy=5",
            files=files
        )

        assert response.status_code == 400
        assert "Invalid redundancy level 5" in response.json()["detail"]
        assert "Must be 0-4" in response.json()["detail"]

    def test_upload_with_invalid_redundancy_level_negative(self):
        """Test upload with negative redundancy level returns 400."""
        test_data = {"test": "data"}
        json_content = json.dumps(test_data).encode('utf-8')

        files = {"file": ("test.json", io.BytesIO(json_content), "application/json")}
        response = client.post(
            "/api/v1/data/?stamp_id=test_stamp&redundancy=-1",
            files=files
        )

        assert response.status_code == 400
        assert "Invalid redundancy level -1" in response.json()["detail"]

    def test_upload_with_invalid_redundancy_level_large(self):
        """Test upload with very large redundancy level returns 400."""
        test_data = {"test": "data"}
        json_content = json.dumps(test_data).encode('utf-8')

        files = {"file": ("test.json", io.BytesIO(json_content), "application/json")}
        response = client.post(
            "/api/v1/data/?stamp_id=test_stamp&redundancy=100",
            files=files
        )

        assert response.status_code == 400
        assert "Invalid redundancy level 100" in response.json()["detail"]


class TestManifestUploadRedundancy:
    """Test redundancy parameter for /api/v1/data/manifest endpoint."""

    @patch('app.api.endpoints.data.upload_collection_to_swarm')
    def test_manifest_without_redundancy_uses_default(self, mock_upload):
        """Test that manifest upload without redundancy uses default level."""
        mock_upload.return_value = "manifest_reference"

        files = {"file.txt": b"content"}
        tar_bytes = create_tar_archive(files)

        response = client.post(
            "/api/v1/data/manifest?stamp_id=test_stamp",
            files={"file": ("files.tar", io.BytesIO(tar_bytes), "application/x-tar")}
        )

        assert response.status_code == 200
        call_kwargs = mock_upload.call_args[1]
        assert call_kwargs.get('redundancy_level') is None

    @patch('app.api.endpoints.data.upload_collection_to_swarm')
    def test_manifest_with_redundancy_level_0(self, mock_upload):
        """Test manifest upload with redundancy level 0 (none)."""
        mock_upload.return_value = "manifest_reference"

        files = {"file.txt": b"content"}
        tar_bytes = create_tar_archive(files)

        response = client.post(
            "/api/v1/data/manifest?stamp_id=test_stamp&redundancy=0",
            files={"file": ("files.tar", io.BytesIO(tar_bytes), "application/x-tar")}
        )

        assert response.status_code == 200
        call_kwargs = mock_upload.call_args[1]
        assert call_kwargs.get('redundancy_level') == 0

    @patch('app.api.endpoints.data.upload_collection_to_swarm')
    def test_manifest_with_redundancy_level_4(self, mock_upload):
        """Test manifest upload with redundancy level 4 (paranoid)."""
        mock_upload.return_value = "manifest_reference"

        files = {"file.txt": b"content"}
        tar_bytes = create_tar_archive(files)

        response = client.post(
            "/api/v1/data/manifest?stamp_id=test_stamp&redundancy=4",
            files={"file": ("files.tar", io.BytesIO(tar_bytes), "application/x-tar")}
        )

        assert response.status_code == 200
        call_kwargs = mock_upload.call_args[1]
        assert call_kwargs.get('redundancy_level') == 4

    @patch('app.api.endpoints.data.upload_collection_to_swarm')
    def test_manifest_with_all_valid_redundancy_levels(self, mock_upload):
        """Test manifest upload with each valid redundancy level (0-4)."""
        mock_upload.return_value = "manifest_reference"

        for level in range(5):
            mock_upload.reset_mock()
            files = {"file.txt": b"content"}
            tar_bytes = create_tar_archive(files)

            response = client.post(
                f"/api/v1/data/manifest?stamp_id=test_stamp&redundancy={level}",
                files={"file": ("files.tar", io.BytesIO(tar_bytes), "application/x-tar")}
            )

            assert response.status_code == 200, f"Failed for redundancy level {level}"
            call_kwargs = mock_upload.call_args[1]
            assert call_kwargs.get('redundancy_level') == level

    def test_manifest_with_invalid_redundancy_level_5(self):
        """Test manifest upload with invalid redundancy level 5 returns 400."""
        files = {"file.txt": b"content"}
        tar_bytes = create_tar_archive(files)

        response = client.post(
            "/api/v1/data/manifest?stamp_id=test_stamp&redundancy=5",
            files={"file": ("files.tar", io.BytesIO(tar_bytes), "application/x-tar")}
        )

        assert response.status_code == 400
        assert "Invalid redundancy level 5" in response.json()["detail"]

    def test_manifest_with_invalid_redundancy_level_negative(self):
        """Test manifest upload with negative redundancy level returns 400."""
        files = {"file.txt": b"content"}
        tar_bytes = create_tar_archive(files)

        response = client.post(
            "/api/v1/data/manifest?stamp_id=test_stamp&redundancy=-1",
            files={"file": ("files.tar", io.BytesIO(tar_bytes), "application/x-tar")}
        )

        assert response.status_code == 400
        assert "Invalid redundancy level -1" in response.json()["detail"]


class TestRedundancyWithOtherParameters:
    """Test redundancy parameter combined with other upload parameters."""

    @patch('app.api.endpoints.data.upload_data_to_swarm')
    def test_redundancy_with_deferred(self, mock_upload):
        """Test redundancy parameter combined with deferred upload."""
        mock_upload.return_value = "test_reference"

        test_data = {"test": "data"}
        json_content = json.dumps(test_data).encode('utf-8')

        files = {"file": ("test.json", io.BytesIO(json_content), "application/json")}
        response = client.post(
            "/api/v1/data/?stamp_id=test_stamp&deferred=true&redundancy=3",
            files=files
        )

        assert response.status_code == 200
        call_kwargs = mock_upload.call_args[1]
        assert call_kwargs.get('deferred') is True
        assert call_kwargs.get('redundancy_level') == 3

    @patch('app.api.endpoints.data.upload_data_to_swarm')
    def test_redundancy_with_include_timing(self, mock_upload):
        """Test redundancy parameter combined with include_timing."""
        mock_upload.return_value = "test_reference"

        test_data = {"test": "data"}
        json_content = json.dumps(test_data).encode('utf-8')

        files = {"file": ("test.json", io.BytesIO(json_content), "application/json")}
        response = client.post(
            "/api/v1/data/?stamp_id=test_stamp&include_timing=true&redundancy=1",
            files=files
        )

        assert response.status_code == 200
        data = response.json()
        assert "timing" in data
        call_kwargs = mock_upload.call_args[1]
        assert call_kwargs.get('redundancy_level') == 1

    @patch('app.api.endpoints.data.upload_data_to_swarm')
    def test_redundancy_with_custom_content_type(self, mock_upload):
        """Test redundancy parameter combined with custom content type."""
        mock_upload.return_value = "test_reference"

        binary_content = b'\x89PNG\r\n\x1a\n'

        files = {"file": ("test.png", io.BytesIO(binary_content), "image/png")}
        response = client.post(
            "/api/v1/data/?stamp_id=test_stamp&content_type=image/png&redundancy=2",
            files=files
        )

        assert response.status_code == 200
        call_kwargs = mock_upload.call_args[1]
        assert call_kwargs.get('content_type') == "image/png"
        assert call_kwargs.get('redundancy_level') == 2

    @patch('app.api.endpoints.data.upload_collection_to_swarm')
    def test_manifest_redundancy_with_deferred(self, mock_upload):
        """Test manifest redundancy parameter combined with deferred upload."""
        mock_upload.return_value = "manifest_reference"

        files = {"file.txt": b"content"}
        tar_bytes = create_tar_archive(files)

        response = client.post(
            "/api/v1/data/manifest?stamp_id=test_stamp&deferred=true&redundancy=3",
            files={"file": ("files.tar", io.BytesIO(tar_bytes), "application/x-tar")}
        )

        assert response.status_code == 200
        call_kwargs = mock_upload.call_args[1]
        assert call_kwargs.get('deferred') is True
        assert call_kwargs.get('redundancy_level') == 3


class TestServiceLayerRedundancy:
    """Test redundancy handling in service layer functions."""

    @patch('app.services.swarm_api.requests.post')
    def test_upload_data_to_swarm_default_redundancy(self, mock_post):
        """Test that upload_data_to_swarm uses default redundancy when not specified."""
        from app.services.swarm_api import upload_data_to_swarm

        mock_response = mock_post.return_value
        mock_response.status_code = 200
        mock_response.json.return_value = {"reference": "test_ref"}

        upload_data_to_swarm(
            data=b"test data",
            stamp_id="test_stamp",
            content_type="text/plain"
        )

        # Check that Swarm-Redundancy-Level header was set to default (2)
        call_kwargs = mock_post.call_args[1]
        assert call_kwargs["headers"]["Swarm-Redundancy-Level"] == "2"

    @patch('app.services.swarm_api.requests.post')
    def test_upload_data_to_swarm_custom_redundancy(self, mock_post):
        """Test that upload_data_to_swarm passes custom redundancy level."""
        from app.services.swarm_api import upload_data_to_swarm

        mock_response = mock_post.return_value
        mock_response.status_code = 200
        mock_response.json.return_value = {"reference": "test_ref"}

        upload_data_to_swarm(
            data=b"test data",
            stamp_id="test_stamp",
            content_type="text/plain",
            redundancy_level=4
        )

        # Check that Swarm-Redundancy-Level header was set to 4
        call_kwargs = mock_post.call_args[1]
        assert call_kwargs["headers"]["Swarm-Redundancy-Level"] == "4"

    @patch('app.services.swarm_api.requests.post')
    def test_upload_data_to_swarm_level_0(self, mock_post):
        """Test that upload_data_to_swarm correctly handles level 0 (no erasure coding)."""
        from app.services.swarm_api import upload_data_to_swarm

        mock_response = mock_post.return_value
        mock_response.status_code = 200
        mock_response.json.return_value = {"reference": "test_ref"}

        upload_data_to_swarm(
            data=b"test data",
            stamp_id="test_stamp",
            content_type="text/plain",
            redundancy_level=0
        )

        # Check that Swarm-Redundancy-Level header was set to 0
        call_kwargs = mock_post.call_args[1]
        assert call_kwargs["headers"]["Swarm-Redundancy-Level"] == "0"

    @patch('app.services.swarm_api.requests.post')
    def test_upload_collection_to_swarm_custom_redundancy(self, mock_post):
        """Test that upload_collection_to_swarm passes custom redundancy level."""
        from app.services.swarm_api import upload_collection_to_swarm

        mock_response = mock_post.return_value
        mock_response.status_code = 200
        mock_response.json.return_value = {"reference": "test_ref"}

        files = {"file.txt": b"content"}
        tar_bytes = create_tar_archive(files)

        upload_collection_to_swarm(
            tar_data=tar_bytes,
            stamp_id="test_stamp",
            redundancy_level=3
        )

        # Check that Swarm-Redundancy-Level header was set to 3
        call_kwargs = mock_post.call_args[1]
        assert call_kwargs["headers"]["Swarm-Redundancy-Level"] == "3"

    def test_upload_data_to_swarm_invalid_redundancy(self):
        """Test that upload_data_to_swarm raises ValueError for invalid level."""
        from app.services.swarm_api import upload_data_to_swarm

        with pytest.raises(ValueError, match="Invalid redundancy level"):
            upload_data_to_swarm(
                data=b"test data",
                stamp_id="test_stamp",
                content_type="text/plain",
                redundancy_level=99
            )

    def test_upload_collection_to_swarm_invalid_redundancy(self):
        """Test that upload_collection_to_swarm raises ValueError for invalid level."""
        from app.services.swarm_api import upload_collection_to_swarm

        files = {"file.txt": b"content"}
        tar_bytes = create_tar_archive(files)

        with pytest.raises(ValueError, match="Invalid redundancy level"):
            upload_collection_to_swarm(
                tar_data=tar_bytes,
                stamp_id="test_stamp",
                redundancy_level=-5
            )
