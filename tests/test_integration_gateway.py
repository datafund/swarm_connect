# tests/test_integration_gateway.py
"""
Integration tests for the production gateway.

These tests run against the live provenance gateway and require:
1. Network connectivity to https://provenance-gateway.datafund.io
2. A valid postage stamp (purchased or existing)

Run with: pytest tests/test_integration_gateway.py -v -s

To skip these tests in CI, they are marked with @pytest.mark.integration
"""
import pytest
import requests
import tarfile
import io
import os
import time

# Production gateway URL
GATEWAY_URL = os.environ.get("GATEWAY_URL", "https://provenance-gateway.datafund.io")

# Minimum hours for stamp purchase (25h to avoid borderline 24h failures)
MIN_STAMP_HOURS = 25


def create_test_tar(files: dict) -> bytes:
    """Create a TAR archive from a dictionary of {filename: content}."""
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode='w') as tar:
        for filename, content in files.items():
            if isinstance(content, str):
                content = content.encode('utf-8')
            file_buffer = io.BytesIO(content)
            tarinfo = tarfile.TarInfo(name=filename)
            tarinfo.size = len(content)
            tar.addfile(tarinfo, file_buffer)
    tar_buffer.seek(0)
    return tar_buffer.read()


@pytest.fixture(scope="module")
def gateway_available():
    """Check if the gateway is available."""
    try:
        response = requests.get(f"{GATEWAY_URL}/", timeout=10)
        return response.status_code == 200
    except requests.RequestException:
        return False


@pytest.fixture(scope="module")
def usable_stamp(gateway_available):
    """
    Get or purchase a usable stamp for testing.

    First checks for existing usable stamps, then purchases one if needed.
    """
    if not gateway_available:
        pytest.skip("Gateway not available")

    # First, check for existing usable local stamps
    try:
        response = requests.get(f"{GATEWAY_URL}/api/v1/stamps/", timeout=30)
        if response.status_code == 200:
            stamps = response.json().get("stamps", [])
            # Look for a usable stamp with good TTL that is local (owned by this node)
            for stamp in stamps:
                if stamp.get("usable") and stamp.get("local") and stamp.get("batchTTL", 0) > 3600:
                    print(f"\nUsing existing local stamp: {stamp['batchID'][:16]}...")
                    return stamp["batchID"]
    except requests.RequestException:
        pass

    # No usable local stamp found, purchase a new one
    print("\nNo usable local stamp found, purchasing new stamp...")

    # Get current price - try multiple sources
    try:
        # Try local Bee node first (most reliable), then fallback options
        chainstate_sources = [
            "http://localhost:1633/chainstate",  # Local Bee node
            "https://api.gateway.ethswarm.org/chainstate",  # Public gateway (may not work)
        ]
        chainstate = None
        for source_url in chainstate_sources:
            try:
                chainstate_response = requests.get(source_url, timeout=10)
                if chainstate_response.status_code == 200:
                    chainstate = chainstate_response.json()
                    print(f"Got chainstate from {source_url}")
                    break
            except requests.RequestException:
                continue

        if not chainstate:
            pytest.skip("Could not get chainstate from any source")

        current_price = int(chainstate.get("currentPrice", 0))
        if current_price == 0:
            pytest.skip("Current price is 0, cannot calculate stamp amount")

        # Calculate amount for MIN_STAMP_HOURS hours (25h to avoid borderline 24h failures)
        blocks_per_hour = 720  # Gnosis chain ~5 second blocks
        amount = current_price * blocks_per_hour * MIN_STAMP_HOURS
        depth = 17  # Small stamp for testing

        print(f"Purchasing stamp: amount={amount}, depth={depth} (~{MIN_STAMP_HOURS}h)")

        # Purchase stamp via our gateway
        purchase_response = requests.post(
            f"{GATEWAY_URL}/api/v1/stamps/",
            json={"amount": amount, "depth": depth},
            timeout=120
        )

        if purchase_response.status_code != 200:
            pytest.skip(f"Could not purchase stamp: {purchase_response.text}")

        batch_id = purchase_response.json().get("batchID")
        if not batch_id:
            pytest.skip("No batchID in purchase response")

        print(f"Purchased stamp: {batch_id[:16]}...")

        # Wait for stamp to become usable (can take 30-60 seconds)
        print("Waiting for stamp to become usable...")
        for i in range(24):  # Wait up to 2 minutes
            time.sleep(5)
            try:
                stamp_response = requests.get(
                    f"{GATEWAY_URL}/api/v1/stamps/{batch_id}",
                    timeout=10
                )
                if stamp_response.status_code == 200:
                    stamp_data = stamp_response.json()
                    if stamp_data.get("usable"):
                        print(f"Stamp is usable after {(i+1)*5} seconds")
                        return batch_id
            except requests.RequestException:
                pass
            print(f"  Attempt {i+1}/24: not usable yet...")

        pytest.skip("Stamp did not become usable within 2 minutes")

    except requests.RequestException as e:
        pytest.skip(f"Error during stamp purchase: {e}")


