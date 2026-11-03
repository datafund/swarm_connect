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
import asyncio
import hashlib
import json
import logging
import os
import re
import time
from threading import Lock, RLock
from typing import Dict, Optional, Tuple

from fastapi import HTTPException, Request

from app.core.atomic_io import StateLoadError, atomic_write_json, load_json_state, unreadable_state
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
# While the state file cannot be written, retry at most this often.
WRITE_RETRY_SECONDS = 5.0
# Which kind of unknown outcome. BEFORE_WORK: settle_payment, nothing ran; a
# retry with the same authorization is the documented recovery (it settles at
# most once). AFTER_WORK: the middleware fallback, the work already ran; a
# retry with the same authorization would run it again, so it is stopped.
BEFORE_WORK = "before_work"
AFTER_WORK = "after_work"


def _key_id(key: Tuple[str, str]) -> str:
    # Hashed, as in the idempotency store, so the state file holds no raw
    # addresses or nonces.
    return hashlib.sha256(json.dumps([key[0].lower(), key[1].lower()]).encode()).hexdigest()


class OutcomeUnknownRegistry:
    """Authorizations whose settlement outcome is unknown (#553).

    The transfer may have gone through. A later attempt with the same
    authorization that the facilitator refuses (at verify or at settle) is then
    probably refused BECAUSE the first one spent the nonce, so it must not be
    answered with a 402 and `accepts`: an x402 client pays again from that.

    Write-ahead: every settlement is recorded here (begin_settlement) before
    the facilitator is asked, and the record is removed, on disk, when the
    answer is definite (resolve). So an entry on disk is always either a real
    unknown outcome or a settlement interrupted by a crash or kill, which is one
    too. While this process waits for the facilitator the entry is also "in
    flight" (memory only): a parallel duplicate is told the settlement is in
    progress rather than that its outcome is unknown.

    Persisted (X402_SETTLEMENT_UNKNOWN_STATE_FILE) and rewritten atomically on
    each change: two writes per paid request (write-ahead and resolution),
    offloaded from the event loop by the async wrappers. Entries past their
    expiry are dropped on load, and a test write at load finds an unwritable
    file at startup. An unreadable file is copied aside once and never
    overwritten. Either way `unavailable` is set and the dependency refuses paid
    requests, unpaid, until it is repaired (fail closed): an empty or memory-only
    registry would answer a retry of a possibly collected payment with a 402
    that clients pay from.
    """

    def __init__(self, state_file: Optional[str] = None):
        self._lock = RLock()
        self._state_file = state_file
        # key id -> {"expires": wall-clock seconds, "kind": BEFORE_WORK | AFTER_WORK}
        self._entries: Dict[str, dict] = {}
        # key id -> settlements of it this process is waiting on (never persisted).
        self._inflight: Dict[str, int] = {}
        # Why the file could not be read (never written then), or why the last
        # write failed (retried at most every WRITE_RETRY_SECONDS).
        self._unreadable: Optional[str] = None
        self._write_error: Optional[str] = None
        self._last_write_attempt = 0.0
        self._load()

    @property
    def unavailable(self) -> Optional[str]:
        """Why paid requests must be refused, or None.

        Unreadable file: until it is repaired and the gateway restarted. Failed
        write: an entry held only in memory is lost at a restart, so until a
        later write succeeds. A check retries the write at most once every
        WRITE_RETRY_SECONDS, so requests and /health probes cannot hammer a
        failing disk.
        """
        if self._unreadable:
            return self._unreadable
        if self._write_error and time.monotonic() - self._last_write_attempt >= WRITE_RETRY_SECONDS:
            with self._lock:
                self._save()
        return self._write_error

    def _path(self) -> str:
        return self._state_file or settings.X402_SETTLEMENT_UNKNOWN_STATE_FILE

    def public_cause(self) -> Optional[str]:
        """Short and safe to publish (/health): the file's base name and whether it
        is unreadable or unwritable. Paths and error text stay in the log."""
        cause = "unreadable" if self._unreadable else "unwritable" if self.unavailable else None
        return f"{os.path.basename(self._path())} is {cause}" if cause else None

    def _load(self) -> None:
        path = self._path()
        consequence = ("The gateway runs, but answers every paid (X-PAYMENT) request with 503 "
                       "PAYMENT_STATE_UNAVAILABLE, unpaid, until the file is repaired or restored "
                       "(or moved away deliberately to start empty) and the gateway restarted.")
        try:
            data = load_json_state(path, consequence)
            entries = {} if data is None else data.get("entries")
            if not isinstance(entries, dict) or not all(
                    isinstance(v, dict) and v.get("kind") in (BEFORE_WORK, AFTER_WORK)
                    and isinstance(v.get("expires"), (int, float)) for v in entries.values()):
                raise unreadable_state(path, "expected {\"entries\": {id: {kind, expires}}}", consequence)
        except StateLoadError as e:
            self._unreadable = str(e)
            logger.error("x402: settlement-unknown state is unreadable: %s", e)
            return
        now = time.time()
        self._entries = {k: v for k, v in entries.items() if v["expires"] > now}
        # A file that loads but cannot be written would keep new entries only in
        # memory and lose them at the next restart: find out now (#553).
        with self._lock:
            self._save()

    def _save(self) -> bool:
        """Write the entries; True on success. Call with the lock held."""
        if self._unreadable:
            return False    # never overwrite the only copy
        self._last_write_attempt = time.monotonic()
        try:
            atomic_write_json(self._path(), {"entries": self._entries})
        except Exception as e:
            if not self._write_error:
                logger.error("x402: could not persist settlement-unknown state (%s); paid requests "
                             "get 503 PAYMENT_STATE_UNAVAILABLE until a write succeeds", e)
            self._write_error = (f"State file {self._path()} could not be written ({e}). Paid "
                                 f"requests get 503 PAYMENT_STATE_UNAVAILABLE until a write succeeds.")
            return False
        if self._write_error:
            logger.warning("x402: settlement-unknown state written again; paid requests resume")
        self._write_error = None
        return True

    def _prune(self, now: float) -> None:
        for k in [k for k, v in self._entries.items() if v["expires"] <= now and k not in self._inflight]:
            del self._entries[k]

    def _insert(self, kid: str, kind: str, now: float) -> bool:
        """Insert or refresh kid, then enforce the cap without evicting kid or any
        in-flight entry. False if the cap cannot be met that way."""
        previous = self._entries.get(kid)
        if previous is not None and previous["kind"] == AFTER_WORK:
            kind = AFTER_WORK    # once the work has run, it must not run again
        self._entries[kid] = {"expires": now + OUTCOME_UNKNOWN_SECONDS, "kind": kind}
        excess = len(self._entries) - max(1, OUTCOME_UNKNOWN_MAX_ENTRIES)
        if excess <= 0:
            return True
        # BEFORE_WORK first (a retry of one at worst meets an ordinary refusal),
        # oldest first; AFTER_WORK guards against rerunning work.
        candidates = sorted((k for k in self._entries if k != kid and k not in self._inflight),
                            key=lambda k: (self._entries[k]["kind"] == AFTER_WORK, self._entries[k]["expires"]))
        for k in candidates[:excess]:
            del self._entries[k]
        logger.warning("x402: settlement-unknown registry over its cap; evicted %d",
                       min(excess, len(candidates)))
        return len(candidates) >= excess

    def add(self, key: Optional[Tuple[str, str]], kind: str) -> bool:
        """Record an unknown outcome; True if it is on disk.

        Kept in memory even if the write fails (the registry is then
        unavailable, so paid requests are refused anyway)."""
        if key is None:
            return True
        kid = _key_id(key)
        now = time.time()
        with self._lock:
            self._prune(now)
            self._insert(kid, kind, now)
            return self._save()

    def begin_settlement(self, key: Optional[Tuple[str, str]], kind: str) -> Tuple[Optional[str], bool]:
        """Write-ahead before the facilitator is asked to settle (#553).

        Atomically: reads the kind remembered BEFORE this attempt (None: no
        earlier unknown outcome, so a definite refusal is an ordinary one),
        records `key` (BEFORE_WORK or AFTER_WORK), writes it, and marks it in
        flight. Returns (prior, recorded). If the entry cannot be kept (cap)
        or written, the insert is rolled back and recorded is False: the caller
        must not settle. Call release() when the facilitator call ends.
        """
        if key is None:
            return None, True
        kid = _key_id(key)
        now = time.time()
        with self._lock:
            self._prune(now)
            previous = self._entries.get(kid)
            prior = previous["kind"] if previous is not None and previous["expires"] > now else None
            snapshot = dict(self._entries)
            if not self._insert(kid, kind, now) or not self._save():
                self._entries = snapshot
                if not self._write_error:
                    logger.error("x402: settlement-unknown registry full of entries that cannot be "
                                 "evicted; refusing to settle")
                return prior, False
            self._inflight[kid] = self._inflight.get(kid, 0) + 1
            return prior, True

    def release(self, key: Optional[Tuple[str, str]]) -> None:
        """The facilitator call that begin_settlement announced has ended."""
        if key is None:
            return
        kid = _key_id(key)
        with self._lock:
            n = self._inflight.get(kid, 0) - 1
            if n > 0:
                self._inflight[kid] = n
            else:
                self._inflight.pop(kid, None)

    def resolve(self, key: Optional[Tuple[str, str]], prior: Optional[str], settled: bool) -> None:
        """A definite answer. Settled: no attempt left the authorization unknown,
        the entry goes. Refused: the entry goes unless an earlier attempt's
        outcome is still unknown (`prior`). The removal is written at once, so
        a resolved entry never outlives the process."""
        if key is None or (not settled and prior is not None):
            return
        with self._lock:
            if self._entries.pop(_key_id(key), None) is not None:
                self._save()

    async def abegin_settlement(self, key, kind):
        return await asyncio.to_thread(self.begin_settlement, key, kind)

    async def aresolve(self, key, prior, settled: bool) -> None:
        await asyncio.to_thread(self.resolve, key, prior, settled)

    def in_flight(self, key: Optional[Tuple[str, str]]) -> bool:
        """Whether this process is waiting on the facilitator for `key` right now."""
        if key is None:
            return False
        with self._lock:
            return _key_id(key) in self._inflight

    def kind(self, key: Optional[Tuple[str, str]]) -> Optional[str]:
        """BEFORE_WORK or AFTER_WORK for a remembered authorization, else None."""
        if key is None:
            return None
        with self._lock:
            entry = self._entries.get(_key_id(key))
            if entry is None or entry["expires"] <= time.time():
                return None
            return entry["kind"]

    def contains(self, key: Optional[Tuple[str, str]]) -> bool:
        return self.kind(key) is not None

    def discard(self, key: Optional[Tuple[str, str]]) -> None:
        if key is None:
            return
        with self._lock:
            if self._entries.pop(_key_id(key), None) is not None:
                self._save()

    def reset(self) -> None:
        """For tests: empty, readable, and nothing on disk."""
        with self._lock:
            self._entries.clear()
            self._inflight.clear()
            self._unreadable = None
            self._write_error = None
            self._last_write_attempt = 0.0
            self._save()


