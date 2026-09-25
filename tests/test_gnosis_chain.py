# tests/test_gnosis_chain.py
"""
Tests for the Gnosis chain client (Flow B #227).

The on-chain orchestration is tested by patching _connect() to return a fake
web3 + account, so no RPC or web3 internals are needed.
"""
import asyncio
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from eth_abi import encode as abi_encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address

from web3.exceptions import TimeExhausted

from app.services.gnosis_chain import (
    APPROVE_BUFFER_BATCHES,
    CHAIN_DEFAULTS,
    GnosisChainClient,
    GnosisChainError,
    TransactionPending,
    compute_batch_id,
)
from app.services.swarm_api import PLUR_PER_BZZ

KEY = "0x" + "11" * 32
ADDR = Account.from_key(KEY).address
OWNER = Account.from_key("0x" + "22" * 32).address
NONCE = b"\x33" * 32


# --------------------------------------------------------------------------- #
# pure helpers / config
# --------------------------------------------------------------------------- #
def test_compute_batch_id_matches_abi_encode():
    expected = "0x" + keccak(abi_encode(["address", "bytes32"], [ADDR, NONCE])).hex()
    assert compute_batch_id(ADDR, NONCE) == expected
    assert len(compute_batch_id(ADDR, NONCE)) == 66


def test_chain_defaults_select_addresses_by_chain_id():
    mainnet = GnosisChainClient(rpc_url="x", private_key=KEY, chain_id=100)
    assert mainnet._postage.lower() == CHAIN_DEFAULTS[100]["postage_stamp"].lower()
    assert mainnet._bzz.lower() == CHAIN_DEFAULTS[100]["bzz_token"].lower()
    testnet = GnosisChainClient(rpc_url="x", private_key=KEY, chain_id=11155111)
    assert testnet._postage.lower() == CHAIN_DEFAULTS[11155111]["postage_stamp"].lower()


def test_explicit_addresses_override_defaults():
    c = GnosisChainClient(rpc_url="x", private_key=KEY, chain_id=100,
                          postage_stamp="0x" + "ab" * 20, bzz_token="0x" + "cd" * 20)
    assert c._postage == "0x" + "ab" * 20


def test_is_configured():
    assert GnosisChainClient(rpc_url="x", private_key=KEY, chain_id=100).is_configured is True
    assert GnosisChainClient(rpc_url=None, private_key=None, chain_id=100).is_configured is False


def test_repr_never_leaks_key():
    c = GnosisChainClient(rpc_url="x", private_key=KEY, chain_id=100)
    assert KEY not in repr(c)
    assert "11" * 32 not in repr(c)


# --------------------------------------------------------------------------- #
# create_batch orchestration (fake web3 via patched _connect)
# --------------------------------------------------------------------------- #
def _fakes(allowance: int, receipt_status: int = 1):
    acct = MagicMock()
    acct.address = ADDR
    acct.sign_transaction.return_value = MagicMock(raw_transaction=b"raw")

    contract = MagicMock()
    contract.functions.allowance.return_value.call.return_value = allowance
    contract.functions.balanceOf.return_value.call.return_value = 10**18
    contract.functions.approve.return_value.build_transaction.return_value = {"from": ADDR}
    contract.functions.createBatch.return_value.build_transaction.return_value = {"from": ADDR}

    receipt = MagicMock()
    receipt.status = receipt_status
    receipt.transactionHash.hex.return_value = "0xdeadbeef"

    w3 = MagicMock()
    w3.is_connected.return_value = True
    w3.eth.contract.return_value = contract
    w3.eth.get_transaction_count.return_value = 0
    w3.eth.gas_price = 1_000_000_000
    w3.eth.estimate_gas.return_value = 200_000
    w3.eth.send_raw_transaction.return_value = b"txh"
    w3.eth.wait_for_transaction_receipt.return_value = receipt
    return w3, acct, contract


def test_create_batch_approves_when_allowance_insufficient():
    c = GnosisChainClient(rpc_url="x", private_key=KEY, chain_id=100)
    w3, acct, contract = _fakes(allowance=0)  # below total cost -> approve needed
    with patch.object(GnosisChainClient, "_connect", return_value=(w3, acct)):
        res = c._create_batch_sync(OWNER, 1000, 17, 16, False, NONCE)
    assert res["batch_id"] == compute_batch_id(ADDR, NONCE)
    assert res["tx_hash"] == "0xdeadbeef"
    assert res["owner"] == to_checksum_address(OWNER)
    contract.functions.approve.assert_called_once()
    contract.functions.createBatch.assert_called_once()