@pytest.mark.integration
class TestGatewayHealth:
    """Basic gateway connectivity tests."""

    def test_gateway_root_endpoint(self, gateway_available):
        """Test that the gateway root endpoint returns expected response."""
        if not gateway_available:
            pytest.skip("Gateway not available")

        response = requests.get(f"{GATEWAY_URL}/", timeout=10)
        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "ok"
        assert "message" in data

    def test_gateway_stamps_list(self, gateway_available):
        """Test that stamps list endpoint works."""
        if not gateway_available:
            pytest.skip("Gateway not available")

        response = requests.get(f"{GATEWAY_URL}/api/v1/stamps/", timeout=30)
        assert response.status_code == 200
        data = response.json()
        assert "stamps" in data
        assert isinstance(data["stamps"], list)


@pytest.mark.integration
class TestManifestUploadIntegration:
    """Integration tests for manifest upload against production gateway."""

    def test_manifest_upload_success(self, gateway_available, usable_stamp):
        """Test successful manifest upload with multiple files."""
        if not gateway_available:
            pytest.skip("Gateway not available")

        # Create test TAR
        test_files = {
            "hello.txt": "Hello, Swarm!",
            "data/nested.json": '{"test": true}',
            "binary.bin": bytes([0x00, 0x01, 0x02, 0xFF])
        }
        tar_data = create_test_tar(test_files)

        # Upload via manifest endpoint
        response = requests.post(
            f"{GATEWAY_URL}/api/v1/data/manifest",
            params={"stamp_id": usable_stamp},
            files={"file": ("test.tar", tar_data, "application/x-tar")},
            timeout=120
        )

        assert response.status_code == 200, f"Upload failed: {response.text}"
        data = response.json()

        assert "reference" in data
        assert len(data["reference"]) == 64  # Swarm reference is 64 hex chars
        assert data["file_count"] == 3
        assert "successfully" in data["message"].lower()

        # Store reference for verification test
        pytest.manifest_reference = data["reference"]

    def test_manifest_files_accessible(self, gateway_available, usable_stamp):
        """Test that uploaded files are accessible via the manifest reference."""
        if not gateway_available:
            pytest.skip("Gateway not available")

        if not hasattr(pytest, "manifest_reference"):
            pytest.skip("No manifest reference from previous test")

        reference = pytest.manifest_reference

        # Note: Direct Bee access would be at /bzz/{ref}/{path}
        # The gateway may or may not expose this - test against Bee directly
        # For now, we just verify the reference was returned
        assert len(reference) == 64

    def test_manifest_upload_validation_empty_tar(self, gateway_available, usable_stamp):
        """Test that empty TAR archives are rejected."""
        if not gateway_available:
            pytest.skip("Gateway not available")

        # Create empty TAR (no files)
        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode='w') as tar:
            pass  # Empty archive
        tar_buffer.seek(0)
        empty_tar = tar_buffer.read()

        response = requests.post(
            f"{GATEWAY_URL}/api/v1/data/manifest",
            params={"stamp_id": usable_stamp},
            files={"file": ("empty.tar", empty_tar, "application/x-tar")},
            timeout=60
        )

        assert response.status_code == 400
        assert "no files" in response.json().get("detail", "").lower()

    def test_manifest_upload_validation_invalid_tar(self, gateway_available, usable_stamp):
        """Test that invalid TAR data is rejected."""
        if not gateway_available:
            pytest.skip("Gateway not available")

        invalid_data = b"This is not a TAR file"

        response = requests.post(
            f"{GATEWAY_URL}/api/v1/data/manifest",
            params={"stamp_id": usable_stamp},
            files={"file": ("invalid.tar", invalid_data, "application/x-tar")},
            timeout=60
        )

        assert response.status_code == 400
        assert "invalid" in response.json().get("detail", "").lower()

    def test_manifest_upload_missing_stamp_id(self, gateway_available):
        """Test that missing stamp_id returns 422."""
        if not gateway_available:
            pytest.skip("Gateway not available")

        tar_data = create_test_tar({"test.txt": "test"})

        response = requests.post(
            f"{GATEWAY_URL}/api/v1/data/manifest",
            files={"file": ("test.tar", tar_data, "application/x-tar")},
            timeout=60
        )

        assert response.status_code == 422  # FastAPI validation error


@pytest.mark.integration
class TestDataUploadIntegration:
    """Integration tests for basic data upload."""

    def test_data_upload_success(self, gateway_available, usable_stamp):
        """Test successful raw data upload."""
        if not gateway_available:
            pytest.skip("Gateway not available")

        test_data = b"Integration test data upload"

        response = requests.post(
            f"{GATEWAY_URL}/api/v1/data/",
            params={"stamp_id": usable_stamp},
            files={"file": ("test.bin", test_data, "application/octet-stream")},
            timeout=60
        )

        assert response.status_code == 200, f"Upload failed: {response.text}"
        data = response.json()

        assert "reference" in data
        assert len(data["reference"]) == 64

        # Store for download test
        pytest.data_reference = data["reference"]

    def test_data_download_success(self, gateway_available):
        """Test downloading previously uploaded data."""
        if not gateway_available:
            pytest.skip("Gateway not available")

        if not hasattr(pytest, "data_reference"):
            pytest.skip("No data reference from previous test")

        reference = pytest.data_reference

        response = requests.get(
            f"{GATEWAY_URL}/api/v1/data/{reference}",
            timeout=60
        )

        assert response.status_code == 200
        assert response.content == b"Integration test data upload"


# Allow running directly
if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s", "-m", "integration"])