outcome_unknown = OutcomeUnknownRegistry()

OUTCOME_UNKNOWN_ADVICE = (
    "Do not pay again from this response. Check on-chain whether this authorization's nonce "
    "was used. If it was, contact the operator with the nonce. If it was not, the facilitator "
    "may still submit it, so before signing a new payment wait until its validBefore has "
    "passed, or cancel it on-chain with EIP-3009 cancelAuthorization."
)


OUTCOME_UNKNOWN_HEADERS = {"X-Payment-Mode": "failed", "X-Payment-Status": "settlement_unknown"}


def payment_state_unavailable_detail() -> dict:
    return {
        "code": "PAYMENT_STATE_UNAVAILABLE",
        "message": ("Paid requests cannot be processed right now; this payment was not "
                    "charged. Retry later with the same X-PAYMENT."),
    }


def settlement_unavailable_detail(request: Request, interrupted: bool = False) -> dict:
    """Body of 502 PAYMENT_SETTLEMENT_UNAVAILABLE: settlement before any work got no answer."""
    if interrupted:
        retry = ("The request was interrupted while waiting for it. This authorization stays "
                 "reserved for up to an hour: a retry with the same X-PAYMENT in that time is "
                 "answered 409 and not charged. ")
    else:
        retry = ("Retry with the same X-PAYMENT while it is valid: if the transfer was "
                 "collected, the retry is not charged again. ")
    return {
        "code": "PAYMENT_SETTLEMENT_UNAVAILABLE",
        "message": ("The payment could not be settled, so nothing was delivered, but it is "
                    "unknown whether the transfer was collected. " + retry + OUTCOME_UNKNOWN_ADVICE),
        "nonce": request_nonce(request),
        "x402_status": "settlement_failed",
    }


