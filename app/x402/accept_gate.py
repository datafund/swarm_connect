# app/x402/accept_gate.py
"""
Refuse paid requests before payment when the gateway cannot accept them (#589).

The /health snapshot (#588) holds the pre-flight balance check. Its
`can_accept` is false when the Bee node's xBZZ or xDAI balance could not be
read, which in practice means the Bee node's wallet API is failing. A paid
request taken then fails after its payment settled, and the buyer ends up in
the refund or outcome-unknown paths. So while the snapshot says so, or is too
old to trust, a request that would pay (or be offered a 402) is answered
503 PAYMENTS_UNAVAILABLE before it is priced, verified or settled.

A retry of an attempt that may have been charged, whose existing answer comes
from stored state without a new settlement, is not refused here (see
is_retry_of_possibly_charged); it may still fail at pricing while Bee is down.

The check reads the snapshot in memory only: it adds no Bee call per request.
With no snapshot yet (startup) it lets requests through, as before.
"""
import logging
import math
from typing import Optional

from fastapi import HTTPException

from app.core.config import settings

logger = logging.getLogger(__name__)

_MESSAGES = {
    "balances_unreadable": (
        "The gateway cannot accept payments right now: it cannot read its Bee node's "
        "wallet balances. This request was not charged. Retry later."),
    "snapshot_too_old": (
        "The gateway cannot accept payments right now: it has not been able to check its "
        "Bee node recently. This request was not charged. Retry later."),
}


def payments_refusal() -> Optional[str]:
    """Why paid requests are refused now, or None when they are not.

    "balances_unreadable": the snapshot's pre-flight check could not read the
    xBZZ or xDAI balance. "snapshot_too_old": its refreshes are failing and it
    is too old to trust.
    """
    if not settings.X402_ENABLED:
        return None
    from app.services import health_snapshot
    snap = health_snapshot.peek(True)
    if snap is None:
        return None
    if health_snapshot.too_old(snap):
        return "snapshot_too_old"
    if snap.gnosis is not None and snap.gnosis.get("can_accept") is False:
        return "balances_unreadable"
    return None


def retry_after_seconds() -> int:
    """How long to wait before retrying: one snapshot TTL."""
    return max(1, math.ceil(settings.HEALTH_SNAPSHOT_TTL_SECONDS))


def is_retry_of_possibly_charged(request) -> bool:
    """The request retries an attempt that may have been charged, and its
    existing answer comes from stored state, without a new settlement.

    Such a request is not refused by the gate. It goes on to its existing
    answer, read from stored state: the stored result of its Idempotency-Key
    (a 201 replay, or a 409 naming the settlement), the 409 for an
    authorization whose settlement is in progress or whose work already ran
    with an unknown outcome, or the 402/409 of an authorization already in
    use. Refusing it with "not charged" would be wrong for a client whose
    first attempt was, and would delay its recovery.

    True when the request carries an X-PAYMENT whose authorization is:
    - in the outcome-unknown registry and in flight (409 in progress), or
      remembered as AFTER_WORK (409 outcome unknown, refused before any work);
    - reserved, unexpired, in the replay guard (409, or 402 "already used");
    or an Idempotency-Key with a stored entry for that authorization's payer
    on this route (a replay or a 409).
    Only X-PAYMENT is read: a PAYMENT-SIGNATURE (x402 v2) payment is not
    accepted yet, so no earlier attempt with one can have been charged (#600).

    Not exempt: a BEFORE_WORK authorization. Its retry is settled again by
    settle_payment, which could settle it for the first time while payments
    are refused, so the gate refuses it like a new payment. As a backstop,
    require_x402_payment refuses an exempted request that would still go on
    to settle (its state changed after this check), before the settlement.

    What is guaranteed is only that the gate does not refuse it. The request
    is still priced before it is verified, and pricing can call Bee: while
    Bee cannot price, such a retry can get 503 "Failed to calculate price"
    (or BEE_BUSY) instead of its stored result or 409. Retrying later, with
    the same Idempotency-Key and the same authorization, gets that answer
    once Bee prices again.

    Read-only lookups in memory: no Bee call, no write. The payer here is the
    one the authorization claims; the normal path verifies it before
    anything is replayed.
    """
    from app.x402.middleware import X_PAYMENT_HEADER, decode_payment_header
    header = request.headers.get(X_PAYMENT_HEADER)
    if not header:
        return False
    try:
        payload = decode_payment_header(header)
    except Exception:
        return False
    if payload is None:
        return False
    from app.x402.settlement import AFTER_WORK, authorization_key, outcome_unknown, replay_guard
    auth_key = authorization_key(payload)
    if auth_key is None:
        return False
    in_flight, kind = outcome_unknown.state(auth_key)
    if in_flight or kind == AFTER_WORK or replay_guard.is_reserved(auth_key):
        return True
    from app.x402.idempotency import IDEMPOTENCY_HEADER, MAX_KEY_LENGTH, entry_id, idempotency_store
    key = request.headers.get(IDEMPOTENCY_HEADER)
    if key is None or not (0 < len(key) <= MAX_KEY_LENGTH):
        return False
    eid = entry_id(auth_key[0], request.method, request.url.path, key)
    return idempotency_store.peek(eid) is not None


def refusal(reason: str) -> HTTPException:
    """503 PAYMENTS_UNAVAILABLE with Retry-After of one snapshot TTL."""
    retry_after = retry_after_seconds()
    return HTTPException(
        status_code=503,
        headers={"Retry-After": str(retry_after)},
        detail={
            # A literal at the raise site: tests/test_error_codes_doc.py scans for it.
            "code": "PAYMENTS_UNAVAILABLE",
            "message": _MESSAGES.get(reason, _MESSAGES["balances_unreadable"]),
            "reason": reason,
        },
    )
