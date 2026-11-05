"""One map between x402 v1 network names and CAIP-2 ids (#599, D6 in #596).

X402_NETWORK accepts both forms. Internally the tables are keyed by CAIP-2;
everything a v1 client or facilitator reads keeps the v1 name. So a gateway
configured with eip155:84532 must send exactly what one configured with
base-sepolia sends: compared here against the v1 goldens captured before the
map existed (tests/golden/x402_v1/).
"""
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
