# tests/test_x402_live.py
"""
Live integration tests for x402 payment flow.

These tests require:
1. A running gateway with X402_ENABLED=true
2. A funded wallet on Base Sepolia (testnet USDC)
3. Network access to the x402 facilitator

To run these tests:
    RUN_LIVE_TESTS=1 pytest tests/test_x402_live.py -v -s                    # staging
    RUN_LIVE_TESTS=1 GATEWAY_URL=http://localhost:8000 pytest ... -v -s     # local

Environment variables:
    RUN_LIVE_TESTS           - Required (1/true/yes); otherwise every test is skipped
    GATEWAY_URL              - Gateway URL (default: staging, never production).
                               The same variable tests/test_integration_gateway.py
                               reads. TEST_GATEWAY_URL is accepted as a legacy alias.
    TEST_WALLET_PRIVATE_KEY  - Private key for test wallet (with testnet USDC);
                               only test_payment_with_x402_client needs it, and it pays.

Protected routes validate the request before payment, pricing and the free tier
(#427). A request that is malformed gets 422 before any of those, so a test that
wants a 402 or a free-tier slot has to send a well-formed body.
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


def payment_required_body(response) -> dict:
    """The x402 402 body. The spec puts it at the top level; older gateway
    versions nest it under "detail", so accept either."""
    data = response.json()
    return data if "accepts" in data else data["detail"]


# Defaults to staging, like tests/test_integration_gateway.py, so one variable
# points both live modules at the same gateway. Production is never a default.
STAGING_URL = "https://provenance-gateway.dev.datafund.io"


def gateway_url() -> str:
    """GATEWAY_URL, else the legacy TEST_GATEWAY_URL, else staging."""
    return (os.environ.get("GATEWAY_URL")
            or os.environ.get("TEST_GATEWAY_URL")
            or STAGING_URL).rstrip("/")


# A well-formed stamp purchase. Since #427 a malformed body is refused with 422
# before payment is considered, so tests that expect a 402 must send this.
# Without a payment header or the free-tier opt-in it is refused with 402 before
# the handler runs, so nothing is bought.
VALID_STAMP_BODY = {"size": "small", "duration_hours": 25}


def get_test_config():
    """Get test configuration from environment."""
    return {
        "gateway_url": gateway_url(),
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

        response = requests.post(
            f"{config['gateway_url']}/api/v1/stamps/", json=VALID_STAMP_BODY, timeout=30)

        # A valid request without a payment or free-tier header is always 402
        assert response.status_code == 402, f"Unexpected status: {response.status_code}"

        if response.status_code == 402:
            data = payment_required_body(response)
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


    @live_test
    def test_402_contains_valid_payment_address(self):
        """Verify 402 contains a valid Ethereum address."""
        config = get_test_config()

        response = requests.post(
            f"{config['gateway_url']}/api/v1/stamps/", json=VALID_STAMP_BODY, timeout=30)

        assert response.status_code == 402, f"Unexpected status: {response.status_code}"

        req = payment_required_body(response)["accepts"][0]

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

        # Uses the runnable client sample, so the documented flow is what is tested.
        import sys
        from pathlib import Path
        from eth_account import Account
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "docs" / "samples"))
        from x402_client import Budget, call

        response = call(
            "POST", f"{config['gateway_url']}/api/v1/stamps/",
            json={"size": "small", "duration_hours": 25},
            paid=True, account=Account.from_key(config["wallet_private_key"]),
            budget=Budget(0.10, 0.10), network=config["network"], pay_to=None,
        )

        print(f"Response status: {response.status_code}")
        print(f"Response body: {response.text}")

        # Stamp purchase answers 201 Created
        assert response.status_code == 201, f"Payment failed: {response.text}"
        assert "X-PAYMENT-RESPONSE" in response.headers, "Missing settlement proof"

        data = response.json()
        assert "batchID" in data, "Missing batchID in response"

        print(f"Successfully purchased stamp: {data}")


class TestFreeTierFlow:
    """Test free tier access flow."""

    @live_test
    def test_free_tier_access(self):
        """Test that free tier allows limited access."""
        config = get_test_config()

        # Opt in to the free tier
        response = requests.post(
            f"{config['gateway_url']}/api/v1/stamps/",
            json={"size": "small", "duration_hours": 25},
            headers={"X-Payment-Mode": "free"},
        )

        if response.status_code == 402:
            print("Free tier is DISABLED - got 402")
            return

        if response.ok:
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
            info = data.get("payment_info") or data.get("detail", {}).get("payment_info")
            assert info, "429 should include payment upgrade info"
            print(f"Upgrade info: {info}")

    @live_test
    def test_free_tier_rate_limit(self):
        """Test that free tier enforces rate limits."""
        config = get_test_config()

        # A well-formed free-tier upload to a batch nobody owns. It passes
        # validation, so it takes a free-tier slot; the handler then refuses it
        # (403, the batch is not the caller's) before anything is uploaded or
        # bought. A malformed body would not do: since #427 it gets 422 before
        # the free-tier check and never counts against the limit.
        unowned_batch = "0" * 64

        results = []
        for i in range(10):
            response = requests.post(
                f"{config['gateway_url']}/api/v1/data/",
                params={"stamp_id": unowned_batch},
                headers={"X-Payment-Mode": "free"},
                files={"file": ("rate-limit.txt", b"rate limit probe", "text/plain")},
                timeout=30,
            )
            results.append(response.status_code)
            print(f"Request {i+1}: {response.status_code}")
            assert response.status_code not in (200, 201), (
                f"Upload to an unowned batch succeeded: {response.text}")

            if response.status_code == 429:
                print(f"Rate limited after {i+1} requests")
                break

        # Should eventually get rate limited (if free tier enabled)
        # or get 402 immediately (if free tier disabled)
        assert 429 in results or 402 in results, f"Expected rate limit or payment required, got {results}"

    @live_test
    def test_malformed_request_is_refused_before_free_tier(self):
        """A malformed free-tier request gets 422 and takes no free-tier slot (#427).

        More requests than the free tier allows in a minute, all malformed: every
        one is 422, none 429. Holds even when the window is already used up,
        because validation comes before the free-tier check.
        """
        config = get_test_config()

        results = []
        for _ in range(5):
            response = requests.post(
                f"{config['gateway_url']}/api/v1/stamps/",
                json={"depth": 99},
                headers={"X-Payment-Mode": "free"},
                timeout=30,
            )
            results.append(response.status_code)

        assert results == [422] * len(results), f"Expected only 422, got {results}"


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
