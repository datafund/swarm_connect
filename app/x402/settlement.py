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
import re
import time
from threading import Lock
from typing import Dict, Optional, Tuple

from fastapi import HTTPException, Request

from app.core.config import settings
from app.core.client_ip import get_client_ip
from app.x402.audit import log_payment_failed, log_payment_settled
from app.services.metrics import x402_settlements_total

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


# How long an authorization whose settlement outcome is unknown is remembered.
# Longer than the replay window: the question "was the first transfer
# collected?" outlives the reservation. Same span as an Idempotency-Key entry.
OUTCOME_UNKNOWN_SECONDS = 24 * 3600
OUTCOME_UNKNOWN_MAX_ENTRIES = 10000
# Which kind of unknown outcome. BEFORE_WORK: settle_payment, nothing ran; a
# retry with the same authorization is the documented recovery (it settles at
# most once). AFTER_WORK: the middleware fallback, the work already ran; a
# retry with the same authorization would run it again, so it is stopped.
BEFORE_WORK = "before_work"
AFTER_WORK = "after_work"


class OutcomeUnknownRegistry:
    """Authorizations sent for settlement that got no answer from the facilitator (#553).

    The transfer may have gone through. A later attempt with the same
    authorization that the facilitator refuses (at verify or at settle) is then
    probably refused BECAUSE the first one spent the nonce, so it must not be
    answered with a 402 and `accepts`: an x402 client pays again from that.
    In memory, like the replay guard: a gateway restart forgets it (a keyed
    request is still covered by its persisted Idempotency-Key entry).
    """

    def __init__(self):
        self._lock = Lock()
        # key -> (expiry, kind)
        self._entries: Dict[Tuple[str, str], Tuple[float, str]] = {}

    def add(self, key: Optional[Tuple[str, str]], kind: str) -> None:
        if key is None:
            return
        now = time.monotonic()
        with self._lock:
            for k in [k for k, (until, _) in self._entries.items() if until <= now]:
                del self._entries[k]
            previous = self._entries.get(key)
            if previous is not None and previous[1] == AFTER_WORK:
                kind = AFTER_WORK    # once the work has run, it must not run again
            self._entries[key] = (now + OUTCOME_UNKNOWN_SECONDS, kind)
            while len(self._entries) > OUTCOME_UNKNOWN_MAX_ENTRIES:
                del self._entries[min(self._entries, key=lambda k: self._entries[k][0])]

    def kind(self, key: Optional[Tuple[str, str]]) -> Optional[str]:
        """BEFORE_WORK or AFTER_WORK for a remembered authorization, else None."""
        if key is None:
            return None
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry[0] <= time.monotonic():
                del self._entries[key]
                entry = None
            return entry[1] if entry else None

    def contains(self, key: Optional[Tuple[str, str]]) -> bool:
        return self.kind(key) is not None

    def discard(self, key: Optional[Tuple[str, str]]) -> None:
        """This authorization has now settled: the earlier attempt did not spend it."""
        if key is None:
            return
        with self._lock:
            self._entries.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()


outcome_unknown = OutcomeUnknownRegistry()

OUTCOME_UNKNOWN_ADVICE = (
    "Do not pay again from this response. Check on-chain whether this authorization's nonce "
    "was used. If it was, contact the operator with the nonce. If it was not, the facilitator "
    "may still submit it, so before signing a new payment wait until its validBefore has "
    "passed, or cancel it on-chain with EIP-3009 cancelAuthorization."
)


OUTCOME_UNKNOWN_HEADERS = {"X-Payment-Mode": "failed", "X-Payment-Status": "settlement_unknown"}


def request_nonce(request) -> Optional[str]:
    """The canonical nonce of the request's payment authorization, for audit records."""
    key = getattr(getattr(request, "state", None), "x402_auth_key", None)
    return key[1] if key else None


def outcome_unknown_refusal(auth_key: Tuple[str, str], reason: Optional[str] = None) -> HTTPException:
    """The answer to a later attempt with an authorization whose settlement outcome is unknown.

    409, not 402, and no `accepts`. Whatever refused it, this request did
    nothing and was not charged; the earlier attempt may have been.
    """
    detail = {
        "code": "PAYMENT_AUTHORIZATION_OUTCOME_UNKNOWN",
        "message": ("An earlier request with this payment authorization was sent for settlement "
                    "and no answer came back, so that transfer may already have been collected. "
                    "This request did nothing and was not charged. " + OUTCOME_UNKNOWN_ADVICE),
        "nonce": auth_key[1],
        "x402_status": "settlement_unknown",
    }
    if reason:
        detail["reason"] = reason
    return HTTPException(status_code=409, detail=detail, headers=dict(OUTCOME_UNKNOWN_HEADERS))


