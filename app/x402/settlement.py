# app/x402/settlement.py
"""
x402 settlement: collect a verified payment before delivering what it pays for.

The dependency (dependency.py) only VERIFIES a payment: it checks the signed
authorization with the facilitator. Money moves when the facilitator SETTLES it.
Three things used to go wrong between the two (#355, #356, #357):

- the work was done first and settled afterwards, so a failed settlement left
  the gateway having bought a batch or granted credit for nothing;
- the settlement result was never checked. x402's FacilitatorClient.settle()
  returns SettleResponse(success=False, ...) rather than raising, so a failed
  settlement was reported, and delivered, as paid;
- nothing remembered which authorizations had been accepted, so concurrent
  requests carrying the same X-PAYMENT all verified and all did the work.

So: every paid handler calls settle_payment() immediately before its
irreversible step (a Bee purchase or top-up, an on-chain batch, a credit, a
handed-over pool batch, an upload). All validation and funds checks come
before that call, so a request that would be refused is refused unpaid. A
failed settlement stops the request with nothing delivered. The replay guard
reserves each authorization when it is verified, so a second request with the
same one is refused before it can do any work.
"""
import logging
import time
from threading import Lock
from typing import Dict, Optional, Tuple

from fastapi import HTTPException, Request

from app.core.config import settings
from app.core.client_ip import get_client_ip
from app.x402.audit import log_payment_failed, log_payment_settled

logger = logging.getLogger(__name__)

# How long a verified authorization stays reserved. Settlement happens within
# the request, so this only needs to outlive it; a later replay of an
# authorization that was settled fails at settlement anyway (the nonce is spent
# on-chain) and, since settlement precedes delivery, gets nothing.
REPLAY_WINDOW_SECONDS = 3600


class PaymentReplayGuard:
    """Remembers verified payment authorizations so each is used at most once."""

    def __init__(self):
        self._lock = Lock()
        self._reserved: Dict[Tuple[str, str], float] = {}

    def reserve(self, key: Tuple[str, str]) -> bool:
        """Reserve an authorization. False if it is already in use."""
        now = time.monotonic()
        with self._lock:
            expired = [k for k, until in self._reserved.items() if until <= now]
            for k in expired:
                del self._reserved[k]
            if key in self._reserved:
                return False
            self._reserved[key] = now + REPLAY_WINDOW_SECONDS
            return True

    def release(self, key: Optional[Tuple[str, str]]) -> None:
        """Release an authorization that was not settled, so it can be retried."""
        if key is None:
            return
        with self._lock:
            self._reserved.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._reserved.clear()


replay_guard = PaymentReplayGuard()


def authorization_key(payment_payload) -> Optional[Tuple[str, str]]:
    """(payer, nonce) of an EIP-3009 authorization, or None if the payload has none."""
    try:
        auth = payment_payload.payload.authorization
        return (str(auth.from_).lower(), str(auth.nonce).lower())
    except AttributeError:
        return None


def _facilitator():
    # Resolved through the dependency module so there is one client, and tests
    # that patch dependency._get_facilitator_client cover settlement too.
    from app.x402 import dependency
    return dependency._get_facilitator_client()


async def settle_payment(request: Request) -> None:
    """Settle the verified payment on this request, or stop the request.

    No-op unless the request was verified as paid, and idempotent: a request is
    settled at most once. Call it immediately before the irreversible step.
    """
    if getattr(request.state, "x402_mode", None) != "paid":
        return
    if getattr(request.state, "x402_settlement", None) is not None:
        return

    payment = getattr(request.state, "x402_payment", None)
    requirements = getattr(request.state, "x402_requirements", None)
    payer = getattr(request.state, "x402_payer", None)
    client_ip = get_client_ip(request)

    try:
        result = await _facilitator().settle(payment=payment, payment_requirements=requirements)
    except Exception as e:
        # The outcome is unknown: the facilitator may or may not have submitted
        # the transfer. Deliver nothing. The middleware then releases the
        # reservation, which is safe: a retry with the same authorization is
        # delivered only if its own settlement succeeds, and a transfer that did
        # go through spent the nonce, so it cannot.
        logger.error(f"x402: settlement error before delivery: {type(e).__name__}: {e}", exc_info=True)
        log_payment_failed(client_ip=client_ip, reason=f"{type(e).__name__}: {e}",
                           stage="settle", wallet_address=payer)
        raise HTTPException(
            status_code=502,
            detail={
                "code": "PAYMENT_SETTLEMENT_UNAVAILABLE",
                "message": (
                    "The payment could not be settled, so nothing was delivered. "
                    "If the transfer nevertheless appears on-chain, contact the operator "
                    "with this request's payment authorization for a refund."
                ),
                "x402_status": "settlement_failed",
            },
        )

    if not getattr(result, "success", False):
        reason = getattr(result, "error_reason", None) or "unknown"
        logger.warning(f"x402: settlement refused before delivery: {reason}")
        log_payment_settled(client_ip=client_ip, payer=payer, transaction_hash=None,
                            network=settings.X402_NETWORK, success=False, error_reason=reason)
        raise HTTPException(
            status_code=402,
            detail={
                "code": "PAYMENT_SETTLEMENT_FAILED",
                "message": f"The payment could not be settled ({reason}). Nothing was delivered.",
                "x402_status": "settlement_failed",
                "reason": reason,
            },
        )

    request.state.x402_settlement = result
    tx = getattr(result, "transaction", None)
    logger.info(f"x402: payment settled before delivery, tx={tx}")
    log_payment_settled(client_ip=client_ip, payer=payer, transaction_hash=tx,
                        network=settings.X402_NETWORK, success=True)
