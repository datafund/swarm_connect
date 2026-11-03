# tests/test_balance_check_ok_metric.py
"""A failed balance read is visible next to the balance it left stale (#586).

The balance gauges keep their last value when a read fails, so a Bee or RPC
failure does not page as "wallet empty". On 2026-11-03 every wallet read
failed for over 30 minutes and the gauges kept showing the last good balance.
gateway_balance_check_ok and gateway_balance_last_success_timestamp_seconds
say whether the value is current. These tests drive update_balance_metrics()
with the real check functions and a patched Bee client.
"""
import os
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from prometheus_client import REGISTRY

from app.services import metrics

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

WALLET = {"bzzBalance": str(12 * 10**16), "nativeTokenBalance": str(5 * 10**17),
          "walletAddress": "0xWALLET586"}
CHEQUEBOOK = {"availableBalance": str(7 * 10**16), "totalBalance": str(8 * 10**16),
              "chequebookAddress": "0xCHEQUE586"}


def _ok(check):
    return REGISTRY.get_sample_value("gateway_balance_check_ok", {"check": check})


def _last(check):
    return REGISTRY.get_sample_value("gateway_balance_last_success_timestamp_seconds", {"check": check})


def _poll(status):
    return REGISTRY.get_sample_value("gateway_bee_poll_total", {"status": status}) or 0.0


def _settings(x402=False):
    s = MagicMock()
    s.X402_ENABLED = x402
    return s


async def _run(wallet, cheque, x402=False, base=None):
    with patch("app.services.metrics.settings", _settings(x402)), \
         patch("app.x402.preflight.get_wallet_info", new_callable=AsyncMock) as w, \
         patch("app.x402.preflight.get_chequebook_info", new_callable=AsyncMock) as c, \
         patch("app.x402.base_balance.check_base_eth_balance", new_callable=AsyncMock) as b:
        w.side_effect = wallet if isinstance(wallet, Exception) else None
        w.return_value = None if isinstance(wallet, Exception) else wallet
        c.side_effect = cheque if isinstance(cheque, Exception) else None
        c.return_value = None if isinstance(cheque, Exception) else cheque
        if isinstance(base, Exception):
            b.side_effect = base
        else:
            b.return_value = base
        await metrics.update_balance_metrics()


@pytest.mark.asyncio
async def test_successful_reads_set_ok_and_the_balances():
    await _run(WALLET, CHEQUEBOOK)
    for check in ("xbzz", "xdai", "chequebook"):
        assert _ok(check) == 1.0
        assert _last(check) > 0
    assert REGISTRY.get_sample_value("gateway_wallet_bzz_balance", {"wallet": "0xWALLET586"}) == 12.0
    assert REGISTRY.get_sample_value("gateway_wallet_xdai_balance", {"wallet": "0xWALLET586"}) == 0.5


@pytest.mark.asyncio
async def test_chequebook_gauge_is_set_from_a_successful_read():
    """It read a key the check never returned, so the gauge stayed at 0."""
    await _run(WALLET, CHEQUEBOOK)
    assert REGISTRY.get_sample_value("gateway_chequebook_available_balance") == 7.0
    assert _ok("chequebook") == 1.0


@pytest.mark.asyncio
async def test_failed_read_sets_ok_to_zero_and_keeps_the_last_balance():
    await _run(WALLET, CHEQUEBOOK)
    good_time = _last("xbzz")
    errors_before = _poll("error")

    await _run(httpx.PoolTimeout(""), httpx.PoolTimeout(""))

    for check in ("xbzz", "xdai", "chequebook"):
        assert _ok(check) == 0.0, check
    # The balances keep the last good value (no false "wallet empty")...
    assert REGISTRY.get_sample_value("gateway_wallet_bzz_balance", {"wallet": "0xWALLET586"}) == 12.0
    assert REGISTRY.get_sample_value("gateway_chequebook_available_balance") == 7.0
    # ...and the last success does not move, so its age grows.
    assert _last("xbzz") == good_time
    assert _poll("error") == errors_before + 1


@pytest.mark.asyncio
async def test_a_balance_read_below_threshold_is_a_successful_check():
    """ok=False from the check means "low" here, not "unread"."""
    low = dict(WALLET, bzzBalance="1", nativeTokenBalance="1")
    await _run(low, dict(CHEQUEBOOK, availableBalance="0"))
    for check in ("xbzz", "xdai", "chequebook"):
        assert _ok(check) == 1.0, check
    # A balance read as 0 is written, not skipped as before.
    assert REGISTRY.get_sample_value("gateway_chequebook_available_balance") == 0.0


