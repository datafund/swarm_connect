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
import time
from typing import List, Optional

from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import HTTPException, status

from app.core.config import settings

logger = logging.getLogger(__name__)

# Message prefixes. One per privilege — see the module docstring.
DEBUG_PREFIX = "swarm-connect-debug:"
POOL_CHECK_PREFIX = "swarm-connect-pool-check:"


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
