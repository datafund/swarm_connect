# tests/test_manifest_upload.py
"""
Tests for manifest/collection upload functionality.
Tests TAR archive uploads with Swarm-Collection header.
"""
import pytest
import io
import tarfile
from unittest.mock import patch
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def create_tar_archive(files: dict[str, bytes]) -> bytes:
    """
    Create a TAR archive from a dictionary of filename -> content.

    Args:
        files: Dictionary mapping filenames to file contents

    Returns:
        TAR archive as bytes
    """
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode='w') as tar:
        for filename, content in files.items():
            file_buffer = io.BytesIO(content)
            tarinfo = tarfile.TarInfo(name=filename)
            tarinfo.size = len(content)
            tar.addfile(tarinfo, file_buffer)
    tar_buffer.seek(0)
    return tar_buffer.read()


class TestManifestUploadBasics:
    """Test basic manifest upload functionality."""

    @patch('app.api.endpoints.data.upload_collection_to_swarm')
    def test_successful_manifest_upload(self, mock_upload):
        """Test successful upload of valid TAR archive."""
        mock_upload.return_value = "manifest_reference_abc123"

        # Create test TAR with multiple files
        files = {
            "file1.json": b'{"id": 1}',
            "file2.json": b'{"id": 2}',
            "file3.json": b'{"id": 3}'
        }
        tar_bytes = create_tar_archive(files)

        response = client.post(
            "/api/v1/data/manifest?stamp_id=test_stamp",
            files={"file": ("files.tar", io.BytesIO(tar_bytes), "application/x-tar")}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["reference"] == "manifest_reference_abc123"
        assert data["file_count"] == 3
        assert "3 files" in data["message"]
        mock_upload.assert_called_once()

    @patch('app.api.endpoints.data.upload_collection_to_swarm')
    def test_single_file_tar(self, mock_upload):
        """Test upload of TAR with single file."""
        mock_upload.return_value = "single_file_ref"

        files = {"single.txt": b"Single file content"}
        tar_bytes = create_tar_archive(files)

        response = client.post(
            "/api/v1/data/manifest?stamp_id=test_stamp",
            files={"file": ("single.tar", io.BytesIO(tar_bytes), "application/x-tar")}
        )

        assert response.status_code == 200
        assert response.json()["file_count"] == 1

    @patch('app.api.endpoints.data.upload_collection_to_swarm')
    def test_nested_directory_tar(self, mock_upload):
        """Test upload of TAR with nested directory structure."""
        mock_upload.return_value = "nested_ref"

        files = {
            "root.txt": b"Root file",
            "subdir/nested.txt": b"Nested file",
            "subdir/deeper/deep.txt": b"Deep nested file"
        }
        tar_bytes = create_tar_archive(files)

        response = client.post(
            "/api/v1/data/manifest?stamp_id=test_stamp",
            files={"file": ("nested.tar", io.BytesIO(tar_bytes), "application/x-tar")}
        )

        assert response.status_code == 200
        assert response.json()["file_count"] == 3

    @patch('app.api.endpoints.data.upload_collection_to_swarm')
    def test_large_file_count(self, mock_upload):
        """Test upload of TAR with many files."""
        mock_upload.return_value = "many_files_ref"

        # Create TAR with 100 files
        files = {f"file_{i}.json": f'{{"id": {i}}}'.encode() for i in range(100)}
        tar_bytes = create_tar_archive(files)

        response = client.post(
            "/api/v1/data/manifest?stamp_id=test_stamp",
            files={"file": ("many.tar", io.BytesIO(tar_bytes), "application/x-tar")}
        )

        assert response.status_code == 200
        assert response.json()["file_count"] == 100


class TestManifestUploadValidation:
    """Test TAR validation and error handling."""

    def test_empty_tar(self):
        """Test upload of empty TAR archive (no files)."""
        # Create TAR with no files
        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode='w') as tar:
            pass  # Empty archive
        tar_buffer.seek(0)
        tar_bytes = tar_buffer.read()

        response = client.post(
            "/api/v1/data/manifest?stamp_id=test_stamp",
            files={"file": ("empty.tar", io.BytesIO(tar_bytes), "application/x-tar")}
        )

        assert response.status_code == 400
        assert "no files" in response.json()["detail"].lower()

    def test_invalid_tar_data(self):
        """Test upload of invalid TAR data."""
        invalid_data = b"This is not a valid TAR archive"

        response = client.post(
            "/api/v1/data/manifest?stamp_id=test_stamp",
            files={"file": ("invalid.tar", io.BytesIO(invalid_data), "application/x-tar")}
        )

        assert response.status_code == 400
        assert "invalid tar" in response.json()["detail"].lower()

    def test_corrupted_tar(self):
        """Test upload of corrupted TAR archive."""
        # Start with valid TAR header but truncate
        files = {"file.txt": b"content"}
        tar_bytes = create_tar_archive(files)
        corrupted = tar_bytes[:50]  # Truncate

        response = client.post(
            "/api/v1/data/manifest?stamp_id=test_stamp",
            files={"file": ("corrupted.tar", io.BytesIO(corrupted), "application/x-tar")}
        )

        assert response.status_code == 400

    def test_missing_stamp_id(self):
        """Test upload without stamp_id parameter."""
        files = {"file.txt": b"content"}
        tar_bytes = create_tar_archive(files)

        response = client.post(
            "/api/v1/data/manifest",
            files={"file": ("files.tar", io.BytesIO(tar_bytes), "application/x-tar")}
        )

        assert response.status_code == 422  # Validation error

    def test_missing_file(self):
        """Test upload without file."""
        response = client.post("/api/v1/data/manifest?stamp_id=test_stamp")

        assert response.status_code == 422  # Validation error

    def test_tar_with_only_directories(self):
        """Test upload of TAR containing only directories (no files)."""
        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode='w') as tar:
            # Add empty directory
            dirinfo = tarfile.TarInfo(name="empty_dir/")
            dirinfo.type = tarfile.DIRTYPE
            tar.addfile(dirinfo)
        tar_buffer.seek(0)
        tar_bytes = tar_buffer.read()

        response = client.post(
            "/api/v1/data/manifest?stamp_id=test_stamp",
            files={"file": ("dirs_only.tar", io.BytesIO(tar_bytes), "application/x-tar")}
        )

        assert response.status_code == 400
        assert "no files" in response.json()["detail"].lower()


