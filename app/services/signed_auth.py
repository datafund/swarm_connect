# app/services/signed_auth.py
"""Signature-gated authorisation for operator-only endpoints.

The caller proves control of an allow-listed Ethereum address by signing a
short, timestamped message (EIP-191 personal_sign). Nothing secret is stored on
the gateway: the allow-list holds public addresses, and the signature is
verified by recovering the signer.

    Headers: X-Debug-Timestamp: <unix seconds>
             X-Debug-Signature: 0x<65-byte sig>

Every protected operation signs a DIFFERENT message prefix, and that is the
point. Reading Bee's diagnostics and spending the gateway's money are not the
same privilege, so a signature captured from one must not authorise the other.
Sharing the prefix would have made every debug signature a spending signature
for as long as its timestamp stayed fresh.

An empty allow-list disables the operation entirely — the route answers 404
rather than 401, so an unconfigured gateway does not advertise that a
privileged endpoint exists. This is also why the safe default is empty: a
deployment that has not thought about who may spend its money cannot be talked
into spending it.
"""
import logging
import threading
import time
from typing import Dict, List, Optional

from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import HTTPException, status

from app.core.config import settings

logger = logging.getLogger(__name__)

# Message prefixes. One per privilege — see the module docstring.
DEBUG_PREFIX = "swarm-connect-debug:"
POOL_CHECK_PREFIX = "swarm-connect-pool-check:"
OWNER_UPLOAD_PREFIX = "swarm-connect-owner-upload:"


def authorize_signed_request(
    prefix: str,
    allowed: List[str],
    timestamp: Optional[str],
    signature: Optional[str],
    operation: str = "operation",
) -> str:
    """Verify a request is signed by an allow-listed address over a fresh timestamp.

    Returns the recovered address, or raises HTTPException.
    """
    if not allowed:
        # Hidden when not configured, rather than announcing a locked door.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    if not timestamp or not signature:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Debug-Timestamp / X-Debug-Signature",
        )

    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid timestamp")

    # Replay guard. Also rejects timestamps in the future, so a signature cannot
    # be minted now and held until it becomes convenient.
    if abs(int(time.time()) - ts) > settings.DEBUG_SIG_MAX_AGE_SECONDS:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Stale or future timestamp")

    message = encode_defunct(text=f"{prefix}{ts}")
    try:
        signer = Account.recover_message(message, signature=signature)
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")

    if signer.lower() not in allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Address not allow-listed")

    logger.info("Authorized %s for %s", signer, operation)
    return signer


# ---------------------------------------------------------------------------
# Owner proof for uploads to an owned batch (#384)
#
# An owned batch used to be writable only on a paid request, because the x402
# payer was the only identity the gateway knew. The owner therefore paid again
# on every upload just to say who they were. A signature proves the same thing
# for free, so the free-tier upload path accepts one instead.
#
# The message names the batch, so a proof for one batch is not a proof for
# another, and uses its own prefix, so no debug or pool-maintenance signature
# can be presented as one. Each proof is accepted ONCE: operator signatures only
# expire, but an owner proof travels with every upload, and a copy of one
# should not let anybody else write to the owner's batch until it goes stale.
# Keyed on the message and the signer rather than the signature bytes, which
# can be re-encoded into a second valid signature for the same message.
#
# The used set lives in memory. That is sufficient because the gateway runs as a
# single process, and a restart forgets nothing that is still fresh for longer
# than the freshness window. Running several workers would need a shared store.
# ---------------------------------------------------------------------------


class OwnerProofError(Exception):
    """An owner proof was presented and is not acceptable."""


_used_owner_proofs: Dict[str, float] = {}  # message|signer -> expiry (unix seconds)
_used_owner_proofs_lock = threading.Lock()


def owner_proof_message(batch_id: str, timestamp: int) -> str:
    """The exact text an owner signs (EIP-191 personal_sign) to upload to batch_id."""
    return f"{OWNER_UPLOAD_PREFIX}{batch_id.lower()}:{timestamp}"


def verify_owner_proof(batch_id: str, timestamp: Optional[str], signature: Optional[str]) -> str:
    """Return the address that signed a fresh, unused owner proof for batch_id.

    Only proves who is asking; whether that address owns the batch is for the
    ownership registry to decide. Raises OwnerProofError.
    """
    if not timestamp or not signature:
        raise OwnerProofError("both X-Owner-Timestamp and X-Owner-Signature are required")
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        raise OwnerProofError("timestamp is not a unix time in seconds")
    # Same freshness window, and the same refusal of future timestamps, as the
    # operator signatures above.
    if abs(int(time.time()) - ts) > settings.DEBUG_SIG_MAX_AGE_SECONDS:
        raise OwnerProofError("timestamp is stale or in the future")

    message = owner_proof_message(batch_id, ts)
    try:
        signer = Account.recover_message(encode_defunct(text=message), signature=signature)
    except Exception:
        raise OwnerProofError("signature is not valid")

    key = f"{message}|{signer.lower()}"
    now = time.time()
    with _used_owner_proofs_lock:
        for k in [k for k, exp in _used_owner_proofs.items() if exp < now]:
            del _used_owner_proofs[k]
        if key in _used_owner_proofs:
            raise OwnerProofError("owner proof has already been used; sign a new one")
        _used_owner_proofs[key] = ts + settings.DEBUG_SIG_MAX_AGE_SECONDS + 1

    return signer
