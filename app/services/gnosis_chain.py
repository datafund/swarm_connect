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
import time
from typing import Any, Dict, Optional

from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address
from web3 import Web3

from app.core.config import settings

logger = logging.getLogger(__name__)

BUCKET_DEPTH = 16  # fixed by the Swarm protocol
PLUR_PER_BZZ = 10 ** 16  # 1 BZZ = 10^16 PLUR

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


def compute_batch_id(sender: str, nonce: bytes) -> str:
    """batchId = keccak256(abi.encode(msg.sender, nonce)) — derived from the CALLER."""
    encoded = abi_encode(["address", "bytes32"], [to_checksum_address(sender), nonce])
    return "0x" + keccak(encoded).hex()


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
    def _build_and_send(self, w3, acct, fn) -> Any:
        # Provide an explicit nonce (web3 build_transaction doesn't fill it reliably),
        # but let it set gas + EIP-1559 fees. Do NOT add gasPrice — mixing legacy and
        # EIP-1559 fee fields is rejected by the node.
        tx = fn.build_transaction({
            "from": acct.address,
            "nonce": w3.eth.get_transaction_count(acct.address, "pending"),
        })
        return self._send(w3, acct, tx)

    def _send(self, w3, acct, tx) -> Any:
        signed = acct.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
        txh = w3.eth.send_raw_transaction(raw)
        receipt = w3.eth.wait_for_transaction_receipt(txh, timeout=180)
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

        # ensure allowance (skip if already sufficient)
        allowance = bzz.functions.allowance(acct.address, postage_addr).call()
        if allowance < total_cost:
            self._build_and_send(w3, acct, bzz.functions.approve(postage_addr, total_cost))

        receipt = self._build_and_send(w3, acct, postage.functions.createBatch(
            owner, int(initial_balance_per_chunk), int(depth), int(bucket_depth), nonce, bool(immutable)
        ))

        return {
            "batch_id": compute_batch_id(acct.address, nonce),
            "tx_hash": receipt.transactionHash.hex(),
            "owner": owner,
        }

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
