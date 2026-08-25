"""Balance metrics must carry the wallet address they describe.

Without it, an alert firing on these metrics cannot name the wallet to fund, so
the address was written into the Grafana alert text by hand. That literal named
one specific node — so it was already wrong for the other environment, and
became wrong for both when that node was replaced. The alert correctly reported
a low balance and then pointed at a decommissioned wallet, which is worse than
reporting nothing: an operator following it sends real funds to a dead address.
"""
import pytest
from unittest.mock import AsyncMock, patch

from prometheus_client import REGISTRY

from app.services import metrics as m


def _sample(metric_name: str, wallet: str):
    for metric in REGISTRY.collect():
        for s in metric.samples:
            if s.name == metric_name and s.labels.get("wallet") == wallet:
                return s.value
    return None


class TestWalletLabel:
    def test_bzz_gauge_declares_the_wallet_label(self):
        assert "wallet" in m.wallet_bzz_balance._labelnames

    def test_xdai_gauge_declares_the_wallet_label(self):
        assert "wallet" in m.wallet_xdai_balance._labelnames

    def test_balances_are_recorded_against_their_wallet(self):
        m.wallet_bzz_balance.labels(wallet="0xaaa").set(8.9)
        m.wallet_xdai_balance.labels(wallet="0xaaa").set(0.2)
        assert _sample("gateway_wallet_bzz_balance", "0xaaa") == 8.9
        assert _sample("gateway_wallet_xdai_balance", "0xaaa") == 0.2

    def test_two_wallets_do_not_overwrite_each_other(self):
        """The defect this guards against: one figure attributed to the wrong wallet.

        Two environments run two Bee nodes with two distinct wallets. Their
        balances must remain separately addressable.
        """
        m.wallet_bzz_balance.labels(wallet="0xmain").set(8.896554193715)
        m.wallet_bzz_balance.labels(wallet="0xdev").set(0.803536719872)

        assert _sample("gateway_wallet_bzz_balance", "0xmain") == 8.896554193715
        assert _sample("gateway_wallet_bzz_balance", "0xdev") == 0.803536719872

    def test_missing_address_is_labelled_rather_than_dropped(self):
        """A balance with no known wallet must still be reported, not lost."""
        m.wallet_bzz_balance.labels(wallet="unknown").set(1.0)
        assert _sample("gateway_wallet_bzz_balance", "unknown") == 1.0


class TestBaseEthCarriesItsWalletToo:
    """The Base wallet had the last remaining hardcoded address in an alert.

    It is currently correct, unlike the Bee wallet — but it is the same latent
    trap: a literal in alert text that nothing keeps in step with the running
    configuration. Labelling the metric is what lets the alert stop hardcoding it.
    """

    def test_gauge_declares_the_wallet_label(self):
        assert "wallet" in m.base_eth_balance._labelnames

    def test_balance_is_recorded_against_its_wallet(self):
        m.base_eth_balance.labels(wallet="0xbase").set(0.2)
        assert _sample("gateway_base_eth_balance", "0xbase") == 0.2

    def test_missing_address_is_labelled_rather_than_dropped(self):
        m.base_eth_balance.labels(wallet="unknown").set(0.1)
        assert _sample("gateway_base_eth_balance", "unknown") == 0.1
