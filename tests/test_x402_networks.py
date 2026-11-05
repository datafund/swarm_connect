"""One map between x402 v1 network names and CAIP-2 ids (#599, D6 in #596).

X402_NETWORK accepts both forms. Internally the tables are keyed by CAIP-2;
everything a v1 client or facilitator reads keeps the v1 name. So a gateway
configured with eip155:84532 must send exactly what one configured with
base-sepolia sends: compared here against the v1 goldens captured before the
map existed (tests/golden/x402_v1/).
"""
import json

import pytest

from app.core.config import is_testnet_network, settings
from app.x402 import facilitator
from app.x402.middleware import USDC_ADDRESSES, USDC_TOKEN_METADATA
from app.x402.networks import CAIP2_TO_V1, V1_TO_CAIP2, accepted_network_values, to_caip2, to_v1
from tests.test_x402_v1_golden import gateway, run_capture  # noqa: F401  (fixture)

PAIRS = [("base", "eip155:8453"), ("base-sepolia", "eip155:84532")]


def test_the_map():
    assert V1_TO_CAIP2 == {"base": "eip155:8453", "base-sepolia": "eip155:84532"}
    assert CAIP2_TO_V1 == {"eip155:8453": "base", "eip155:84532": "base-sepolia"}


@pytest.mark.parametrize("v1,caip2", PAIRS)
def test_both_forms_convert_both_ways(v1, caip2):
    assert to_caip2(v1) == caip2
    assert to_caip2(caip2) == caip2
    assert to_v1(caip2) == v1
    assert to_v1(v1) == v1


@pytest.mark.parametrize("unknown", ["", None, "Base", "base-mainnet", "polygon", "eip155:1", "EIP155:8453"])
def test_unknown_values_are_not_mapped(unknown):
    assert to_caip2(unknown) is None
    assert to_v1(unknown) == unknown      # never turned into another network


def test_asset_tables_are_keyed_by_caip2():
    assert set(USDC_ADDRESSES) == set(USDC_TOKEN_METADATA) == set(CAIP2_TO_V1)


def test_accepted_values_list_both_forms():
    assert accepted_network_values() == ["base", "base-sepolia", "eip155:8453", "eip155:84532"]


@pytest.mark.parametrize("v1,caip2", PAIRS)
def test_testnet_classification_agrees(v1, caip2):
    assert is_testnet_network(v1) == is_testnet_network(caip2)


@pytest.fixture
def x402(monkeypatch):
    monkeypatch.setattr(settings, "X402_ENABLED", True)
    monkeypatch.setattr(settings, "X402_PAY_TO_ADDRESS", "0x" + "22" * 20)
    monkeypatch.setattr(settings, "X402_FACILITATOR_CDP_API_KEY_ID", None)
    monkeypatch.setattr(settings, "X402_FACILITATOR_CDP_API_KEY_SECRET", None)
    monkeypatch.setattr(settings, "X402_FACILITATOR_BEARER_TOKEN", None)
    facilitator.reset_facilitator_client()
    yield monkeypatch
    facilitator.reset_facilitator_client()


@pytest.mark.parametrize("url", ["https://x402.org/facilitator", "https://facilitator.example.com"])
@pytest.mark.parametrize("v1,caip2", PAIRS)
def test_caip2_id_starts_exactly_like_its_v1_name(x402, v1, caip2, url):
    """Same outcome at startup (accepted, or refused with the same reason)."""
    x402.setattr(settings, "X402_FACILITATOR_URL", url)

    def outcome(network):
        x402.setattr(settings, "X402_NETWORK", network)
        try:
            facilitator.validate_x402_config()
            return None
        except RuntimeError as e:
            return str(e).replace(repr(network), "<network>")

    assert outcome(caip2) == outcome(v1)


@pytest.mark.parametrize("v1,caip2", PAIRS)
def test_caip2_configured_gateway_sends_the_v1_golden_bytes(gateway, monkeypatch, v1, caip2):  # noqa: F811
    got = run_capture(gateway, monkeypatch, caip2, v1)
    assert '"network":"%s"' % v1 in got["no_header_stamps"]["body"]
    assert caip2 not in "".join(str(v) for v in got.values())


def _audit_events():
    with open(settings.X402_AUDIT_LOG_PATH) as f:
        return [json.loads(line) for line in f if line.strip()]


def test_caip2_configured_gateway_shows_v1_names_outside_the_goldens(gateway, monkeypatch):  # noqa: F811
    """With X402_NETWORK=eip155:84532: /pricing, the free-tier 429 and the audit
    records all name the network base-sepolia."""
    from unittest.mock import AsyncMock, patch
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.endpoints import pricing
    from tests.test_x402_v1_golden import payment_header

    # Payment audit record (payment_settled) and the free-tier 429.
    client, _ = gateway("eip155:84532", free_tier=True)
    start = len(_audit_events())
    r = client.post("/api/v1/stamps/", headers={"X-PAYMENT": payment_header("base-sepolia", nonce_byte="0f")})
    assert r.status_code == 200, r.text
    settled = [e for e in _audit_events()[start:] if e["event_type"] == "payment_settled"]
    assert settled and {e["data"]["network"] for e in settled} == {"base-sepolia"}

    for _ in range(3):     # the free-tier limit
        assert client.post("/api/v1/stamps/", headers={"X-Payment-Mode": "free"}).status_code == 200
    r = client.post("/api/v1/stamps/", headers={"X-Payment-Mode": "free"})
    assert r.status_code == 429, r.text
    body = r.json()
    info = body.get("payment_info") or body["detail"]["payment_info"]
    assert info["network"] == "base-sepolia"

    # /pricing
    app = FastAPI()
    app.include_router(pricing.router, prefix="/api/v1")
    with patch("app.services.swarm_api.get_chainstate",
               new=AsyncMock(return_value={"currentPrice": "240000", "chainTip": 1, "block": 1})):
        body = TestClient(app).get("/api/v1/pricing").json()
    assert body["network"] == "base-sepolia"
    assert settings.X402_NETWORK == "eip155:84532"