class TestManifestUploadErrors:
    """Test error handling for manifest uploads."""

    @patch('app.api.endpoints.data.upload_collection_to_swarm')
    def test_swarm_api_error(self, mock_upload):
        """Test handling of Swarm API errors."""
        from requests.exceptions import RequestException
        mock_upload.side_effect = RequestException("Swarm API unavailable")

        files = {"file.txt": b"content"}
        tar_bytes = create_tar_archive(files)

        response = client.post(
            "/api/v1/data/manifest?stamp_id=test_stamp",
            files={"file": ("files.tar", io.BytesIO(tar_bytes), "application/x-tar")}
        )

        assert response.status_code == 502
        assert "Failed to upload collection to Swarm" in response.json()["detail"]

    @patch('app.api.endpoints.data.upload_collection_to_swarm')
    def test_unexpected_error(self, mock_upload):
        """Test handling of unexpected errors."""
        mock_upload.side_effect = Exception("Unexpected error")

        files = {"file.txt": b"content"}
        tar_bytes = create_tar_archive(files)

        response = client.post(
            "/api/v1/data/manifest?stamp_id=test_stamp",
            files={"file": ("files.tar", io.BytesIO(tar_bytes), "application/x-tar")}
        )

        assert response.status_code == 500
        assert "Internal server error" in response.json()["detail"]


class TestTarHelperFunctions:
    """Test TAR helper functions directly."""

    def test_validate_tar_valid(self):
        """Test validate_tar with valid TAR."""
        from app.services.swarm_api import validate_tar

        files = {"file.txt": b"content"}
        tar_bytes = create_tar_archive(files)

        # Should not raise
        validate_tar(tar_bytes)

    def test_validate_tar_empty(self):
        """Test validate_tar with empty TAR."""
        from app.services.swarm_api import validate_tar

        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode='w') as tar:
            pass
        tar_buffer.seek(0)
        tar_bytes = tar_buffer.read()

        with pytest.raises(ValueError, match="no files"):
            validate_tar(tar_bytes)

    def test_validate_tar_invalid(self):
        """Test validate_tar with invalid data."""
        from app.services.swarm_api import validate_tar

        with pytest.raises(ValueError, match="Invalid TAR"):
            validate_tar(b"not a tar")

    def test_count_tar_files(self):
        """Test count_tar_files function."""
        from app.services.swarm_api import count_tar_files

        files = {
            "file1.txt": b"content1",
            "file2.txt": b"content2",
            "subdir/file3.txt": b"content3"
        }
        tar_bytes = create_tar_archive(files)

        assert count_tar_files(tar_bytes) == 3

    def test_count_tar_files_with_dirs(self):
        """Test count_tar_files excludes directories."""
        from app.services.swarm_api import count_tar_files

        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode='w') as tar:
            # Add directory
            dirinfo = tarfile.TarInfo(name="mydir/")
            dirinfo.type = tarfile.DIRTYPE
            tar.addfile(dirinfo)

            # Add file
            file_content = b"content"
            fileinfo = tarfile.TarInfo(name="mydir/file.txt")
            fileinfo.size = len(file_content)
            tar.addfile(fileinfo, io.BytesIO(file_content))

        tar_buffer.seek(0)
        tar_bytes = tar_buffer.read()

        # Should count only the file, not the directory
        assert count_tar_files(tar_bytes) == 1


