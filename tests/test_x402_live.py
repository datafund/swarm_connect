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
    ALLOW_LIVE_STAMP_PURCHASE - Required (1/true/yes) for test_free_tier_access, which
                               has the gateway buy a real postage batch with its BZZ.
                               Same switch as tests/test_integration_gateway.py.

The payment and free-tier tests read GET /health once and skip unless the gateway
reports x402 enabled (and, for the free-tier tests, the free tier enabled). With
x402 off — the config default, e.g. a local gateway — a well-formed stamp request
is not refused with 402 but goes straight to the handler and buys a batch.

Protected routes validate the request before payment, pricing and the free tier
(#427). A request that is malformed gets 422 before any of those, so a test that
wants a 402 or a free-tier slot has to send a well-formed body.
"""
import os
import pytest
import requests
import json
from typing import Optional

# Skip all tests in this module unless RUN_LIVE_TESTS is set
def pytest_configure(config):
    config.addinivalue_line(
        "markers", "live: mark test as requiring live testnet (deselect with '-m \"not live\"')"
    )


# Check if live tests should run
def should_run_live_tests() -> bool:
    """Check if live tests are enabled via the environment."""
    return _flag("RUN_LIVE_TESTS")


def _flag(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes")


# Spending is a separate decision from running live tests, as in
# tests/test_integration_gateway.py.
ALLOW_LIVE_STAMP_PURCHASE = _flag("ALLOW_LIVE_STAMP_PURCHASE")


# Skip decorator for live tests
live_test = pytest.mark.skipif(
    not should_run_live_tests(),
    reason="Live tests disabled. Set RUN_LIVE_TESTS=1"
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


@pytest.fixture(scope="module")
def gateway_x402() -> dict:
    """The gateway's x402 section of GET /health, read once per module.

    {} when the gateway reports none, which is what a gateway with x402
    disabled does. Skips when the gateway cannot be reached.
    """
    url = f"{gateway_url()}/health"
    try:
        response = requests.get(url, timeout=15)
    except requests.RequestException as e:
        pytest.skip(f"Gateway not reachable at {url}: {e}")
    if response.status_code != 200:
        pytest.skip(f"GET {url} answered {response.status_code}")
    return response.json().get("x402") or {}


@pytest.fixture
def x402_enabled(gateway_x402) -> dict:
    """Skip unless the gateway enforces x402.

    Without it a well-formed POST /stamps/ is not refused with 402: it reaches
    the handler and buys a batch with the gateway's BZZ.
    """
    if gateway_x402.get("enabled") is not True:
        pytest.skip(f"x402 is not enabled on {gateway_url()}; a valid stamp request there would buy a batch")
    return gateway_x402


@pytest.fixture
def free_tier_enabled(x402_enabled) -> dict:
    """Skip unless x402 and its free tier are both enabled."""
    if (x402_enabled.get("free_tier") or {}).get("enabled") is not True:
        pytest.skip(f"The x402 free tier is not enabled on {gateway_url()}")
    return x402_enabled


class TestGatewayHealth:
    """Basic gateway connectivity tests."""

    @live_test
    def test_gateway_is_running(self, gateway_x402):
        """Verify the gateway is reachable (skips via gateway_x402 when it is not)."""
        config = get_test_config()
        response = requests.get(f"{config['gateway_url']}/", timeout=15)
        assert response.status_code == 200

    @live_test
    def test_health_endpoint(self, gateway_x402):
        """Verify health endpoint works (skips via gateway_x402 when unreachable)."""
        config = get_test_config()
        # Try common health endpoint paths
        for path in ["/health", "/api/v1/health", "/"]:
            try:
                response = requests.get(f"{config['gateway_url']}{path}", timeout=15)
                if response.status_code == 200:
                    print(f"Health check OK at {path}")
                    return
            except Exception:
                continue
        pytest.fail("No health endpoint responded")


class TestX402ResponseFormat:
    """Test that 402 responses are correctly formatted."""

    @live_test
    def test_protected_endpoint_returns_402(self, x402_enabled):
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
    def test_402_contains_valid_payment_address(self, x402_enabled):
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
    def test_payment_with_x402_client(self, x402_enabled):
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
    def test_free_tier_access(self, free_tier_enabled):
        """Test that free tier allows limited access.

        A successful request has the gateway BUY A REAL POSTAGE BATCH with its
        own BZZ (on staging, a mainnet batch), so it needs ALLOW_LIVE_STAMP_PURCHASE.
        """
        if not ALLOW_LIVE_STAMP_PURCHASE:
            pytest.skip("Buys a real postage batch on the gateway; set ALLOW_LIVE_STAMP_PURCHASE=1 to run it")
        config = get_test_config()

        # Opt in to the free tier
        response = requests.post(
            f"{config['gateway_url']}/api/v1/stamps/",
            json=VALID_STAMP_BODY,
            headers={"X-Payment-Mode": "free"},
            timeout=180,
        )
        print(f"Free tier stamp request: {response.status_code}")

        if response.status_code == 201:
            assert response.headers.get("X-Payment-Mode") == "free-tier", (
                f"Free-tier success without the free-tier header: {dict(response.headers)}")
            assert response.json().get("batchID"), f"No batchID: {response.text}"
            limit = response.headers.get("X-RateLimit-Limit")
            remaining = response.headers.get("X-RateLimit-Remaining")
            print(f"Rate limit: {remaining}/{limit} remaining")
            return

        body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        detail = body.get("detail") if isinstance(body.get("detail"), dict) else {}
        code = detail.get("code")

        # The free tier's own rate limit: must point at the paid route.
        if response.status_code == 429 and code is None:
            info = body.get("payment_info") or detail.get("payment_info")
            assert info, f"429 should include payment upgrade info: {response.text}"
            print(f"Upgrade info: {info}")
            return

        # The gateway's spending limits refused the purchase: the free tier
        # worked, but whether a batch is handed out was not checked.
        if (response.status_code, code) in {
            (429, "DAILY_SPEND_BUDGET_EXHAUSTED"),
            (503, "GATEWAY_DAILY_SPEND_CEILING"),
        }:
            pytest.skip(f"Not checked: spending limit reached ({code}): {response.text[:200]}")

        pytest.fail(f"Unexpected free-tier response {response.status_code}: {response.text[:500]}")

    @live_test
    def test_free_tier_rate_limit(self, free_tier_enabled):
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

        # The free tier is enabled (the fixture checked), so ten requests must
        # run into its limit. A 402 here would mean the opt-in was ignored.
        assert 429 in results, f"Expected the free tier to rate-limit, got {results}"

    @live_test
    def test_malformed_request_is_refused_before_free_tier(self, free_tier_enabled):
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
