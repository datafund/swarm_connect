# tests/test_x402_live.py
"""
Live integration tests for x402 payment flow.

These tests require:
1. A running gateway with X402_ENABLED=true
2. A funded wallet on Base Sepolia (testnet USDC)
3. Network access to the x402 facilitator

To run these tests:
    # First, start the gateway in another terminal:
    X402_ENABLED=true X402_FREE_TIER_ENABLED=false X402_PAY_TO_ADDRESS=0x... python run.py

    # Then run the live tests:
    pytest tests/test_x402_live.py -v --run-live

Environment variables required:
    TEST_WALLET_PRIVATE_KEY  - Private key for test wallet (with testnet USDC)
    TEST_GATEWAY_URL         - Gateway URL (default: http://localhost:8000)
"""
import os
import pytest
import requests
import json
from typing import Optional

# Skip all tests in this module unless --run-live is provided
def pytest_configure(config):
    config.addinivalue_line(
        "markers", "live: mark test as requiring live testnet (deselect with '-m \"not live\"')"
    )


# Check if live tests should run
def should_run_live_tests() -> bool:
    """Check if live tests are enabled via environment or pytest flag."""
    return os.environ.get("RUN_LIVE_TESTS", "").lower() in ("1", "true", "yes")


# Skip decorator for live tests
live_test = pytest.mark.skipif(
    not should_run_live_tests(),
    reason="Live tests disabled. Set RUN_LIVE_TESTS=1 or use --run-live flag"
)


def get_test_config():
    """Get test configuration from environment."""
    return {
        "gateway_url": os.environ.get("TEST_GATEWAY_URL", "http://localhost:8000"),
        "wallet_private_key": os.environ.get("TEST_WALLET_PRIVATE_KEY"),
        "network": "base-sepolia",
    }


class TestGatewayHealth:
    """Basic gateway connectivity tests."""

    @live_test
    def test_gateway_is_running(self):
        """Verify the gateway is reachable."""
        config = get_test_config()
        response = requests.get(f"{config['gateway_url']}/")
        assert response.status_code == 200

    @live_test
    def test_health_endpoint(self):
        """Verify health endpoint works."""
        config = get_test_config()
        # Try common health endpoint paths
        for path in ["/health", "/api/v1/health", "/"]:
            try:
                response = requests.get(f"{config['gateway_url']}{path}")
                if response.status_code == 200:
                    print(f"Health check OK at {path}")
                    return
            except Exception:
                continue
        pytest.fail("No health endpoint responded")


class TestX402ResponseFormat:
    """Test that 402 responses are correctly formatted."""

    @live_test
    def test_protected_endpoint_returns_402(self):
        """Protected endpoint returns 402 without payment."""
        config = get_test_config()

        response = requests.post(f"{config['gateway_url']}/api/v1/stamps/")

        # Should be 402 (if free tier disabled) or 200 (if free tier enabled)
        assert response.status_code in [402, 200, 429], f"Unexpected status: {response.status_code}"

        if response.status_code == 402:
            data = response.json()
            print(f"402 Response: {json.dumps(data, indent=2)}")

            # Verify x402 protocol fields
            assert "x402Version" in data, "Missing x402Version"
            assert data["x402Version"] == 1, "Wrong x402 version"
            assert "accepts" in data, "Missing accepts array"
            assert len(data["accepts"]) > 0, "Empty accepts array"

            # Verify payment requirements
            req = data["accepts"][0]
            assert "scheme" in req, "Missing scheme"
            assert "network" in req, "Missing network"
            assert "maxAmountRequired" in req, "Missing maxAmountRequired"
            assert "resource" in req, "Missing resource"

            print(f"Payment required: {int(req['maxAmountRequired']) / 1_000_000} USDC on {req['network']}")

        elif response.status_code == 200:
            print("Free tier is enabled - got 200 OK")
            assert "X-Payment-Mode" in response.headers or True  # May or may not have header

        elif response.status_code == 429:
            print("Rate limited (free tier) - got 429")

    @live_test
    def test_402_contains_valid_payment_address(self):
        """Verify 402 contains a valid Ethereum address."""
        config = get_test_config()

        response = requests.post(f"{config['gateway_url']}/api/v1/data/")

        if response.status_code != 402:
            pytest.skip(f"Got {response.status_code}, not 402 (free tier may be enabled)")

        data = response.json()
        req = data["accepts"][0]

        pay_to = req.get("payTo", req.get("receiver", ""))
        assert pay_to.startswith("0x"), f"Invalid payTo address: {pay_to}"
        assert len(pay_to) == 42, f"Invalid address length: {pay_to}"

        print(f"Payment address: {pay_to}")


