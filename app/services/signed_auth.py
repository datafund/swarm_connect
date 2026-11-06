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
from typing import Dict, List, NamedTuple, Optional, Tuple

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
# A proof is spent only by an upload that Bee stored (#470). It goes through
# three steps:
#
# - reserve_owner_proof, when it grants access. A second request carrying the
#   same proof is refused while the first is in flight, so the proof still buys
#   at most one upload. Proofs from non-owners are never reserved, so they do
#   not fill the set.
# - confirm_owner_proof, as soon as Bee returns a reference. A failure after
#   that point (building the response, say) does not give the proof back,
#   because the upload it paid for has happened.
# - release_owner_proof, when the request fails before Bee could have stored
#   anything: a later validation refused it, the payment did not settle, Bee
#   could not be reached, or Bee answered 4xx. The owner can retry with the
#   same proof while it is fresh. A Bee 4xx (for example "batch overissued")
#   can follow a partial store; releasing then never yields a second
#   successful upload from one proof, but a retry may use some stamp capacity
#   again. When the outcome at Bee is unknown (timeout,
#   5xx, cancelled mid-upload) the proof stays spent: failing closed costs the
#   owner one signature, failing open could give a copied proof a second upload.
#
# The set lives in memory. That is sufficient because the gateway runs as a
# single process. Every entry, reserved or used, expires when the proof's
# timestamp window ends, after which the proof is refused as stale anyway, so a
# reservation held by a request that never finished cannot stay stuck. A
# restart empties the set, so proofs signed before the process started are
# refused outright; otherwise one seen just before a restart could be accepted
# again while still fresh. Running several workers would need a shared store.
#
# A proof is a bearer credential: it is not bound to the upload's content, so
# whoever presents it first gets that one upload. It must not be logged or shared.
# ---------------------------------------------------------------------------


class OwnerProofError(Exception):
    """An owner proof was presented and is not acceptable."""


class OwnerProof(NamedTuple):
    """A verified owner proof, not yet reserved (see reserve_owner_proof)."""
    signer: str
    key: str        # message|signer: what "used" is recorded against
    expires: float  # unix seconds after which the proof is stale anyway


class OwnerProofReservation:
    """One request's hold on a proof. Settled once: confirmed or released."""

    def __init__(self, key: str):
        self.key = key
        self.settled = False


_RESERVED = "reserved"
_USED = "used"

# message|signer -> (expiry in unix seconds, state, the reservation holding it)
_used_owner_proofs: Dict[str, Tuple[float, str, Optional[OwnerProofReservation]]] = {}
_used_owner_proofs_lock = threading.Lock()
_process_started_at = int(time.time())


def _refusal(state: str) -> OwnerProofError:
    if state == _RESERVED:
        return OwnerProofError("owner proof is in use by another request; wait for it or sign a new one")
    return OwnerProofError("owner proof has already been used; sign a new one")


def owner_proof_message(batch_id: str, timestamp: int) -> str:
    """The exact text an owner signs (EIP-191 personal_sign) to upload to batch_id."""
    return f"{OWNER_UPLOAD_PREFIX}{batch_id.lower()}:{timestamp}"


def verify_owner_proof(batch_id: str, timestamp: Optional[str], signature: Optional[str]) -> OwnerProof:
    """Verify a fresh, unused owner proof for batch_id, without marking it used.

    Only proves who is asking; whether that address owns the batch is for the
    ownership registry to decide. Call reserve_owner_proof once access is
    granted. Raises OwnerProofError.
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
    if ts < _process_started_at:
        raise OwnerProofError("proof was signed before the gateway restarted; sign a new one")

    message = owner_proof_message(batch_id, ts)
    try:
        signer = Account.recover_message(encode_defunct(text=message), signature=signature)
    except Exception:
        raise OwnerProofError("signature is not valid")

    proof = OwnerProof(signer, f"{message}|{signer.lower()}", ts + settings.DEBUG_SIG_MAX_AGE_SECONDS + 1)
    with _used_owner_proofs_lock:
        entry = _used_owner_proofs.get(proof.key)
        if entry is not None and entry[0] >= time.time():
            raise _refusal(entry[1])
    return proof


def reserve_owner_proof(proof: OwnerProof) -> OwnerProofReservation:
    """Hold a verified proof for one request. Raises OwnerProofError if it is
    already reserved or used.

    Check and mark happen under one lock, so two requests racing with the same
    proof cannot both get through. Settle the result with confirm_owner_proof
    or release_owner_proof.
    """
    now = time.time()
    with _used_owner_proofs_lock:
        for k in [k for k, (exp, _, _) in _used_owner_proofs.items() if exp < now]:
            del _used_owner_proofs[k]
        entry = _used_owner_proofs.get(proof.key)
        if entry is not None:
            raise _refusal(entry[1])
        reservation = OwnerProofReservation(proof.key)
        _used_owner_proofs[proof.key] = (proof.expires, _RESERVED, reservation)
        return reservation


def confirm_owner_proof(reservation: Optional[OwnerProofReservation]) -> None:
    """Mark a reserved proof used: the upload it granted has happened.

    No-op for None or a reservation that is already settled.
    """
    if reservation is None or reservation.settled:
        return
    with _used_owner_proofs_lock:
        reservation.settled = True
        entry = _used_owner_proofs.get(reservation.key)
        if entry is not None and entry[2] is reservation:
            _used_owner_proofs[reservation.key] = (entry[0], _USED, None)


def release_owner_proof(reservation: Optional[OwnerProofReservation]) -> None:
    """Give a reserved proof back: the upload certainly did not happen.

    No-op for None or a reservation that is already settled, so a confirmed
    proof is never released, and a release cannot drop a hold that another
    request took after this one was settled.
    """
    if reservation is None or reservation.settled:
        return
    with _used_owner_proofs_lock:
        reservation.settled = True
        entry = _used_owner_proofs.get(reservation.key)
        if entry is not None and entry[2] is reservation:
            del _used_owner_proofs[reservation.key]


def consume_owner_proof(proof: OwnerProof) -> None:
    """Reserve and confirm in one step. Raises OwnerProofError if already held."""
    confirm_owner_proof(reserve_owner_proof(proof))