@pytest.mark.asyncio
async def test_one_failed_read_does_not_skip_the_others():
    await _run(WALLET, httpx.ConnectError("refused"))
    assert _ok("xbzz") == 1.0
    assert _ok("xdai") == 1.0
    assert _ok("chequebook") == 0.0


@pytest.mark.asyncio
async def test_poll_counts_success_only_when_every_read_worked():
    before = _poll("success")
    await _run(WALLET, CHEQUEBOOK)
    assert _poll("success") == before + 1


@pytest.mark.asyncio
async def test_base_eth_check_is_recorded_when_x402_is_on():
    base_ok = {"ok": False, "fetched": True, "balance_eth": 0.0001, "address": "0xBASE586"}
    await _run(WALLET, CHEQUEBOOK, x402=True, base=base_ok)
    assert _ok("base_eth") == 1.0
    assert REGISTRY.get_sample_value("gateway_base_eth_balance", {"wallet": "0xBASE586"}) == 0.0001

    base_failed = {"ok": False, "fetched": False, "balance_eth": 0.0, "address": "0xBASE586"}
    await _run(WALLET, CHEQUEBOOK, x402=True, base=base_failed)
    assert _ok("base_eth") == 0.0
    assert REGISTRY.get_sample_value("gateway_base_eth_balance", {"wallet": "0xBASE586"}) == 0.0001


@pytest.mark.asyncio
async def test_base_balance_check_reports_fetched():
    from app.x402 import base_balance
    with patch.object(base_balance.settings, "X402_PAY_TO_ADDRESS", "0x" + "1" * 40), \
         patch.object(base_balance, "_get_cached_balance", return_value=None), \
         patch.object(base_balance, "_get_eth_balance_from_rpc", new_callable=AsyncMock) as rpc:
        rpc.return_value = 0
        assert (await base_balance.check_base_eth_balance())["fetched"] is True
        rpc.side_effect = httpx.ConnectError("refused")
        result = await base_balance.check_base_eth_balance()
        assert result["fetched"] is False


@pytest.mark.asyncio
async def test_gnosis_signer_check_follows_its_read():
    client = MagicMock()
    client.is_configured = True
    client.get_balances = AsyncMock(return_value={"xbzz_plur": 3 * 10**16, "xdai_wei": 10**18})
    s = MagicMock()
    s.X402_ENABLED = False
    s.STAMP_POOL_ENABLED = False
    s.CHUNK_UPLOAD_ENABLED = False
    s.STAMP_PURCHASE_FOR_OTHERS_ENABLED = True
    s.X402_BZZ_PRICE_FEED_URL = ""

    async def one_cycle():
        import asyncio
        with patch("app.services.metrics.settings", s), \
             patch("app.services.gnosis_chain.gnosis_chain_client", client), \
             patch("app.services.metrics.update_balance_metrics", new_callable=AsyncMock), \
             patch("app.services.metrics.update_node_stamp_metrics", new_callable=AsyncMock), \
             patch("app.services.swarm_api.get_all_stamps", new_callable=AsyncMock, return_value=[]), \
             patch("asyncio.sleep", side_effect=asyncio.CancelledError):
            try:
                await metrics._poll_balances()
            except asyncio.CancelledError:
                pass

    await one_cycle()
    assert _ok("gnosis_signer") == 1.0
    client.get_balances.side_effect = RuntimeError("rpc down")
    await one_cycle()
    assert _ok("gnosis_signer") == 0.0
    assert REGISTRY.get_sample_value("gateway_gnosis_signer_xbzz_balance") == 3.0


def _fresh_exposition(env):
    code = (
        "import app.services.metrics\n"
        "from prometheus_client import REGISTRY, generate_latest\n"
        "print(generate_latest(REGISTRY).decode())\n"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True,
                            text=True, timeout=120, env={**os.environ, **env})
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


def test_enabled_check_series_exist_at_zero_from_startup():
    lines = _fresh_exposition({"X402_ENABLED": "false", "STAMP_PURCHASE_FOR_OTHERS_ENABLED": "false"})
    for check in ("xbzz", "xdai", "chequebook"):
        assert f'gateway_balance_check_ok{{check="{check}"}} 0.0' in lines
        assert f'gateway_balance_last_success_timestamp_seconds{{check="{check}"}} 0.0' in lines
    # Disabled checks have no series, so they cannot alert.
    assert not any('check="base_eth"' in line for line in lines)
    assert not any('check="gnosis_signer"' in line for line in lines)


def test_optional_check_series_exist_when_enabled():
    lines = _fresh_exposition({"X402_ENABLED": "true", "STAMP_PURCHASE_FOR_OTHERS_ENABLED": "true"})
    for check in ("base_eth", "gnosis_signer"):
        assert f'gateway_balance_check_ok{{check="{check}"}} 0.0' in lines