def test_create_batch_skips_approve_when_allowance_sufficient():
    c = GnosisChainClient(rpc_url="x", private_key=KEY, chain_id=100)
    w3, acct, contract = _fakes(allowance=10**30)  # standing allowance well above the buffer
    with patch.object(GnosisChainClient, "_connect", return_value=(w3, acct)):
        c._create_batch_sync(OWNER, 1000, 17, 16, False, NONCE)
    contract.functions.approve.assert_not_called()
    contract.functions.createBatch.assert_called_once()


def test_create_batch_raises_on_revert():
    c = GnosisChainClient(rpc_url="x", private_key=KEY, chain_id=100)
    w3, acct, _ = _fakes(allowance=10**30, receipt_status=0)  # reverted createBatch
    with patch.object(GnosisChainClient, "_connect", return_value=(w3, acct)):
        with pytest.raises(GnosisChainError):
            c._create_batch_sync(OWNER, 1000, 17, 16, False, NONCE)


@pytest.mark.asyncio
async def test_create_batch_async_wraps_sync():
    c = GnosisChainClient(rpc_url="x", private_key=KEY, chain_id=100)
    w3, acct, _ = _fakes(allowance=0)
    with patch.object(GnosisChainClient, "_connect", return_value=(w3, acct)):
        res = await c.create_batch(OWNER, 1000, 17, nonce=NONCE)
    assert res["batch_id"] == compute_batch_id(ADDR, NONCE)


# --------------------------------------------------------------------------- #
# signer preflight (#231)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_preflight_ok_when_funded():
    c = GnosisChainClient(rpc_url="x", private_key=KEY, chain_id=100)
    c.get_balances = AsyncMock(return_value={
        "xbzz_plur": 10 * 10**16, "xdai_wei": 10**18, "address": ADDR})  # 10 BZZ, 1 xDAI
    pf = await c.preflight(required_plur=2 * 10**16)
    assert pf["ok"] and not pf["is_critical"]
    assert pf["warnings"] == []


@pytest.mark.asyncio
async def test_preflight_critical_no_gas():
    c = GnosisChainClient(rpc_url="x", private_key=KEY, chain_id=100)
    c.get_balances = AsyncMock(return_value={
        "xbzz_plur": 10 * 10**16, "xdai_wei": 0, "address": ADDR})  # no xDAI -> no gas
    pf = await c.preflight(required_plur=10**16)
    assert pf["is_critical"] and not pf["ok"]
    assert any("xDAI" in w for w in pf["warnings"])


@pytest.mark.asyncio
async def test_preflight_critical_insufficient_bzz():
    c = GnosisChainClient(rpc_url="x", private_key=KEY, chain_id=100)
    c.get_balances = AsyncMock(return_value={
        "xbzz_plur": 10**15, "xdai_wei": 10**18, "address": ADDR})  # 0.1 BZZ < cost
    pf = await c.preflight(required_plur=10**16)  # needs 1 BZZ
    assert pf["is_critical"]
    assert any("xBZZ" in w for w in pf["warnings"])


# --------------------------------------------------------------------------- #
# signer serialisation, standing allowance, receipt timeout (#368)
# --------------------------------------------------------------------------- #
def _buffer_target():
    from app.core.config import settings
    return int(settings.STAMP_FOR_OTHERS_MAX_BZZ * PLUR_PER_BZZ) * APPROVE_BUFFER_BATCHES


def test_approve_sets_a_buffered_allowance_not_the_exact_cost():
    c = GnosisChainClient(rpc_url="x", private_key=KEY, chain_id=100)
    w3, acct, contract = _fakes(allowance=0)
    with patch.object(GnosisChainClient, "_connect", return_value=(w3, acct)):
        c._create_batch_sync(OWNER, 1000, 17, 16, False, NONCE)
    approved = contract.functions.approve.call_args.args[1]
    assert approved == _buffer_target()
    assert approved > 1000 * 2 ** 17