_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_NONCE = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")


def authorization_key(payment_payload) -> Optional[Tuple[str, str]]:
    """(payer, nonce) of an EIP-3009 authorization, canonical, or None.

    Canonical so that every spelling of the same authorization is the same key:
    the replay guard and the Idempotency-Key store compare these as strings.
    The nonce is a bytes32 (64 hex digits, "0x" optional on input) and the
    payer a 20-byte address; anything else is refused rather than compared.
    """
    try:
        auth = payment_payload.payload.authorization
        payer, nonce = str(auth.from_), str(auth.nonce)
    except AttributeError:
        return None
    if not _ADDRESS.match(payer) or not _NONCE.match(nonce):
        return None
    return (payer.lower(), "0x" + nonce[-64:].lower())


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

    # For an Idempotency-Key: from here until the facilitator answers, the
    # outcome is unknown, and a retry must not be charged on the assumption
    # that nothing happened (idempotency.py).
    from app.x402.idempotency import record_settlement, record_settling, settlement_refused
    record_settling(request)
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
                           stage="settle", wallet_address=payer, nonce=request_nonce(request))
        x402_settlements_total.labels(result="error").inc()
        outcome_unknown.add(getattr(request.state, "x402_auth_key", None), BEFORE_WORK)
        raise HTTPException(
            status_code=502,
            detail={
                "code": "PAYMENT_SETTLEMENT_UNAVAILABLE",
                "message": (
                    "The payment could not be settled, so nothing was delivered, but it is "
                    "unknown whether the transfer was collected. Retry with the same X-PAYMENT "
                    "while it is valid: if the transfer was collected, the retry is not charged "
                    "again. " + OUTCOME_UNKNOWN_ADVICE
                ),
                "nonce": (getattr(request.state, "x402_auth_key", None) or (None, None))[1],
                "x402_status": "settlement_failed",
            },
        )

    if not getattr(result, "success", False):
        settlement_refused(request)
        reason = getattr(result, "error_reason", None) or "unknown"
        auth_key = getattr(request.state, "x402_auth_key", None)
        if outcome_unknown.contains(auth_key):
            # Probably refused because the earlier attempt spent the nonce, but
            # the reason does not say so reliably: no 402, no `accepts`.
            logger.warning(f"x402: settlement refused ({reason}) for an authorization whose "
                           f"earlier settlement outcome is unknown")
            log_payment_settled(client_ip=client_ip, payer=payer, transaction_hash=None,
                                network=settings.X402_NETWORK, success=False, error_reason=reason,
                                nonce=request_nonce(request))
            x402_settlements_total.labels(result="refused").inc()
            raise outcome_unknown_refusal(auth_key, reason)
        logger.warning(f"x402: settlement refused before delivery: {reason}")
        log_payment_settled(client_ip=client_ip, payer=payer, transaction_hash=None,
                            network=settings.X402_NETWORK, success=False, error_reason=reason,
                                nonce=request_nonce(request))
        x402_settlements_total.labels(result="refused").inc()
        message = f"The payment could not be settled ({reason}). Nothing was delivered."
        detail = {
            "code": "PAYMENT_SETTLEMENT_FAILED",
            "message": message,
            "x402_status": "settlement_failed",
            "reason": reason,
        }
        # Also a well-formed x402 Payment Required body, so a client can sign a
        # fresh payment from it like from any other 402 (#372).
        if hasattr(requirements, "model_dump"):
            detail.update({
                "x402Version": 1,
                "error": message,
                "accepts": [requirements.model_dump(by_alias=True)],
            })
        raise HTTPException(status_code=402, detail=detail)

    request.state.x402_settlement = result
    outcome_unknown.discard(getattr(request.state, "x402_auth_key", None))
    # From here on a retry with the same Idempotency-Key must not pay again,
    # whatever happens to this request (#359).
    record_settlement(request, result)
    tx = getattr(result, "transaction", None)
    logger.info(f"x402: payment settled before delivery, tx={tx}")
    log_payment_settled(client_ip=client_ip, payer=payer, transaction_hash=tx,
                        network=settings.X402_NETWORK, success=True, nonce=request_nonce(request))
    x402_settlements_total.labels(result="settled").inc()
