# tests/test_integration_gateway.py
"""
Integration tests against a LIVE gateway. Opt-in, and they do not default to production.

These used to default GATEWAY_URL to https://provenance-gateway.datafund.io and skip
only when that host was unreachable. Production is normally up, so a plain
`pytest tests/` — the mandatory gate before any PR, per CLAUDE.md — fired real
traffic at production on every run.

Two consequences, one worse than the other.

The known one (#233): the free tier allows three writes a minute, the FreeTierPacer
does not fully absorb it when local timing shifts, and tests failed with 429 instead
of the status they asserted. So the gate that everything else depends on was
non-deterministic for reasons unrelated to the change being tested.

The one that was not in the issue: `usable_stamp` PURCHASES A STAMP when it cannot
find a usable local one. That is real BZZ, on production, from a routine local test
run. It has been harmless only because production happens to hold usable stamps.

So this module is now opt-in on three axes, each with its own switch:

    RUN_LIVE_TESTS=1              run these at all
    GATEWAY_URL=...               which gateway (defaults to localhost, not production)
    ALLOW_LIVE_STAMP_PURCHASE=1   permit the fixture to spend

Run with:
    RUN_LIVE_TESTS=1 GATEWAY_URL=https://provenance-gateway.dev.datafund.io \
        pytest tests/test_integration_gateway.py -v -s
"""
import pytest
import requests
import tarfile
import io
import os
import time


def _flag(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes")


# Opt-in, matching tests/test_x402_live.py. Reachability is NOT the gate: whether
# production happens to be up says nothing about whether this run intended to
# touch it.
RUN_LIVE_TESTS = _flag("RUN_LIVE_TESTS")

# Defaults to localhost. A remote target must be named explicitly, so no one
# reaches production by omission.
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8000")

# Spending is a separate decision from running live tests. Opting in to live
# tests should not silently opt in to buying postage batches.
ALLOW_LIVE_STAMP_PURCHASE = _flag("ALLOW_LIVE_STAMP_PURCHASE")

pytestmark = pytest.mark.skipif(
    not RUN_LIVE_TESTS,
    reason="Live gateway tests are opt-in: set RUN_LIVE_TESTS=1 (and GATEWAY_URL).",
)

# Minimum hours for stamp purchase (25h to avoid borderline 24h failures)
MIN_STAMP_HOURS = 25

# Free tier rate limit window (seconds)
FREE_TIER_WINDOW = 60


class FreeTierPacer:
    """Tracks free tier POST requests and sleeps when approaching the rate limit."""

    def __init__(self, limit: int = 3):
        self.limit = limit
        self.timestamps: list[float] = []

    def wait_if_needed(self):
        """Sleep if we'd exceed the rate limit with the next request."""
        now = time.time()
        # Remove timestamps outside the window
        self.timestamps = [t for t in self.timestamps if now - t < FREE_TIER_WINDOW]
        if len(self.timestamps) >= self.limit:
            # Wait for the oldest request to fall out of the window
            sleep_time = FREE_TIER_WINDOW - (now - self.timestamps[0]) + 1
            if sleep_time > 0:
                print(f"\n  Rate limit pacer: sleeping {sleep_time:.0f}s for window reset...")
                time.sleep(sleep_time)
        self.timestamps.append(time.time())


# Module-level pacer instance shared across all tests
_pacer = FreeTierPacer()


def _detect_free_tier_limit() -> int:
    """Query the gateway health endpoint to discover the free tier rate limit."""
    try:
        resp = requests.get(f"{GATEWAY_URL}/health", timeout=10)
        if resp.status_code == 200:
            limit = resp.json().get("x402", {}).get("free_tier", {}).get("rate_limit_per_minute")
            if limit:
                return int(limit)
    except Exception:
        pass
    return 3  # default


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
    """Check if the gateway is available. Also initializes the rate limit pacer."""
    try:
        response = requests.get(f"{GATEWAY_URL}/", timeout=10)
        if response.status_code == 200:
            _pacer.limit = _detect_free_tier_limit()
            return True
        return False
    except requests.RequestException:
        return False


@pytest.fixture(autouse=True)
def pace_free_tier():
    """Auto-fixture that paces POST requests to stay within free tier rate limits."""
    # Runs before each test; actual pacing happens when wait_if_needed() is called
    return _pacer


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

    # No usable local stamp found. Purchasing one costs real BZZ, so it needs its
    # own opt-in — running live tests is not the same decision as spending money,
    # and this fixture is reached by any test that needs a stamp.
    if not ALLOW_LIVE_STAMP_PURCHASE:
        pytest.skip(
            f"No usable local stamp on {GATEWAY_URL} and purchasing is not enabled. "
            "Set ALLOW_LIVE_STAMP_PURCHASE=1 to let this test buy one with real BZZ."
        )

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

        # Purchase stamp via our gateway (use free tier header for x402-enabled gateways)
        _pacer.wait_if_needed()
        purchase_response = requests.post(
            f"{GATEWAY_URL}/api/v1/stamps/",
            json={"amount": amount, "depth": depth},
            headers={"X-Payment-Mode": "free"},
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

        # Upload via manifest endpoint (use free tier header for x402-enabled gateways)
        _pacer.wait_if_needed()
        response = requests.post(
            f"{GATEWAY_URL}/api/v1/data/manifest",
            params={"stamp_id": usable_stamp},
            headers={"X-Payment-Mode": "free"},
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

        _pacer.wait_if_needed()
        response = requests.post(
            f"{GATEWAY_URL}/api/v1/data/manifest",
            params={"stamp_id": usable_stamp},
            headers={"X-Payment-Mode": "free"},
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

        _pacer.wait_if_needed()
        response = requests.post(
            f"{GATEWAY_URL}/api/v1/data/manifest",
            params={"stamp_id": usable_stamp},
            headers={"X-Payment-Mode": "free"},
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

        _pacer.wait_if_needed()
        response = requests.post(
            f"{GATEWAY_URL}/api/v1/data/manifest",
            headers={"X-Payment-Mode": "free"},
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

        _pacer.wait_if_needed()
        response = requests.post(
            f"{GATEWAY_URL}/api/v1/data/",
            params={"stamp_id": usable_stamp},
            headers={"X-Payment-Mode": "free"},
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