class TestEdgeCases:
    """Test edge cases for manifest uploads."""

    @patch('app.api.endpoints.data.upload_collection_to_swarm')
    def test_special_characters_in_filename(self, mock_upload):
        """Test TAR with special characters in filenames."""
        mock_upload.return_value = "special_chars_ref"

        files = {
            "file with spaces.json": b'{"id": 1}',
            "file-with-dashes.json": b'{"id": 2}',
            "file_with_underscores.json": b'{"id": 3}',
        }
        tar_bytes = create_tar_archive(files)

        response = client.post(
            "/api/v1/data/manifest?stamp_id=test_stamp",
            files={"file": ("special.tar", io.BytesIO(tar_bytes), "application/x-tar")}
        )

        assert response.status_code == 200
        assert response.json()["file_count"] == 3

    @patch('app.api.endpoints.data.upload_collection_to_swarm')
    def test_unicode_filenames(self, mock_upload):
        """Test TAR with unicode characters in filenames."""
        mock_upload.return_value = "unicode_ref"

        files = {
            "données.json": b'{"id": 1}',
            "文件.json": b'{"id": 2}',
        }
        tar_bytes = create_tar_archive(files)

        response = client.post(
            "/api/v1/data/manifest?stamp_id=test_stamp",
            files={"file": ("unicode.tar", io.BytesIO(tar_bytes), "application/x-tar")}
        )

        assert response.status_code == 200
        assert response.json()["file_count"] == 2

    @patch('app.api.endpoints.data.upload_collection_to_swarm')
    def test_binary_file_content(self, mock_upload):
        """Test TAR with binary file content."""
        mock_upload.return_value = "binary_ref"

        # Create files with various binary content
        files = {
            "image.bin": bytes(range(256)),  # All possible byte values
            "null_bytes.bin": b'\x00\x00\x00',
            "random.bin": b'\xde\xad\xbe\xef',
        }
        tar_bytes = create_tar_archive(files)

        response = client.post(
            "/api/v1/data/manifest?stamp_id=test_stamp",
            files={"file": ("binary.tar", io.BytesIO(tar_bytes), "application/x-tar")}
        )

        assert response.status_code == 200
        assert response.json()["file_count"] == 3


class TestCompressedTar:
    """Test handling of compressed TAR archives."""

    @patch('app.api.endpoints.data.upload_collection_to_swarm')
    def test_gzip_compressed_tar(self, mock_upload):
        """Test upload of gzip-compressed TAR (.tar.gz)."""
        mock_upload.return_value = "gzip_ref"

        files = {"file.txt": b"content"}

        # Create gzip compressed TAR
        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode='w:gz') as tar:
            for filename, content in files.items():
                file_buffer = io.BytesIO(content)
                tarinfo = tarfile.TarInfo(name=filename)
                tarinfo.size = len(content)
                tar.addfile(tarinfo, file_buffer)
        tar_buffer.seek(0)
        tar_bytes = tar_buffer.read()

        response = client.post(
            "/api/v1/data/manifest?stamp_id=test_stamp",
            files={"file": ("files.tar.gz", io.BytesIO(tar_bytes), "application/gzip")}
        )

        assert response.status_code == 200

    @patch('app.api.endpoints.data.upload_collection_to_swarm')
    def test_bz2_compressed_tar(self, mock_upload):
        """Test upload of bz2-compressed TAR (.tar.bz2)."""
        mock_upload.return_value = "bz2_ref"

        files = {"file.txt": b"content"}

        # Create bz2 compressed TAR
        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode='w:bz2') as tar:
            for filename, content in files.items():
                file_buffer = io.BytesIO(content)
                tarinfo = tarfile.TarInfo(name=filename)
                tarinfo.size = len(content)
                tar.addfile(tarinfo, file_buffer)
        tar_buffer.seek(0)
        tar_bytes = tar_buffer.read()

        response = client.post(
            "/api/v1/data/manifest?stamp_id=test_stamp",
            files={"file": ("files.tar.bz2", io.BytesIO(tar_bytes), "application/x-bzip2")}
        )

        assert response.status_code == 200
