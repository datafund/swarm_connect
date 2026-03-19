# tests/test_deferred_upload.py
"""Tests for deferred upload mode functionality."""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
import io

VALID_STAMP_ID = "a" * 64


class TestUploadDataDeferredHeader:
    """Tests for deferred header in upload_data_to_swarm."""

    @pytest.mark.asyncio
    async def test_default_deferred_false(self):
        """Test that default deferred=false sends Swarm-Deferred-Upload: false header."""
        from app.services.swarm_api import upload_data_to_swarm

        mock_response = MagicMock()
        mock_response.json.return_value = {"reference": "abc123"}
        mock_response.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch('app.services.swarm_api.get_client', return_value=mock_client):
            await upload_data_to_swarm(b"test data", "stamp123", "application/json")

        # Check that the header was sent with deferred=false
        call_args = mock_client.post.call_args
        headers = call_args.kwargs.get('headers', call_args[1].get('headers', {}))
        assert headers.get("Swarm-Deferred-Upload") == "false"

    @pytest.mark.asyncio
    async def test_deferred_true_sends_header(self):
        """Test that deferred=true sends Swarm-Deferred-Upload: true header."""
        from app.services.swarm_api import upload_data_to_swarm

        mock_response = MagicMock()
        mock_response.json.return_value = {"reference": "abc123"}
        mock_response.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch('app.services.swarm_api.get_client', return_value=mock_client):
            await upload_data_to_swarm(b"test data", "stamp123", "application/json", deferred=True)

        # Check that the header was sent with deferred=true
        call_args = mock_client.post.call_args
        headers = call_args.kwargs.get('headers', call_args[1].get('headers', {}))
        assert headers.get("Swarm-Deferred-Upload") == "true"

    @pytest.mark.asyncio
    async def test_deferred_false_explicit(self):
        """Test that explicit deferred=false sends Swarm-Deferred-Upload: false header."""
        from app.services.swarm_api import upload_data_to_swarm

        mock_response = MagicMock()
        mock_response.json.return_value = {"reference": "abc123"}
        mock_response.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch('app.services.swarm_api.get_client', return_value=mock_client):
            await upload_data_to_swarm(b"test data", "stamp123", "application/json", deferred=False)

        # Check that the header was sent with deferred=false
        call_args = mock_client.post.call_args
        headers = call_args.kwargs.get('headers', call_args[1].get('headers', {}))
        assert headers.get("Swarm-Deferred-Upload") == "false"


class TestUploadCollectionDeferredHeader:
    """Tests for deferred header in upload_collection_to_swarm."""

    @pytest.mark.asyncio
    async def test_default_deferred_false(self):
        """Test that default deferred=false sends Swarm-Deferred-Upload: false header."""
        from app.services.swarm_api import upload_collection_to_swarm
        import tarfile
        import io

        # Create a valid TAR archive
        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode='w') as tar:
            data = b"test content"
            info = tarfile.TarInfo(name="test.txt")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        tar_bytes = tar_buffer.getvalue()

        mock_response = MagicMock()
        mock_response.json.return_value = {"reference": "abc123"}
        mock_response.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch('app.services.swarm_api.get_client', return_value=mock_client):
            await upload_collection_to_swarm(tar_bytes, "stamp123")

        # Check that the header was sent with deferred=false
        call_args = mock_client.post.call_args
        headers = call_args.kwargs.get('headers', call_args[1].get('headers', {}))
        assert headers.get("Swarm-Deferred-Upload") == "false"

    @pytest.mark.asyncio
    async def test_deferred_true_sends_header(self):
        """Test that deferred=true sends Swarm-Deferred-Upload: true header."""
        from app.services.swarm_api import upload_collection_to_swarm
        import tarfile
        import io

        # Create a valid TAR archive
        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode='w') as tar:
            data = b"test content"
            info = tarfile.TarInfo(name="test.txt")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        tar_bytes = tar_buffer.getvalue()

        mock_response = MagicMock()
        mock_response.json.return_value = {"reference": "abc123"}
        mock_response.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch('app.services.swarm_api.get_client', return_value=mock_client):
            await upload_collection_to_swarm(tar_bytes, "stamp123", deferred=True)

        # Check that the header was sent with deferred=true
        call_args = mock_client.post.call_args
        headers = call_args.kwargs.get('headers', call_args[1].get('headers', {}))
        assert headers.get("Swarm-Deferred-Upload") == "true"


