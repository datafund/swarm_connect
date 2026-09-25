"""x402 configuration is checked at startup, and the facilitator can authenticate (#369, #370, #371)."""
import asyncio
import sys
import types

import pytest

from app.core.config import settings
from app.x402 import facilitator

PAY_TO = "0xc87688A40CE2ff1765BA54497c7471c892755488"


@pytest.fixture
def x402(monkeypatch):
    monkeypatch.setattr(settings, "X402_ENABLED", True)
    monkeypatch.setattr(settings, "X402_NETWORK", "base-sepolia")
    monkeypatch.setattr(settings, "X402_PAY_TO_ADDRESS", PAY_TO)
    monkeypatch.setattr(settings, "X402_FACILITATOR_URL", "https://x402.org/facilitator")
    monkeypatch.setattr(settings, "X402_FACILITATOR_CDP_API_KEY_ID", None)
    monkeypatch.setattr(settings, "X402_FACILITATOR_CDP_API_KEY_SECRET", None)
    monkeypatch.setattr(settings, "X402_FACILITATOR_BEARER_TOKEN", None)
    facilitator.reset_facilitator_client()
    yield monkeypatch
    facilitator.reset_facilitator_client()


def test_testnet_with_public_facilitator_is_accepted(x402):
    facilitator.validate_x402_config()


@pytest.mark.parametrize("network", ["base-mainnet", "eip155:8453", "Base", "", "polygon"])
def test_unknown_networks_refuse_to_start(x402, network):
    x402.setattr(settings, "X402_NETWORK", network)
    with pytest.raises(RuntimeError, match="X402_NETWORK"):
        facilitator.validate_x402_config()


@pytest.mark.parametrize("pay_to", [None, "", "0x0", "not-an-address"])
def test_missing_or_malformed_pay_to_refuses_to_start(x402, pay_to):
    x402.setattr(settings, "X402_PAY_TO_ADDRESS", pay_to)
    with pytest.raises(RuntimeError, match="X402_PAY_TO_ADDRESS"):
        facilitator.validate_x402_config()


def test_mainnet_with_the_public_testnet_facilitator_refuses_to_start(x402):
    x402.setattr(settings, "X402_NETWORK", "base")
    with pytest.raises(RuntimeError, match="mainnet"):
        facilitator.validate_x402_config()


def test_mainnet_with_another_facilitator_is_accepted(x402):
    x402.setattr(settings, "X402_NETWORK", "base")
    x402.setattr(settings, "X402_FACILITATOR_URL", "https://facilitator.example.com")
    facilitator.validate_x402_config()


def test_disabled_x402_is_not_checked(x402):
    x402.setattr(settings, "X402_ENABLED", False)
    x402.setattr(settings, "X402_NETWORK", "nonsense")
    facilitator.validate_x402_config()


def test_bearer_token_is_sent_on_verify_and_settle(x402):
    x402.setattr(settings, "X402_FACILITATOR_BEARER_TOKEN", "s3cret")
    config = facilitator.get_facilitator_client().config
    headers = asyncio.run(config["create_headers"]())
    assert headers["verify"]["Authorization"] == "Bearer s3cret"
    assert headers["settle"]["Authorization"] == "Bearer s3cret"


def test_no_auth_by_default(x402):
    assert facilitator.get_facilitator_client().config.get("create_headers") is None


def test_cdp_credentials_without_the_package_refuse_to_start(x402):
    x402.setattr(settings, "X402_FACILITATOR_CDP_API_KEY_ID", "id")
    x402.setattr(settings, "X402_FACILITATOR_CDP_API_KEY_SECRET", "secret")
    x402.setitem(sys.modules, "cdp", None)  # simulate "not installed"
    x402.setitem(sys.modules, "cdp.x402", None)
    with pytest.raises(RuntimeError, match="cdp-sdk"):
        facilitator.validate_x402_config()


def test_cdp_config_is_used_when_available(x402):
    calls = {}

    def create_headers():  # synchronous, as in cdp-sdk
        return {"verify": {"Authorization": "Bearer jwt"}, "settle": {"Authorization": "Bearer jwt"}}

    def create_facilitator_config(key_id, secret):
        calls["args"] = (key_id, secret)
        return {"url": "https://api.cdp.coinbase.com/platform/v2/x402", "create_headers": create_headers}

    fake = types.ModuleType("cdp.x402")
    fake.create_facilitator_config = create_facilitator_config
    x402.setitem(sys.modules, "cdp", types.ModuleType("cdp"))
    x402.setitem(sys.modules, "cdp.x402", fake)
    x402.setattr(settings, "X402_FACILITATOR_CDP_API_KEY_ID", "id")
    x402.setattr(settings, "X402_FACILITATOR_CDP_API_KEY_SECRET", "secret")
    x402.setattr(settings, "X402_NETWORK", "base")
    facilitator.validate_x402_config()
    client = facilitator.get_facilitator_client()
    assert calls["args"] == ("id", "secret")
    assert client.config["url"] == "https://api.cdp.coinbase.com/platform/v2/x402"
    # The x402 client awaits create_headers; cdp-sdk's is synchronous.
    headers = asyncio.run(client.config["create_headers"]())
    assert headers["verify"]["Authorization"] == "Bearer jwt"