def settlement_interrupted(request: Request, kind: str, exc: BaseException) -> None:
    """A settlement request was interrupted (CancelledError) before the facilitator answered.

    The transfer may have been submitted. Recorded like a settlement error, and
    marked on the request so the middleware neither says "not charged" nor
    releases the authorization (#553). Never raises: the caller re-raises.
    """
    try:
        request.state.x402_settlement_interrupted = kind
        outcome_unknown.add(getattr(request.state, "x402_auth_key", None), kind)
        logger.error(f"x402: settlement interrupted ({type(exc).__name__}) on {request.url.path}; "
                     f"outcome unknown")
        log_payment_failed(client_ip=get_client_ip(request),
                           reason=f"settlement interrupted ({type(exc).__name__})", stage="settle",
                           wallet_address=getattr(request.state, "x402_payer", None),
                           nonce=request_nonce(request))
        x402_settlements_total.labels(result="error").inc()
    except Exception as e:
        logger.error(f"x402: could not record an interrupted settlement: {e}")


def request_nonce(request) -> Optional[str]:
    """The canonical nonce of the request's payment authorization, for audit records."""
    key = getattr(getattr(request, "state", None), "x402_auth_key", None)
    return key[1] if key else None


def settlement_in_progress_refusal(auth_key: Tuple[str, str], request: Optional[Request] = None) -> HTTPException:
    """A parallel duplicate of a payment whose settlement this process is waiting on.

    Not an unknown outcome (an answer is coming), and not a 402 (an x402 client
    would sign and pay again). Audited with its own stage, duplicate_in_progress,
    which is not a reconciliation case (#553).
    """
    try:
        log_payment_failed(
            client_ip=get_client_ip(request) if request is not None else None,
            reason="409 PAYMENT_SETTLEMENT_IN_PROGRESS: a duplicate of a payment being settled",
            stage="duplicate_in_progress", wallet_address=auth_key[0], nonce=auth_key[1])
    except Exception as e:
        logger.error(f"x402: could not audit a duplicate payment: {e}")
    return HTTPException(
        status_code=409,
        detail={
            "code": "PAYMENT_SETTLEMENT_IN_PROGRESS",
            "message": ("A settlement for this payment authorization is in progress in another "
                        "request. This request was not run and not charged. Do not sign a new "
                        "payment from this response: wait for the first request's answer (with "
                        "an Idempotency-Key, retry with that key to get it)."),
            "nonce": auth_key[1],
            "x402_status": "settlement_in_progress",
        },
        headers={"X-Payment-Mode": "failed", "X-Payment-Status": "settlement_in_progress",
                 "Retry-After": "5"},
    )


