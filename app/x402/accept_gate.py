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
        "wallet balances. Nothing was charged. Retry later."),
    "snapshot_too_old": (
        "The gateway cannot accept payments right now: it has not been able to check its "
        "Bee node recently. Nothing was charged. Retry later."),
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


def refusal(reason: str) -> HTTPException:
    """503 PAYMENTS_UNAVAILABLE with Retry-After of one snapshot TTL."""
    retry_after = max(1, math.ceil(settings.HEALTH_SNAPSHOT_TTL_SECONDS))
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
