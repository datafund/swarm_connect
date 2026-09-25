# app/services/gnosis_chain.py
"""
Gnosis chain client — the gateway's on-chain write capability for Flow B.

Signs and sends `approve` + `createBatch` on the Swarm PostageStamp contract so a
postage batch can be created with an ARBITRARY owner address (Bee's HTTP API always
makes the node the owner). The created `batchId` is returned so the owner can sign
its own stamps off-node.

Dependency note: uses web3.py, which is already present in the environment (pulled in
by the x402 SDK), so this adds no new heavy dependency and avoids the raw-RPC plumbing.
web3 is synchronous, so the blocking calls run in a thread (asyncio.to_thread) to keep
the event loop free.

Security: the signing key (GNOSIS_PRIVATE_KEY) is sensitive — it is never logged, and
the client's repr never exposes it.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import threading
import time
from typing import Any, Dict, Optional

from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address
from web3 import Web3
from web3.exceptions import TimeExhausted

from app.core.config import settings

logger = logging.getLogger(__name__)

BUCKET_DEPTH = 16  # fixed by the Swarm protocol
from app.services.swarm_api import PLUR_PER_BZZ  # noqa: F401

# Time bounds for one createBatch request (#368). The paid request must finish
# well inside the x402 authorization window (max_timeout_seconds=300 in
# app/x402/middleware.py) and Caddy's 300 s read_timeout: settled after the
# authorization expires, a spent batch would go unpaid and the client would
# never see its txHash.
#   - SIGNER_LOCK_TIMEOUT_SECONDS: how long a request queues for the signer
#     before it is refused as busy, with nothing sent.
#   - RECEIPT_TIMEOUT_SECONDS: total receipt waiting, approve + createBatch.
#   - MIN_CREATE_BATCH_WAIT_SECONDS: createBatch is not sent with less than
#     this left, since it would be reported pending for want of time.
# Worst case 30 + 120 s, plus the preflight reads.
SIGNER_LOCK_TIMEOUT_SECONDS = 30
RECEIPT_TIMEOUT_SECONDS = 120
MIN_CREATE_BATCH_WAIT_SECONDS = 30

# createBatch's gas estimate can be far too low. The batch is inserted into an
# ordered tree keyed on its normalised balance, which grows with the block
# number: estimated in the block of an earlier batch with the same amount, the
# key matches an existing node and the insert is cheap; executed in the next
# block, it creates a new node and rebalances. On the bee-factory chain about
# every second createBatch ran out of gas this way, using up to 2x its estimate
# (1.3x and 2x margins both still failed there). So the gas limit gets a floor
# well above the observed worst case (~570k) as well as a margin. Unused gas is
# not paid for; the limit only raises the balance the node checks for.
CREATE_BATCH_GAS_MARGIN = 1.3
CREATE_BATCH_GAS_FLOOR = 1_000_000
CREATE_BATCH_GAS_CAP = 3_000_000

# The standing allowance, in largest-possible batches. See _ensure_allowance.
APPROVE_BUFFER_BATCHES = 10

# Verified contract/token addresses per chain (ethersphere/go-storage-incentives-abi).
CHAIN_DEFAULTS = {
    100: {  # Gnosis mainnet
        "postage_stamp": "0x45a1502382541Cd610CC9068e88727426b696293",
        "bzz_token": "0xdBF3Ea6F5beE45c02255B2c26a16F300502F68da",
    },
    11155111: {  # Sepolia testnet
        "postage_stamp": "0xcdfdC3752caaA826fE62531E0000C40546eC56A6",
        "bzz_token": "0x543dDb01Ba47acB11de34891cD86B675F04840db",
    },
}

ERC20_ABI = [
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "account", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]},
]

POSTAGE_STAMP_ABI = [
    {"name": "createBatch", "type": "function", "stateMutability": "nonpayable",
     "inputs": [
         {"name": "_owner", "type": "address"},
         {"name": "_initialBalancePerChunk", "type": "uint256"},
         {"name": "_depth", "type": "uint8"},
         {"name": "_bucketDepth", "type": "uint8"},
         {"name": "_nonce", "type": "bytes32"},
         {"name": "_immutable", "type": "bool"},
     ],
     "outputs": [{"name": "", "type": "bytes32"}]},
]


class GnosisChainError(Exception):
    """Raised on chain-client configuration or transaction failures."""


class SignerBusy(GnosisChainError):
    """The signer cannot take a new transaction now; nothing was sent (#368).

    Either another request holds it past SIGNER_LOCK_TIMEOUT_SECONDS, or a
    transaction it sent earlier is still unconfirmed. A GnosisChainError,
    because nothing was spent and nothing may be charged.
    """


class TransactionPending(Exception):
    """createBatch was broadcast but no receipt arrived in time (#368).

    Deliberately NOT a GnosisChainError: that means the batch was not created,
    and this does not. The transaction may still mine and spend the BZZ, so a
    caller must treat the outcome as unknown, never as "not charged".
    """

    def __init__(self, tx_hash: str, batch_id: str, owner: str):
        super().__init__(f"transaction {tx_hash} not confirmed in time")
        self.tx_hash = tx_hash
        self.batch_id = batch_id
        self.owner = owner


def compute_batch_id(sender: str, nonce: bytes) -> str:
    """batchId = keccak256(abi.encode(msg.sender, nonce)) — derived from the CALLER."""
    encoded = abi_encode(["address", "bytes32"], [to_checksum_address(sender), nonce])
    return "0x" + keccak(encoded).hex()


class _NotConfirmed(Exception):
    """A sent transaction had no receipt within its share of RECEIPT_TIMEOUT_SECONDS."""

    def __init__(self, tx_hash: str):
        super().__init__(tx_hash)
        self.tx_hash = tx_hash


def _hex(tx_hash: Any) -> str:
    """Transaction hash as 0x-hex, whatever web3 handed back."""
    if isinstance(tx_hash, (bytes, bytearray)):
        return "0x" + bytes(tx_hash).hex()
    h = tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)
    return h if h.startswith("0x") else "0x" + h


class GnosisChainClient:
    """Minimal Gnosis client for ERC20 approve + PostageStamp.createBatch."""

    def __init__(self, rpc_url=None, private_key=None, chain_id=None,
                 postage_stamp=None, bzz_token=None):
        self._rpc_url = rpc_url if rpc_url is not None else settings.GNOSIS_RPC_URL
        self._private_key = private_key if private_key is not None else settings.GNOSIS_PRIVATE_KEY
        self._chain_id = int(chain_id if chain_id is not None else settings.GNOSIS_CHAIN_ID)
        defaults = CHAIN_DEFAULTS.get(self._chain_id, {})
        self._postage = (postage_stamp or settings.POSTAGE_STAMP_CONTRACT_ADDRESS
                         or defaults.get("postage_stamp"))
        self._bzz = (bzz_token or settings.BZZ_TOKEN_ADDRESS or defaults.get("bzz_token"))
        self._w3 = None
        self._acct = None
        self._bal_cache = None
        self._bal_ts = 0.0
        # One transaction sequence at a time per signer (#368). create_batch
        # runs in a worker thread (asyncio.to_thread), so this is a threading
        # lock: an asyncio.Lock would not be seen by the thread doing the work.
        # Without it, two requests read the same `pending` nonce, and one
        # replaces or fails the other.
        self._signer_lock = threading.Lock()

    def __repr__(self):  # never leak the key
        return f"<GnosisChainClient chain_id={self._chain_id} configured={self.is_configured}>"

    @property
    def is_configured(self) -> bool:
        return bool(self._rpc_url and self._private_key and self._postage and self._bzz)

    def _connect(self):
        if not self.is_configured:
            raise GnosisChainError("Gnosis chain client not configured (RPC/key/addresses missing)")
        if self._w3 is None:
            from eth_account import Account
            self._w3 = Web3(Web3.HTTPProvider(self._rpc_url))
            self._acct = Account.from_key(self._private_key)
        if not self._w3.is_connected():
            raise GnosisChainError(f"cannot connect to Gnosis RPC (chain_id {self._chain_id})")
        return self._w3, self._acct

    @property
    def address(self) -> str:
        _, acct = self._connect()
        return acct.address

    # --- transaction plumbing ---
    def _build_and_send(self, w3, acct, fn, timeout: float = RECEIPT_TIMEOUT_SECONDS,
                        gas_margin: float = 1.0, gas_floor: int = 0) -> Any:
        # Provide an explicit nonce (web3 build_transaction doesn't fill it reliably),
        # but let it set gas + EIP-1559 fees. Do NOT add gasPrice — mixing legacy and
        # EIP-1559 fee fields is rejected by the node.
        tx = fn.build_transaction({
            "from": acct.address,
            "nonce": w3.eth.get_transaction_count(acct.address, "pending"),
        })
        if gas_floor and "gas" in tx:
            wanted = max(int(tx["gas"] * gas_margin), gas_floor)
            tx["gas"] = max(int(tx["gas"]), min(wanted, CREATE_BATCH_GAS_CAP))
        return self._send(w3, acct, tx, timeout)

    def _send(self, w3, acct, tx, timeout: float = RECEIPT_TIMEOUT_SECONDS) -> Any:
        signed = acct.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
        txh = w3.eth.send_raw_transaction(raw)
        try:
            receipt = w3.eth.wait_for_transaction_receipt(txh, timeout=timeout)
        except TimeExhausted as e:
            raise _NotConfirmed(_hex(txh)) from e
        if receipt.status != 1:
            raise GnosisChainError(f"transaction reverted: {receipt.transactionHash.hex()}")
        return receipt

    def _create_batch_sync(self, owner, initial_balance_per_chunk, depth,
                           bucket_depth, immutable, nonce) -> Dict[str, str]:
        w3, acct = self._connect()
        owner = to_checksum_address(owner)
        bzz = w3.eth.contract(address=to_checksum_address(self._bzz), abi=ERC20_ABI)
        postage_addr = to_checksum_address(self._postage)
        postage = w3.eth.contract(address=postage_addr, abi=POSTAGE_STAMP_ABI)

        total_cost = int(initial_balance_per_chunk) * (2 ** int(depth))
        batch_id = compute_batch_id(acct.address, nonce)

        # Held from the nonce read to the createBatch receipt, so the approve and
        # createBatch of one request are never interleaved with another's. The
        # wait is bounded: a request that cannot have the signer soon is refused
        # with nothing sent, rather than queueing past its payment's validity.
        if not self._signer_lock.acquire(timeout=SIGNER_LOCK_TIMEOUT_SECONDS):
            raise SignerBusy("signer is busy with another request")
        try:
            # An earlier transaction still unconfirmed (a createBatch reported
            # pending) would sit ahead of anything sent now, so every later
            # request would time out behind it too, and each be answered 202
            # and charged for a batch that may never mine. Refuse instead, with
            # nothing sent, until it confirms or is replaced.
            latest = w3.eth.get_transaction_count(acct.address, "latest")
            pending = w3.eth.get_transaction_count(acct.address, "pending")
            if pending > latest:
                logger.error(f"for-owner signer has {pending - latest} unconfirmed transaction(s) "
                             f"from nonce {latest}; refusing new batches until they confirm")
                raise SignerBusy(f"signer has an unconfirmed transaction (nonce {latest})")

            deadline = time.monotonic() + RECEIPT_TIMEOUT_SECONDS
            try:
                self._ensure_allowance(w3, acct, bzz, postage_addr, total_cost, deadline)
            except _NotConfirmed as e:
                # approve moves no BZZ, and createBatch was never sent.
                raise GnosisChainError(f"approve {e.tx_hash} not confirmed in time") from e

            remaining = deadline - time.monotonic()
            if remaining < MIN_CREATE_BATCH_WAIT_SECONDS:
                raise GnosisChainError("approve took too long; createBatch not sent")

            fn = postage.functions.createBatch(
                owner, int(initial_balance_per_chunk), int(depth), int(bucket_depth), nonce, bool(immutable)
            )
            try:
                receipt = self._build_and_send(w3, acct, fn, timeout=remaining,
                                               gas_margin=CREATE_BATCH_GAS_MARGIN,
                                               gas_floor=CREATE_BATCH_GAS_FLOOR)
            except _NotConfirmed as e:
                # Broadcast, not confirmed. It may still mine, so this must not
                # look like a failure (#368).
                raise TransactionPending(e.tx_hash, batch_id, owner) from e
        finally:
            self._signer_lock.release()

        return {
            "batch_id": batch_id,
            "tx_hash": receipt.transactionHash.hex(),
            "owner": owner,
        }

    def _ensure_allowance(self, w3, acct, bzz, postage_addr, total_cost: int,
                          deadline: float) -> None:
        """Keep a standing allowance instead of approving each batch's exact cost.

        An exact-amount approve is overwritten by the next one, which is how one
        request's createBatch could revert when another's approve landed first.
        The lock and the unconfirmed-transaction check now keep requests apart,
        and a standing allowance keeps it that way without depending on them.
        It also saves an approve on most requests.

        The allowance is topped up to APPROVE_BUFFER_BATCHES of the largest
        batch the gateway may create whenever it falls below half of that.
        That bounds what the PostageStamp contract may draw to a fixed multiple
        of the per-batch cap rather than the whole wallet.
        """
        largest = max(total_cost, int(settings.STAMP_FOR_OTHERS_MAX_BZZ * PLUR_PER_BZZ))
        target = largest * APPROVE_BUFFER_BATCHES
        allowance = bzz.functions.allowance(acct.address, postage_addr).call()
        if allowance < target // 2:
            receipt = self._build_and_send(w3, acct, bzz.functions.approve(postage_addr, target),
                                           timeout=max(deadline - time.monotonic(), 1))
            logger.info(f"for-owner signer allowance topped up from {allowance} to {target} PLUR "
                        f"(tx {_hex(receipt.transactionHash)})")

    async def create_batch(self, owner: str, initial_balance_per_chunk: int, depth: int,
                           bucket_depth: int = BUCKET_DEPTH, immutable: bool = False,
                           nonce: Optional[bytes] = None) -> Dict[str, str]:
        """Create a postage batch owned by `owner`. Returns {batch_id, tx_hash, owner}."""
        nonce = nonce if nonce is not None else secrets.token_bytes(32)
        return await asyncio.to_thread(
            self._create_batch_sync, owner, initial_balance_per_chunk, depth,
            bucket_depth, immutable, nonce,
        )

    def _balance_sync(self) -> Dict[str, int]:
        w3, acct = self._connect()
        bzz = w3.eth.contract(address=to_checksum_address(self._bzz), abi=ERC20_ABI)
        return {
            "xbzz_plur": bzz.functions.balanceOf(acct.address).call(),
            "xdai_wei": w3.eth.get_balance(acct.address),
            "address": acct.address,
        }

    async def get_balances(self, use_cache: bool = True) -> Dict[str, int]:
        now = time.time()
        if use_cache and self._bal_cache is not None and (now - self._bal_ts) < 15:
            return self._bal_cache
        bals = await asyncio.to_thread(self._balance_sync)
        self._bal_cache, self._bal_ts = bals, now
        return bals

    async def preflight(self, required_plur: int = 0, use_cache: bool = False) -> Dict[str, Any]:
        """Check the signer wallet can fund a batch (gas + BZZ) before spending.

        is_critical (block) when xDAI is below the gas floor or xBZZ can't cover
        `required_plur`. Returns balances + warnings for /health and metrics.
        """
        bals = await self.get_balances(use_cache=use_cache)
        xbzz_plur, xdai_wei = bals["xbzz_plur"], bals["xdai_wei"]
        xdai = xdai_wei / 1e18
        xbzz = xbzz_plur / PLUR_PER_BZZ
        crit = settings.GNOSIS_XDAI_CRITICAL_THRESHOLD
        no_gas = xdai < crit
        insufficient_bzz = required_plur > 0 and xbzz_plur < required_plur
        warnings = []
        if no_gas:
            warnings.append(f"signer xDAI {xdai:.6f} below critical {crit} — cannot pay gas")
        elif xdai < settings.GNOSIS_XDAI_WARN_THRESHOLD:
            warnings.append(f"signer xDAI {xdai:.6f} low")
        if insufficient_bzz:
            warnings.append(f"signer xBZZ {xbzz:.6f} below batch cost {required_plur / PLUR_PER_BZZ:.6f}")
        elif xbzz < settings.GNOSIS_XBZZ_WARN_THRESHOLD:
            warnings.append(f"signer xBZZ {xbzz:.6f} low")
        return {
            "ok": not (no_gas or insufficient_bzz),
            "is_critical": no_gas or insufficient_bzz,
            "address": bals.get("address"),
            "xbzz_bzz": round(xbzz, 8),
            "xdai": round(xdai, 8),
            "warnings": warnings,
        }


# Global singleton (configured from settings).
gnosis_chain_client = GnosisChainClient()