def test_allowance_topped_up_before_it_runs_low():
    """Enough for this batch is not enough: a timed-out batch may still draw on it."""
    c = GnosisChainClient(rpc_url="x", private_key=KEY, chain_id=100)
    w3, acct, contract = _fakes(allowance=_buffer_target() // 2 - 1)
    with patch.object(GnosisChainClient, "_connect", return_value=(w3, acct)):
        c._create_batch_sync(OWNER, 1000, 17, 16, False, NONCE)
    contract.functions.approve.assert_called_once()


class _Chain:
    """Fake web3 that hands out the `pending` nonce like a node and records order.

    Steps are tagged with the worker thread, which is one per request here.
    """

    def __init__(self, allowance=0):
        self.sent = []          # nonces of broadcast transactions, in order
        self.events = []        # (request tag, step)
        self.lock = threading.Lock()
        self.allowance = allowance
        self.timeout_create = False
        self.local = threading.local()  # kind of the last transaction this thread sent

        self.acct = MagicMock()
        self.acct.address = ADDR
        self.acct.sign_transaction.side_effect = lambda tx: MagicMock(raw_transaction=tx)

        contract = MagicMock()
        contract.functions.allowance.return_value.call.side_effect = lambda: self.allowance
        contract.functions.approve.side_effect = lambda spender, amount: self._fn("approve")
        contract.functions.createBatch.side_effect = lambda *a: self._fn("createBatch")
        self.contract = contract

        w3 = MagicMock()
        w3.is_connected.return_value = True
        w3.eth.contract.return_value = contract
        w3.eth.get_transaction_count.side_effect = lambda addr, block: self._nonce()
        w3.eth.send_raw_transaction.side_effect = self._send
        w3.eth.wait_for_transaction_receipt.side_effect = self._receipt
        self.w3 = w3

    def _fn(self, kind):
        fn = MagicMock()
        fn.build_transaction.side_effect = lambda params: {"kind": kind, **params}
        return fn

    def _nonce(self):
        with self.lock:
            n = len(self.sent)
        time.sleep(0.02)  # widen the read-then-send window a race would need
        return n

    def _send(self, tx):
        with self.lock:
            self.sent.append(tx["nonce"])
            self.events.append((threading.get_ident(), tx["kind"]))
            if tx["kind"] == "approve":
                self.allowance = 10**30
        self.local.kind = tx["kind"]
        return b"\x12" * 32

    def _receipt(self, txh, timeout):
        time.sleep(0.02)
        if self.timeout_create and self.local.kind == "createBatch":
            raise TimeExhausted("not in chain")
        r = MagicMock()
        r.status = 1
        r.transactionHash.hex.return_value = "0x" + "12" * 32
        return r


@pytest.mark.asyncio
async def test_concurrent_create_batch_is_serialised():
    """Two requests at once: distinct nonces, and no interleaving of their steps.

    Without the lock both read nonce 0 (one transaction replaces the other), and
    request B's approve can land between A's approve and A's createBatch.
    """
    chain = _Chain(allowance=0)
    c = GnosisChainClient(rpc_url="x", private_key=KEY, chain_id=100)
    with patch.object(GnosisChainClient, "_connect", return_value=(chain.w3, chain.acct)):
        await asyncio.gather(*[
            c.create_batch(OWNER, 1000, 17, nonce=bytes([i]) * 32) for i in range(3)
        ])

    assert sorted(chain.sent) == list(range(len(chain.sent))), f"nonce reused: {chain.sent}"
    tags = [t for t, _ in chain.events]
    # Each request's transactions are contiguous: A A B C, never A B A.
    runs = [t for i, t in enumerate(tags) if i == 0 or tags[i - 1] != t]
    assert len(runs) == len(set(runs)) == 3, chain.events
    # One approve for the lot: the buffered allowance covers the later requests.
    assert [k for _, k in chain.events].count("approve") == 1


def test_receipt_timeout_on_create_batch_reports_pending_with_tx_hash():
    chain = _Chain(allowance=10**30)
    chain.timeout_create = True
    c = GnosisChainClient(rpc_url="x", private_key=KEY, chain_id=100)
    with patch.object(GnosisChainClient, "_connect", return_value=(chain.w3, chain.acct)):
        with pytest.raises(TransactionPending) as ei:
            c._create_batch_sync(OWNER, 1000, 17, 16, False, NONCE)
        # Not a GnosisChainError: that would mean "not created", and it may be.
        assert not isinstance(ei.value, GnosisChainError)
        assert ei.value.tx_hash == "0x" + "12" * 32
        assert ei.value.batch_id == compute_batch_id(ADDR, NONCE)
        # The signer is free again for the next request.
        chain.timeout_create = False
        c._create_batch_sync(OWNER, 1000, 17, 16, False, b"\x44" * 32)


def test_receipt_timeout_on_approve_is_a_plain_failure():
    """approve moves no BZZ and createBatch was never sent, so nothing is pending."""
    c = GnosisChainClient(rpc_url="x", private_key=KEY, chain_id=100)
    w3, acct, contract = _fakes(allowance=0)
    w3.eth.wait_for_transaction_receipt.side_effect = TimeExhausted("slow")
    with patch.object(GnosisChainClient, "_connect", return_value=(w3, acct)):
        with pytest.raises(GnosisChainError, match="approve"):
            c._create_batch_sync(OWNER, 1000, 17, 16, False, NONCE)
    contract.functions.createBatch.assert_not_called()