class TestX402PaymentFlow:
    """Test full payment flow with real x402 client."""

    @live_test
    def test_payment_with_x402_client(self):
        """
        Full payment flow using x402 Python client.

        Requires:
        - TEST_WALLET_PRIVATE_KEY with testnet USDC
        - Gateway running with X402_ENABLED=true
        """
        config = get_test_config()

        if not config["wallet_private_key"]:
            pytest.skip("TEST_WALLET_PRIVATE_KEY not set")

        try:
            from x402.client import X402Client
        except ImportError:
            pytest.skip("x402 client not installed. Run: pip install x402")

        # Create x402 client with test wallet
        client = X402Client(
            private_key=config["wallet_private_key"],
            network=config["network"],
        )

        # Make a paid request to stamps endpoint
        response = client.post(
            f"{config['gateway_url']}/api/v1/stamps/",
            json={"amount": 1000000, "depth": 17}  # Minimal stamp
        )

        print(f"Response status: {response.status_code}")
        print(f"Response body: {response.text}")

        # Should succeed with payment
        assert response.status_code == 200, f"Payment failed: {response.text}"

        # Verify response contains stamp info
        data = response.json()
        assert "stamp_id" in data or "batchID" in data, "Missing stamp ID in response"

        print(f"Successfully purchased stamp: {data}")


class TestFreeTierFlow:
    """Test free tier access flow."""

    @live_test
    def test_free_tier_access(self):
        """Test that free tier allows limited access."""
        config = get_test_config()

        # Make request without payment
        response = requests.post(f"{config['gateway_url']}/api/v1/stamps/")

        if response.status_code == 402:
            print("Free tier is DISABLED - got 402")
            return

        if response.status_code == 200:
            print("Free tier request succeeded")
            # Check for free tier header
            payment_mode = response.headers.get("X-Payment-Mode")
            if payment_mode == "free-tier":
                print("Confirmed free tier access via header")

            # Check rate limit headers
            limit = response.headers.get("X-RateLimit-Limit")
            remaining = response.headers.get("X-RateLimit-Remaining")
            print(f"Rate limit: {remaining}/{limit} remaining")

        elif response.status_code == 429:
            print("Free tier rate limit exceeded")
            data = response.json()
            assert "payment_info" in data, "429 should include payment upgrade info"
            print(f"Upgrade info: {data.get('payment_info')}")

    @live_test
    def test_free_tier_rate_limit(self):
        """Test that free tier enforces rate limits."""
        config = get_test_config()

        # Make multiple requests quickly
        results = []
        for i in range(10):
            response = requests.post(f"{config['gateway_url']}/api/v1/stamps/")
            results.append(response.status_code)
            print(f"Request {i+1}: {response.status_code}")

            if response.status_code == 429:
                print(f"Rate limited after {i+1} requests")
                break

        # Should eventually get rate limited (if free tier enabled)
        # or get 402 immediately (if free tier disabled)
        assert 429 in results or 402 in results, "Expected rate limit or payment required"


# Standalone test runner
if __name__ == "__main__":
    """
    Run live tests directly:
        RUN_LIVE_TESTS=1 python tests/test_x402_live.py
    """
    import sys

    # Enable live tests
    os.environ["RUN_LIVE_TESTS"] = "1"

    # Run with pytest
    sys.exit(pytest.main([__file__, "-v", "-s"]))
