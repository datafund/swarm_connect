"""x402 configuration is checked at startup, and the facilitator can authenticate (#369, #370, #371)."""
import asyncio
import re
import sys
import types
from pathlib import Path

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
    x402.setattr(settings, "X402_FACILITATOR_URL", "")  # production: no default URL
    facilitator.validate_x402_config()
    client = facilitator.get_facilitator_client()
    assert calls["args"] == ("id", "secret")
    assert client.config["url"] == "https://api.cdp.coinbase.com/platform/v2/x402"
    # The x402 client awaits create_headers; cdp-sdk's is synchronous.
    headers = asyncio.run(client.config["create_headers"]())
    assert headers["verify"]["Authorization"] == "Bearer jwt"



def _fake_cdp(x402):
    def create_facilitator_config(key_id, secret):
        return {"url": "https://api.cdp.coinbase.com/platform/v2/x402", "create_headers": lambda: {}}
    fake = types.ModuleType("cdp.x402")
    fake.create_facilitator_config = create_facilitator_config
    x402.setitem(sys.modules, "cdp", types.ModuleType("cdp"))
    x402.setitem(sys.modules, "cdp.x402", fake)
    x402.setattr(settings, "X402_FACILITATOR_CDP_API_KEY_ID", "id")
    x402.setattr(settings, "X402_FACILITATOR_CDP_API_KEY_SECRET", "secret")


def test_cdp_credentials_are_never_sent_to_another_host(x402):
    _fake_cdp(x402)
    x402.setattr(settings, "X402_FACILITATOR_URL", "https://facilitator.example.com")
    with pytest.raises(RuntimeError, match="CDP credentials"):
        facilitator.validate_x402_config()


# #527: the x402 client calls {url}/verify and {url}/settle, and cdp-sdk signs
# each token for /platform/v2/x402/verify and /settle. Any other URL on the CDP
# host sends a token signed for one path to another, so every payment fails.
@pytest.mark.parametrize("url", [
    "https://api.cdp.coinbase.com",
    "https://api.cdp.coinbase.com/",
    "https://api.cdp.coinbase.com/platform/v2",
    "https://api.cdp.coinbase.com/platform/v1/x402",
    "https://api.cdp.coinbase.com/platform/v2/x402/verify",
    "https://api.cdp.coinbase.com/platform/v2/x402//",
    "http://api.cdp.coinbase.com/platform/v2/x402",
    "https://api.cdp.coinbase.com:443/platform/v2/x402",
    "https://api.cdp.coinbase.com/platform/v2/x402?probe=1",
    "https://api.cdp.coinbase.com/platform/v2/x402#probe",
])
def test_cdp_url_not_on_the_signed_route_is_refused(x402, url):
    _fake_cdp(x402)
    x402.setattr(settings, "X402_NETWORK", "base")
    x402.setattr(settings, "X402_FACILITATOR_URL", url)
    with pytest.raises(RuntimeError, match="X402_FACILITATOR_URL is not CDP's x402 route") as e:
        facilitator.validate_x402_config()
    message = str(e.value)
    assert facilitator.CDP_FACILITATOR_URL in message
    # The deploy log is public: the configured value is never echoed.
    assert "probe" not in message and ":443" not in message and "/platform/v1" not in message


@pytest.mark.parametrize("url", [
    "",
    "https://api.cdp.coinbase.com/platform/v2/x402",
    "https://api.cdp.coinbase.com/platform/v2/x402/",  # the x402 client drops one trailing slash
])
def test_cdp_url_empty_or_the_signed_route_is_accepted(x402, url):
    _fake_cdp(x402)
    x402.setattr(settings, "X402_NETWORK", "base")
    x402.setattr(settings, "X402_FACILITATOR_URL", url)
    facilitator.validate_x402_config()
    assert facilitator.get_facilitator_client().config["url"] == facilitator.CDP_FACILITATOR_URL


def test_cdp_config_never_uses_the_configured_url(x402):
    """Even if the startup check were bypassed, CDP requests go to the signed route."""
    _fake_cdp(x402)
    x402.setattr(settings, "X402_FACILITATOR_URL", "https://api.cdp.coinbase.com")
    assert facilitator._config()["url"] == "https://api.cdp.coinbase.com/platform/v2/x402"


def test_cdp_and_bearer_together_are_refused(x402):
    _fake_cdp(x402)
    x402.setattr(settings, "X402_FACILITATOR_URL", "")
    x402.setattr(settings, "X402_FACILITATOR_BEARER_TOKEN", "t")
    with pytest.raises(RuntimeError, match="not both"):
        facilitator.validate_x402_config()


@pytest.mark.parametrize("pay_to,ok", [
    ("0x0000000000000000000000000000000000000000", False),
    ("0xc87688A40CE2ff1765BA54497c7471c892755489", False),   # checksum typo
    ("0xc87688a40ce2ff1765ba54497c7471c892755488", True),    # all lowercase: no checksum to check
    (PAY_TO, True),
])
def test_pay_to_zero_and_checksum(x402, pay_to, ok):
    x402.setattr(settings, "X402_PAY_TO_ADDRESS", pay_to)
    if ok:
        facilitator.validate_x402_config()
    else:
        with pytest.raises(RuntimeError, match="X402_PAY_TO_ADDRESS"):
            facilitator.validate_x402_config()


def test_plain_http_is_refused_on_mainnet_or_with_a_token(x402):
    x402.setattr(settings, "X402_NETWORK", "base")
    x402.setattr(settings, "X402_FACILITATOR_URL", "http://facilitator.example.com")
    with pytest.raises(RuntimeError, match="https"):
        facilitator.validate_x402_config()