class TestDataEndpointDeferredParameter:
    """Tests for deferred parameter in data upload endpoint."""

    @patch('app.api.endpoints.data.upload_data_to_swarm', new_callable=AsyncMock)
    def test_endpoint_default_deferred_false(self, mock_upload):
        """Test that endpoint defaults to deferred=false."""
        from fastapi.testclient import TestClient
        from app.main import app

        mock_upload.return_value = "abc123reference"

        client = TestClient(app)
        response = client.post(
            f"/api/v1/data/?stamp_id={VALID_STAMP_ID}",
            files={"file": ("test.json", io.BytesIO(b'{"test": true}'), "application/json")}
        )

        assert response.status_code == 200
        # Verify deferred=False was passed
        mock_upload.assert_called_once()
        call_kwargs = mock_upload.call_args.kwargs
        assert call_kwargs.get('deferred') is False

    @patch('app.api.endpoints.data.upload_data_to_swarm', new_callable=AsyncMock)
    def test_endpoint_deferred_true(self, mock_upload):
        """Test that endpoint passes deferred=true when specified."""
        from fastapi.testclient import TestClient
        from app.main import app

        mock_upload.return_value = "abc123reference"

        client = TestClient(app)
        response = client.post(
            f"/api/v1/data/?stamp_id={VALID_STAMP_ID}&deferred=true",
            files={"file": ("test.json", io.BytesIO(b'{"test": true}'), "application/json")}
        )

        assert response.status_code == 200
        # Verify deferred=True was passed
        mock_upload.assert_called_once()
        call_kwargs = mock_upload.call_args.kwargs
        assert call_kwargs.get('deferred') is True

    @patch('app.api.endpoints.data.upload_data_to_swarm', new_callable=AsyncMock)
    def test_endpoint_deferred_false_explicit(self, mock_upload):
        """Test that endpoint passes deferred=false when explicitly specified."""
        from fastapi.testclient import TestClient
        from app.main import app

        mock_upload.return_value = "abc123reference"

        client = TestClient(app)
        response = client.post(
            f"/api/v1/data/?stamp_id={VALID_STAMP_ID}&deferred=false",
            files={"file": ("test.json", io.BytesIO(b'{"test": true}'), "application/json")}
        )

        assert response.status_code == 200
        # Verify deferred=False was passed
        mock_upload.assert_called_once()
        call_kwargs = mock_upload.call_args.kwargs
        assert call_kwargs.get('deferred') is False


class TestManifestEndpointDeferredParameter:
    """Tests for deferred parameter in manifest upload endpoint."""

    @patch('app.api.endpoints.data.upload_collection_to_swarm', new_callable=AsyncMock)
    @patch('app.api.endpoints.data.validate_tar')
    @patch('app.api.endpoints.data.count_tar_files')
    def test_manifest_default_deferred_false(self, mock_count, mock_validate, mock_upload):
        """Test that manifest endpoint defaults to deferred=false."""
        from fastapi.testclient import TestClient
        from app.main import app
        import tarfile

        # Create a valid TAR archive
        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode='w') as tar:
            data = b"test content"
            info = tarfile.TarInfo(name="test.txt")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        tar_bytes = tar_buffer.getvalue()

        mock_validate.return_value = None
        mock_count.return_value = 1
        mock_upload.return_value = "abc123reference"

        client = TestClient(app)
        response = client.post(
            f"/api/v1/data/manifest?stamp_id={VALID_STAMP_ID}",
            files={"file": ("test.tar", io.BytesIO(tar_bytes), "application/x-tar")}
        )

        assert response.status_code == 200
        # Verify deferred=False was passed
        mock_upload.assert_called_once()
        call_kwargs = mock_upload.call_args.kwargs
        assert call_kwargs.get('deferred') is False

    @patch('app.api.endpoints.data.upload_collection_to_swarm', new_callable=AsyncMock)
    @patch('app.api.endpoints.data.validate_tar')
    @patch('app.api.endpoints.data.count_tar_files')
    def test_manifest_deferred_true(self, mock_count, mock_validate, mock_upload):
        """Test that manifest endpoint passes deferred=true when specified."""
        from fastapi.testclient import TestClient
        from app.main import app
        import tarfile

        # Create a valid TAR archive
        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode='w') as tar:
            data = b"test content"
            info = tarfile.TarInfo(name="test.txt")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        tar_bytes = tar_buffer.getvalue()

        mock_validate.return_value = None
        mock_count.return_value = 1
        mock_upload.return_value = "abc123reference"

        client = TestClient(app)
        response = client.post(
            f"/api/v1/data/manifest?stamp_id={VALID_STAMP_ID}&deferred=true",
            files={"file": ("test.tar", io.BytesIO(tar_bytes), "application/x-tar")}
        )

        assert response.status_code == 200
        # Verify deferred=True was passed
        mock_upload.assert_called_once()
        call_kwargs = mock_upload.call_args.kwargs
        assert call_kwargs.get('deferred') is True
