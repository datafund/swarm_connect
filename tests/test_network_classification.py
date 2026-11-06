"""Which x402 network identifiers count as test networks (#598).

Anything not recognised as a mainnet is a testnet. That covers unknown names,
unknown CAIP-2 ids and empty values: guessing wrong in that direction refuses
a real payment, guessing wrong the other way gives batches away.
"""
import pytest

from app.core.config import is_testnet_network


@pytest.mark.parametrize("network", [
    # v1 names
    "base", "base-mainnet", "ethereum", "mainnet", "polygon", "avalanche",
    # CAIP-2 ids for the same networks
    "eip155:8453", "eip155:1", "eip155:137", "eip155:43114",
    # whitespace, case and underscore variants
    " base ", "BASE", "Base_Mainnet", "EIP155:8453", " eip155:8453\n",
])
def test_mainnets_are_not_testnets(network):
    assert is_testnet_network(network) is False


@pytest.mark.parametrize("network", [
    "base-sepolia", "base_sepolia", "BASE-SEPOLIA", "sepolia",
    "eip155:84532", " EIP155:84532 ", "eip155:11155111",
    # unknown values stay testnets
    "eip155:999999", "eip155:0x2105", "eip155:", "not-a-network",
    "", "   ", None,
])
def test_everything_else_is_a_testnet(network):
    assert is_testnet_network(network) is True