def test_network_whitespace_is_normalised_by_settings():
    from app.core.config import Settings
    s = Settings(SWARM_BEE_API_URL="http://localhost:1", X402_NETWORK=" base-sepolia ")
    assert s.X402_NETWORK == "base-sepolia"


def test_startup_runs_the_check(x402):
    """The app lifespan calls validate_x402_config()."""
    from fastapi.testclient import TestClient
    from app.main import app
    x402.setattr(settings, "X402_NETWORK", "nonsense")
    with pytest.raises(RuntimeError, match="Refusing to start"):
        with TestClient(app):
            pass


# cdp-sdk ships in the image (#513). Without it, a cutover with CDP credentials
# is refused at startup, so it must not drop out of the lockfile unnoticed.
ROOT = Path(__file__).resolve().parent.parent


def _pin(text, name):
    match = re.search(rf"^{re.escape(name)}==(\S+)", text, re.MULTILINE)
    return match.group(1) if match else None


def test_cdp_sdk_is_pinned_in_requirements_and_the_lock():
    wanted = _pin((ROOT / "requirements.txt").read_text(), "cdp-sdk")
    assert wanted, "requirements.txt must pin cdp-sdk (the CDP facilitator route needs it)"
    lock = (ROOT / "requirements.lock").read_text()
    assert _pin(lock, "cdp-sdk") == wanted, "requirements.lock is out of date: regenerate it"
    entry = lock.split(f"\ncdp-sdk=={wanted}", 1)[1].split("# via", 1)[0]
    assert "--hash=sha256:" in entry, "the Docker build installs with --require-hashes"


def test_real_cdp_sdk_config_is_accepted(x402):
    # Runs where cdp-sdk is installed (the image, CI); skipped otherwise. Never
    # calls create_headers, so no token is signed and nothing goes on the network.
    pytest.importorskip("cdp.x402")
    x402.setattr(settings, "X402_FACILITATOR_CDP_API_KEY_ID", "id")
    x402.setattr(settings, "X402_FACILITATOR_CDP_API_KEY_SECRET", "secret")
    x402.setattr(settings, "X402_NETWORK", "base")
    x402.setattr(settings, "X402_FACILITATOR_URL", "")  # production: no default URL
    facilitator.validate_x402_config()
    config = facilitator.get_facilitator_client().config
    assert facilitator._host(config["url"]) == facilitator.CDP_HOST
    assert callable(config["create_headers"])


def test_real_cdp_sdk_headers_go_through_the_async_adapter(x402):
    # The x402 1.x client awaits create_headers; cdp-sdk's is synchronous and
    # signs a fresh JWT per call. Run the gateway's adapter against the real
    # package, offline, with a throwaway key made here (never a key literal).
    pytest.importorskip("cdp.x402")
    import base64

    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    key = ed25519.Ed25519PrivateKey.generate()
    raw = serialization.Encoding.Raw
    seed = key.private_bytes(raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    public = key.public_key().public_bytes(raw, serialization.PublicFormat.Raw)
    # cdp-sdk takes an Ed25519 secret as base64 of seed + public key (64 bytes).
    x402.setattr(settings, "X402_FACILITATOR_CDP_API_KEY_ID", "test-key-id")
    x402.setattr(settings, "X402_FACILITATOR_CDP_API_KEY_SECRET", base64.b64encode(seed + public).decode())
    x402.setattr(settings, "X402_NETWORK", "base")
    x402.setattr(settings, "X402_FACILITATOR_URL", "")
    facilitator.validate_x402_config()

    headers = asyncio.run(facilitator.get_facilitator_client().config["create_headers"]())
    for route in ("verify", "settle", "supported"):
        auth = headers[route]["Authorization"]
        assert auth.startswith("Bearer ")
        claims = jwt.decode(auth[len("Bearer "):], key.public_key(), algorithms=["EdDSA"],
                            options={"verify_aud": False})
        assert claims["sub"] == "test-key-id"


def test_real_cdp_sdk_route_matches_the_accepted_url():
    # The startup check accepts only CDP_FACILITATOR_URL; it must be the URL
    # the installed cdp-sdk returns, or an upgrade would drift silently (#527).
    cdp_x402 = pytest.importorskip("cdp.x402")
    assert cdp_x402.create_facilitator_config("id", "secret")["url"] == facilitator.CDP_FACILITATOR_URL


def test_real_cdp_sdk_tokens_are_signed_for_the_paths_the_client_calls(x402):
    # #527: the x402 1.x client posts to {url}/verify and {url}/settle. Each CDP
    # token names the one request it is valid for ("uris" claim). Offline, with
    # a throwaway key made here; nothing is sent.
    pytest.importorskip("cdp.x402")
    import base64
    from urllib.parse import urlparse

    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    key = ed25519.Ed25519PrivateKey.generate()
    raw = serialization.Encoding.Raw
    seed = key.private_bytes(raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    public = key.public_key().public_bytes(raw, serialization.PublicFormat.Raw)
    x402.setattr(settings, "X402_FACILITATOR_CDP_API_KEY_ID", "test-key-id")
    x402.setattr(settings, "X402_FACILITATOR_CDP_API_KEY_SECRET", base64.b64encode(seed + public).decode())
    x402.setattr(settings, "X402_NETWORK", "base")
    x402.setattr(settings, "X402_FACILITATOR_URL", facilitator.CDP_FACILITATOR_URL)
    facilitator.validate_x402_config()

    config = facilitator.get_facilitator_client().config
    headers = asyncio.run(config["create_headers"]())
    for route in ("verify", "settle"):
        called = urlparse(f"{config['url']}/{route}")
        token = headers[route]["Authorization"][len("Bearer "):]
        claims = jwt.decode(token, key.public_key(), algorithms=["EdDSA"], options={"verify_aud": False})
        assert claims["uris"] == [f"POST {called.hostname}{called.path}"]