def outcome_unknown_refusal(auth_key: Tuple[str, str], reason: Optional[str] = None, *,
                            where: str, request: Optional[Request] = None,
                            work_ran: bool = False) -> HTTPException:
    """The answer to a later attempt with an authorization whose settlement outcome is unknown.

    409, not 402, and no `accepts`. Whatever refused it, this request did
    nothing and was not charged; the earlier attempt may have been. Audited as
    payment_failed, stage outcome_unknown_retry, with the nonce and `where`
    (verify, in_use, after_work, settle), so the operator sees every retry.
    """
    try:
        log_payment_failed(
            client_ip=get_client_ip(request) if request is not None else None,
            reason=f"409 PAYMENT_AUTHORIZATION_OUTCOME_UNKNOWN at {where}"
                   + (f" ({reason})" if reason else ""),
            stage="outcome_unknown_retry", wallet_address=auth_key[0], nonce=auth_key[1])
    except Exception as e:
        logger.error(f"x402: could not audit an outcome-unknown retry: {e}")
    detail = {
        "code": "PAYMENT_AUTHORIZATION_OUTCOME_UNKNOWN",
        "message": ("An earlier request with this payment authorization was sent for settlement "
                    "and no answer came back, so that transfer may already have been collected. "
                    + ("This request's work ran, but its result is withheld and it was not charged. "
                       if work_ran else "This request did nothing and was not charged. ")
                    + OUTCOME_UNKNOWN_ADVICE),
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
    auth_key = getattr(request.state, "x402_auth_key", None)
    # Write-ahead: on disk before the facilitator is asked, so a kill while
    # waiting leaves an unknown outcome behind, not nothing (#553).
    prior, recorded = await outcome_unknown.abegin_settlement(auth_key, BEFORE_WORK)
    if not recorded:
        raise HTTPException(status_code=503, headers={"Retry-After": "60"},
                            detail=payment_state_unavailable_detail())
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
        raise HTTPException(status_code=502, detail=settlement_unavailable_detail(request),
                            headers=dict(OUTCOME_UNKNOWN_HEADERS))
    except BaseException as e:
        # Interrupted while waiting (CancelledError: a shutdown or a client
        # disconnect). Just as unknown: recorded, then re-raised (#553).
        settlement_interrupted(request, BEFORE_WORK, e)
        raise
    finally:
        outcome_unknown.release(auth_key)

    if not getattr(result, "success", False):
        settlement_refused(request)
        reason = getattr(result, "error_reason", None) or "unknown"
        if prior is not None:
            # Probably refused because the earlier attempt spent the nonce, but
            # the reason does not say so reliably: no 402, no `accepts`.
            logger.warning(f"x402: settlement refused ({reason}) for an authorization whose "
                           f"earlier settlement outcome is unknown")
            log_payment_settled(client_ip=client_ip, payer=payer, transaction_hash=None,
                                network=settings.X402_NETWORK, success=False, error_reason=reason,
                                nonce=request_nonce(request))
            x402_settlements_total.labels(result="refused_after_unknown").inc()
            raise outcome_unknown_refusal(auth_key, reason, where="settle", request=request)
        await outcome_unknown.aresolve(auth_key, None, settled=False)
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
    # Settled now: the transfer happened once, by this attempt.
    await outcome_unknown.aresolve(auth_key, prior, settled=True)
    # From here on a retry with the same Idempotency-Key must not pay again,
    # whatever happens to this request (#359).
    record_settlement(request, result)
    tx = getattr(result, "transaction", None)
    logger.info(f"x402: payment settled before delivery, tx={tx}")
    log_payment_settled(client_ip=client_ip, payer=payer, transaction_hash=tx,
                        network=settings.X402_NETWORK, success=True, nonce=request_nonce(request))
    x402_settlements_total.labels(result="settled").inc()
