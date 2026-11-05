# app/x402/settlement.py
"""
x402 settlement: collect a verified payment before delivering what it pays for.

The dependency (dependency.py) only VERIFIES a payment: it checks the signed
authorization with the facilitator. Money moves when the facilitator SETTLES it.
Three things used to go wrong between the two (#355, #356, #357):

- the work was done first and settled afterwards, so a failed settlement left
  the gateway having bought a batch or granted credit for nothing;
- the settlement result was never checked. FacilitatorClient.settle()
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
import concurrent.futures
import hashlib
import json
import logging
import os
import re
import time
from threading import Lock, RLock
from typing import Dict, Optional, Tuple

from fastapi import HTTPException, Request

from app.core.io_lane import IOLane
from app.core.atomic_io import StateLoadError, atomic_write_json, load_json_state, unreadable_state
from app.core.config import settings
from app.core.client_ip import get_client_ip
from app.x402.audit import log_payment_failed, log_payment_settled
from app.x402.networks import to_v1
from app.x402.payment_headers import paid_version, paid_version_label
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

    def is_reserved(self, key: Optional[Tuple[str, str]]) -> bool:
        """Whether `key` is reserved and not expired. Read-only (#589)."""
        if key is None:
            return False
        with self._lock:
            until = self._reserved.get(key)
            return until is not None and until > time.monotonic()

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


# Registry disk I/O runs on its own lane (app/core/io_lane.py): not the default
# executor, which long blocking calls (an on-chain createBatch holds a worker
# for minutes) share, and not the idempotency store's lane, so a hang on one
# state file fails closed only the requests that need it.
_REGISTRY_IO_WORKERS = 2
# Write-aheads are refused (503) when the oldest pending registry write has
# waited longer than the write timeout (a hung disk), or when this many writes
# are pending (far above any healthy burst). An ordinary burst just queues.
MAX_PENDING_REGISTRY_IO = 64

_registry_lane = IOLane(_REGISTRY_IO_WORKERS, "x402-settlement-state")


def _registry_executor():
    return _registry_lane.executor()


def _submit_registry_io(fn, *args) -> concurrent.futures.Future:
    """Run fn(*args) on the registry lane; tracked so shutdown can wait for it."""
    return _registry_lane.submit(fn, *args)


def _registry_io_backlog() -> int:
    return _registry_lane.backlog()


def _registry_io_oldest_age() -> float:
    """Seconds the oldest pending registry write has been queued or running."""
    return _registry_lane.oldest_age()


def shutdown_registry_io(timeout: float = 5.0) -> None:
    """At gateway shutdown: wait, at most `timeout` seconds, for pending registry
    writes (write-aheads, resolutions, background removals and retries), then
    release the lane. Its daemon workers cannot hold up the process exit."""
    _registry_lane.shutdown(timeout)


class OutcomeUnknownRegistry:
    """Authorizations whose settlement outcome is unknown (#553).

    The transfer may have gone through. A later attempt with the same
    authorization that the facilitator refuses (at verify or at settle) is then
    probably refused BECAUSE the first one spent the nonce, so it must not be
    answered with a 402 and `accepts`: an x402 client pays again from that.

    Write-ahead: every settlement is recorded here (begin_settlement) before
    the facilitator is asked, and the record is removed, on disk, when the
    answer is definite (resolve). An entry on disk is therefore a real unknown
    outcome (no answer, or a settlement cut off by a crash or kill), except in
    one rare case: a request cancelled while its write-ahead was being written
    has the entry removed again by a background write, and if the process dies
    before that write, the entry stays. Its only effect is that a refusal of
    that authorization is answered 409 instead of 402 (no charge either way).
    While this process waits for the facilitator the entry is also "in flight"
    (memory only): a parallel duplicate is told the settlement is in progress.

    Locking: `_lock` guards the in-memory state and is never held across disk
    I/O, so the loop-side reads (in_flight, kind, contains, release) never wait
    for a write. Writes take a snapshot under `_lock`, then write it under
    `_write_lock`; a sequence number makes sure an older snapshot never
    overwrites a newer one. Two writes per paid request (write-ahead and
    resolution), both run off the event loop by the async wrappers; a write
    retry while the file is unwritable runs in a background thread.

    Persisted (X402_SETTLEMENT_UNKNOWN_STATE_FILE). Entries past their expiry
    are dropped on load, and a test write at load finds an unwritable file at
    startup. An unreadable file is copied aside once and never overwritten.
    Either way `unavailable` is set and the dependency refuses paid requests,
    unpaid, until it is repaired (fail closed).
    """

    def __init__(self, state_file: Optional[str] = None):
        self._lock = RLock()
        self._write_lock = Lock()
        self._state_file = state_file
        # key id -> {"expires": wall-clock seconds, "kind": BEFORE_WORK | AFTER_WORK}
        self._entries: Dict[str, dict] = {}
        # key id -> settlements of it this process is waiting on (never persisted).
        self._inflight: Dict[str, int] = {}
        # key id -> generation of the write-ahead that last inserted it (memory
        # only), so a late removal never takes a newer attempt's entry.
        self._gens: Dict[str, int] = {}
        self._gen_counter = 0
        # key id -> [generation, own prior, abandoned] of a write-ahead whose
        # facilitator call has not started (memory only). Such an entry is not an
        # unknown outcome: reads report its own prior, and a later write-ahead
        # inherits that prior rather than counting the entry as one.
        self._unasked: Dict[str, list] = {}
        # Generations whose caller gave up before their write-ahead started.
        self._cancelled_gens: set = set()
        # Set when a write did not finish in time (hung disk) or too many are
        # pending; cleared when a write completes. Makes `unavailable` true.
        self._stalled: Optional[str] = None
        # Why the file could not be read (never written then), or why the last
        # write failed (retried at most every WRITE_RETRY_SECONDS).
        self._unreadable: Optional[str] = None
        self._write_error: Optional[str] = None
        self._last_write_attempt = 0.0
        self._retrying = False
        self._seq = 0            # last snapshot taken
        self._written_seq = 0    # last snapshot on disk
        self._load()

    @property
    def unavailable(self) -> Optional[str]:
        """Why paid requests must be refused, or None. Never writes on the caller's
        thread: a due retry (at most every WRITE_RETRY_SECONDS) runs in the
        background, so requests and /health probes neither block on nor hammer
        a failing disk."""
        if self._unreadable:
            return self._unreadable
        if self._write_error:
            self._retry_in_background()
            return self._write_error
        if self._stalled:
            # A probe write, so the stall clears even if nothing else writes:
            # paid requests are refused meanwhile, so none would.
            self._retry_in_background(only_if_idle=True)
        return self._stalled

    def _retry_in_background(self, only_if_idle: bool = False) -> None:
        """A write (retry or probe) in the background, at most every
        WRITE_RETRY_SECONDS. only_if_idle: skip while other writes are pending,
        whose completion clears a stall anyway."""
        if only_if_idle and _registry_io_backlog():
            return
        with self._lock:
            if self._retrying or time.monotonic() - self._last_write_attempt < WRITE_RETRY_SECONDS:
                return
            self._retrying = True
            self._last_write_attempt = time.monotonic()

        def run():
            try:
                self._persist()
            finally:
                with self._lock:
                    self._retrying = False

        _submit_registry_io(run)

    def _path(self) -> str:
        return self._state_file or settings.X402_SETTLEMENT_UNKNOWN_STATE_FILE

    def state_unreadable(self) -> bool:
        """Whether the state file could not be read at startup (#468). Fixed
        until a restart: the file is read only then."""
        return bool(self._unreadable)

    def write_failed(self) -> bool:
        """Whether the last write failed and paid requests are refused until a
        write of the latest state succeeds (#585). Reads the flag only."""
        return bool(self._write_error)

    def write_stalled(self) -> bool:
        """Whether a write did not finish in time, or too many were pending, and
        paid requests are refused until a write completes (#585). Reads the flag only."""
        return bool(self._stalled)

    def public_cause(self) -> Optional[str]:
        """Short and safe to publish (/health): the file's base name and whether it
        is unreadable or unwritable. Paths and error text stay in the log."""
        cause = ("unreadable" if self._unreadable else "unwritable" if self._write_error
                 else "unresponsive" if self.unavailable else None)
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
        self._persist()

    # --- persistence --------------------------------------------------------

    def _snapshot(self) -> Tuple[int, Dict[str, dict]]:
        with self._lock:
            self._seq += 1
            return self._seq, {k: dict(v) for k, v in self._entries.items()}

    def _write(self, seq: int, snapshot: Dict[str, dict]) -> bool:
        """Write `snapshot` (taken as number `seq`) unless a newer one is already on
        disk; True if the state of that snapshot is on disk. No `_lock` held
        during the I/O."""
        if self._unreadable:
            return False    # never overwrite the only copy
        with self._write_lock:
            if seq <= self._written_seq:
                return True     # a newer snapshot, which includes this one's changes, is on disk
            self._last_write_attempt = time.monotonic()
            try:
                atomic_write_json(self._path(), {"entries": snapshot})
            except Exception as e:
                with self._lock:
                    self._clear_stalled()
                    if not self._write_error:
                        logger.error("x402: could not persist settlement-unknown state (%s); paid "
                                     "requests get 503 PAYMENT_STATE_UNAVAILABLE until a write "
                                     "succeeds", e)
                    self._write_error = (f"State file {self._path()} could not be written ({e}). Paid "
                                         f"requests get 503 PAYMENT_STATE_UNAVAILABLE until a write "
                                         f"succeeds.")
                return False
            self._written_seq = seq
            with self._lock:
                self._clear_stalled()
                # Only the latest snapshot proves memory and disk agree.
                if seq == self._seq and self._write_error:
                    logger.warning("x402: settlement-unknown state written again; paid requests resume")
                    self._write_error = None
            return True

    def _persist(self) -> bool:
        return self._write(*self._snapshot())

    def _clear_stalled(self) -> None:
        """Call with `_lock` held: a write completed."""
        if self._stalled:
            logger.warning("x402: settlement-state write completed again; paid requests resume")
            self._stalled = None

    def mark_stalled(self, why: str) -> None:
        with self._lock:
            if not self._stalled:
                logger.error("x402: settlement-state disk I/O is not responding (%s); paid requests "
                             "get 503 PAYMENT_STATE_UNAVAILABLE until a write completes", why)
            self._stalled = why

    # --- state --------------------------------------------------------------

    def _prune(self, now: float) -> None:
        for k in [k for k, v in self._entries.items() if v["expires"] <= now and k not in self._inflight]:
            del self._entries[k]

    def _insert(self, kid: str, kind: str, now: float,
                version: int = 1) -> Tuple[bool, Optional[dict], Dict[str, dict]]:
        """Insert or refresh kid under `_lock`, then enforce the cap by evicting
        before_work entries only (oldest first), never kid, an in-flight entry or
        an after_work entry (its work would run again). Returns (kept, previous
        value of kid, evicted). Not kept: nothing changed.

        The entry records the x402 version of the attempt (#607) as
        "version": 2; no field means 1, so v1 entries are as before. The key
        itself is version-neutral (D5): one authorization, either envelope."""
        previous = self._entries.get(kid)
        if previous is not None and previous["kind"] == AFTER_WORK:
            kind = AFTER_WORK    # once the work has run, it must not run again
        excess = len(self._entries) + (0 if previous is not None else 1) - max(1, OUTCOME_UNKNOWN_MAX_ENTRIES)
        candidates = []
        if excess > 0:
            candidates = sorted((k for k, v in self._entries.items()
                                 if k != kid and k not in self._inflight and v["kind"] == BEFORE_WORK),
                                key=lambda k: self._entries[k]["expires"])
            if len(candidates) < excess:
                logger.error("x402: settlement-unknown registry full of entries that may not be "
                             "evicted (after_work or in flight)")
                return False, previous, {}
        evicted = {k: self._entries.pop(k) for k in candidates[:max(0, excess)]}
        if evicted:
            logger.warning("x402: settlement-unknown registry over its cap; evicted %d", len(evicted))
        self._entries[kid] = {"expires": now + OUTCOME_UNKNOWN_SECONDS, "kind": kind}
        if version == 2:
            self._entries[kid]["version"] = 2
        return True, previous, evicted

    def _rollback(self, kid: str, previous: Optional[dict], evicted: Dict[str, dict]) -> None:
        with self._lock:
            if previous is None:
                self._entries.pop(kid, None)
            else:
                self._entries[kid] = previous
            for k, v in evicted.items():
                self._entries.setdefault(k, v)

    def add(self, key: Optional[Tuple[str, str]], kind: str) -> bool:
        """Record an unknown outcome directly; True if it is on disk. Kept in memory
        even over the cap or if the write fails (then the registry is
        unavailable). Blocking: not for the event loop."""
        if key is None:
            return True
        kid = _key_id(key)
        now = time.time()
        with self._lock:
            self._prune(now)
            kept, _, _ = self._insert(kid, kind, now)
            if not kept:
                previous = self._entries.get(kid)
                self._entries[kid] = {"expires": now + OUTCOME_UNKNOWN_SECONDS,
                                      "kind": AFTER_WORK if (previous or {}).get("kind") == AFTER_WORK else kind}
        return self._persist()

    def begin_settlement(self, key: Optional[Tuple[str, str]], kind: str) -> Tuple[Optional[str], bool]:
        """Write-ahead before the facilitator is asked to settle (#553). Blocking.

        Under one `_lock`: reads the kind remembered BEFORE this attempt (None:
        no earlier unknown outcome, so a definite refusal is an ordinary one),
        records `key` (BEFORE_WORK or AFTER_WORK) and marks it in flight; then
        writes it. Returns (prior, recorded). If the entry cannot be kept (cap)
        or written, the change is rolled back and recorded is False: the
        caller must not settle. Call release() when the facilitator call ends.
        """
        prior, recorded, gen = self.begin_settlement_ex(key, kind)
        if recorded:
            self.mark_asked(key, gen)     # synchronous callers ask the facilitator next
        return prior, recorded

    def next_generation(self) -> int:
        with self._lock:
            self._gen_counter += 1
            return self._gen_counter

    def begin_settlement_ex(self, key: Optional[Tuple[str, str]], kind: str,
                            gen: Optional[int] = None,
                            version: int = 1) -> Tuple[Optional[str], bool, Optional[int]]:
        """begin_settlement, also returning the generation of this write-ahead,
        which resolve() and remove_abandoned() check. `gen` is allocated by the
        async caller beforehand, so it can give up on it before this runs.

        Refused (recorded False) when the key is already in flight, and when
        the caller already gave up on `gen`. An abandoned, un-asked write-ahead
        found for the key is not a prior unknown outcome: its own prior is
        inherited instead."""
        if key is None:
            return None, True, None
        kid = _key_id(key)
        now = time.time()
        with self._lock:
            if gen is None:
                self._gen_counter += 1
                gen = self._gen_counter
            if gen in self._cancelled_gens:
                self._cancelled_gens.discard(gen)
                return None, False, None
            if kid in self._inflight:
                return self._kind_locked(kid, now), False, None
            self._prune(now)
            prior = self._kind_locked(kid, now)
            previous_unasked = self._unasked.get(kid)
            kept, previous, evicted = self._insert(kid, kind, now, version)
            if not kept:
                return prior, False, None
            self._inflight[kid] = self._inflight.get(kid, 0) + 1
            previous_gen = self._gens.get(kid)
            self._gens[kid] = gen
            self._unasked[kid] = [gen, prior, False]
        if not self._persist():
            with self._lock:
                u = self._unasked.get(kid)
                released_by_abandon = u is not None and u[0] == gen and u[2]
                # Only while this write-ahead still owns the key: a newer one
                # (possible only after an abandon) must not be undone.
                owns = self._gens.get(kid) == gen
                if owns:
                    self._rollback(kid, previous, evicted)
                    if previous_gen is None:
                        self._gens.pop(kid, None)
                    else:
                        self._gens[kid] = previous_gen
                    if previous_unasked is None:
                        self._unasked.pop(kid, None)
                    else:
                        self._unasked[kid] = previous_unasked
                    if not released_by_abandon:
                        n = self._inflight.get(kid, 0) - 1
                        if n > 0:
                            self._inflight[kid] = n
                        else:
                            self._inflight.pop(kid, None)
            return prior, False, None
        return prior, True, gen

    def mark_asked(self, key: Optional[Tuple[str, str]], gen: Optional[int]) -> None:
        """The facilitator is about to be asked: from now on the entry is a real
        settlement attempt, so an outcome that stays unknown is one."""
        if key is None or gen is None:
            return
        kid = _key_id(key)
        with self._lock:
            u = self._unasked.get(kid)
            if u is not None and u[0] == gen:
                del self._unasked[kid]

    def abandon(self, key: Optional[Tuple[str, str]], gen: Optional[int]) -> None:
        """The caller gave up (timeout or cancellation) before asking the
        facilitator. At once, on the caller's thread: the key is no longer in
        flight, and its entry no longer reads as an unknown outcome. The entry
        itself is removed once the write-ahead's write has finished."""
        if key is None or gen is None:
            return
        kid = _key_id(key)
        with self._lock:
            u = self._unasked.get(kid)
            if u is not None and u[0] == gen:
                if not u[2]:
                    u[2] = True
                    n = self._inflight.get(kid, 0) - 1
                    if n > 0:
                        self._inflight[kid] = n
                    else:
                        self._inflight.pop(kid, None)
            elif self._gens.get(kid) != gen:
                # Not started yet (or already rolled back): never start it.
                self._cancelled_gens.add(gen)

    def _kind_locked(self, kid: str, now: float) -> Optional[str]:
        """Kind as a prior unknown outcome; an un-asked write-ahead reports its
        own prior. Call with `_lock` held."""
        u = self._unasked.get(kid)
        if u is not None:
            return u[1]
        entry = self._entries.get(kid)
        return entry["kind"] if entry is not None and entry["expires"] > now else None

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
        a resolved entry never outlives the process. Blocking."""
        self._remove(key, prior, settled, gen=None)

    def _remove(self, key, prior, settled: bool, gen: Optional[int], require_idle: bool = False) -> None:
        if key is None or (not settled and prior is not None):
            return
        kid = _key_id(key)
        with self._lock:
            if gen is not None and self._gens.get(kid) != gen:
                return      # a newer write-ahead owns the entry now
            if require_idle and kid in self._inflight:
                return
            u = self._unasked.get(kid)
            if u is not None and (gen is None or u[0] == gen):
                del self._unasked[kid]
            removed = self._entries.pop(kid, None) is not None
            self._gens.pop(kid, None)
        if removed:
            self._persist()

    def _resolve_then_release(self, key, prior, settled: bool, gen: Optional[int], release: bool) -> None:
        try:
            self._remove(key, prior, settled, gen)
        finally:
            if release:
                self.release(key)

    def remove_abandoned(self, key, prior: Optional[str], gen: Optional[int]) -> None:
        """Remove the entry of a write-ahead whose caller gave up before asking the
        facilitator (cancelled or timed out): only if it is still that write-ahead's
        entry (same generation) and no settlement of the key is in flight. Kept
        if an earlier unknown outcome existed (`prior`). Blocking."""
        if gen is None or key is None:
            return
        kid = _key_id(key)
        with self._lock:
            u = self._unasked.get(kid)
            if u is not None and u[0] == gen:
                prior = u[1]       # the write-ahead's own prior
                if prior is not None:
                    del self._unasked[kid]     # the earlier unknown outcome stays, as itself
                    return
        self._remove(key, prior, settled=False, gen=gen, require_idle=True)

    async def abegin_settlement(self, key, kind, version: int = 1):
        """begin_settlement off the event loop, on the registry pool. Returns
        (prior, recorded, gen); the entry is not yet "asked" (mark_asked, give_up). Refused at
        once (recorded False, so 503) while too many registry writes are pending.
        Waits at most X402_SETTLEMENT_STATE_WRITE_TIMEOUT_SECONDS: a hung disk
        then fails closed instead of hanging the request, the registry is marked
        unavailable (stalled) until a write completes, and the key is released
        at once. If the caller is cancelled or gives up, the write still
        finishes; since the facilitator was never asked, the entry is then
        removed (generation-checked, in the background)."""
        if key is None:
            return None, True, None
        timeout = settings.X402_SETTLEMENT_STATE_WRITE_TIMEOUT_SECONDS
        if _registry_io_oldest_age() > timeout:
            self.mark_stalled(f"a settlement-state write has been pending for over {timeout}s")
            return None, False, None
        if _registry_io_backlog() >= MAX_PENDING_REGISTRY_IO:
            self.mark_stalled(f"{MAX_PENDING_REGISTRY_IO} settlement-state writes pending")
            return None, False, None
        gen = self.next_generation()
        cfut = _submit_registry_io(self.begin_settlement_ex, key, kind, gen, version)
        inner = asyncio.wrap_future(cfut)
        try:
            prior, recorded, got = await asyncio.wait_for(
                asyncio.shield(inner), timeout=settings.X402_SETTLEMENT_STATE_WRITE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            self.mark_stalled("write-ahead did not finish in "
                              f"{settings.X402_SETTLEMENT_STATE_WRITE_TIMEOUT_SECONDS}s")
            self._give_up(key, gen, cfut)
            return None, False, None
        except asyncio.CancelledError:
            self._give_up(key, gen, cfut)
            raise
        # Not yet "asked": the caller marks it (mark_asked) right before calling
        # the facilitator, or gives it up (give_up) if it does not get that far.
        return prior, recorded, got

    def give_up(self, key, gen: Optional[int]) -> None:
        """A recorded write-ahead whose caller will not call the facilitator after
        all (a later step failed): released now, removed in the background."""
        if key is None or gen is None:
            return
        self.abandon(key, gen)
        _submit_registry_io(self.remove_abandoned, key, None, gen)

    def _give_up(self, key, gen: int, cfut) -> None:
        self.abandon(key, gen)
        # On the thread future, so it runs even if the event loop is gone.
        cfut.add_done_callback(lambda f: self._abandon_begin(key, gen, f))

    def _abandon_begin(self, key, gen: int, fut) -> None:
        with self._lock:
            self._cancelled_gens.discard(gen)
        if fut.cancelled() or fut.exception() is not None:
            return
        prior, recorded, _ = fut.result()
        if not recorded:
            return
        # Recorded means its write landed: the disk responds again, even if the
        # timeout that marked the stall fired after that write completed.
        with self._lock:
            self._clear_stalled()
        _submit_registry_io(self.remove_abandoned, key, prior, gen)

    async def aresolve(self, key, prior, settled: bool, gen: Optional[int] = None,
                       release: bool = False) -> None:
        """resolve off the event loop, on the registry pool; finishes even if the
        caller is cancelled. With release, the in-flight mark is dropped in that
        thread after the write (so also after a cancellation). Waits at most the
        write timeout: the records it follows are already written, so a hung disk
        does not hang the response."""
        inner = asyncio.wrap_future(_submit_registry_io(self._resolve_then_release, key, prior,
                                                        settled, gen, release))
        try:
            await asyncio.wait_for(asyncio.shield(inner),
                                   timeout=settings.X402_SETTLEMENT_STATE_WRITE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.error("x402: settlement-state resolution write did not finish in time; it "
                         "continues in the background")

    def state(self, key: Optional[Tuple[str, str]]) -> Tuple[bool, Optional[str]]:
        """(in flight, kind) read under one lock. Use it, not in_flight() then
        kind(), where the answer depends on both: a write-ahead on another thread
        can insert the entry and mark it in flight between two separate reads."""
        if key is None:
            return False, None
        kid = _key_id(key)
        with self._lock:
            return kid in self._inflight, self._kind_locked(kid, time.time())

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
            return self._kind_locked(_key_id(key), time.time())

    def contains(self, key: Optional[Tuple[str, str]]) -> bool:
        return self.kind(key) is not None

    def discard(self, key: Optional[Tuple[str, str]]) -> None:
        if key is None:
            return
        with self._lock:
            removed = self._entries.pop(_key_id(key), None) is not None
        if removed:
            self._persist()

    def reset(self) -> None:
        """For tests: empty, readable, and nothing on disk."""
        with self._lock:
            self._entries.clear()
            self._inflight.clear()
            self._unasked.clear()
            self._cancelled_gens.clear()
            self._stalled = None
            self._unreadable = None
            self._write_error = None
            self._last_write_attempt = 0.0
        self._persist()


outcome_unknown = OutcomeUnknownRegistry()

OUTCOME_UNKNOWN_ADVICE = (
    "Do not pay again from this response. Check on-chain whether this authorization's nonce "
    "was used. If it was, contact the operator with the nonce. If it was not, the facilitator "
    "may still submit it, so before signing a new payment wait until its validBefore has "
    "passed, or cancel it on-chain with EIP-3009 cancelAuthorization."
)


OUTCOME_UNKNOWN_HEADERS = {"X-Payment-Mode": "failed", "X-Payment-Status": "settlement_unknown"}

# A facilitator's refusal reason, as a caller may see it (#576). Facilitators
# answer with machine codes ("insufficient_funds",
# "invalid_exact_evm_nonce_already_used"), which tell the client what to fix,
# but also with free text: a refused settle on Base Sepolia returned a
# multi-line RPC error naming the RPC URL and quoting the request body. Only a
# short code is shown; anything else is replaced by fixed text, and the full
# reason goes to the log and the audit record only.
_FACILITATOR_CODE_RE = re.compile(r"[a-z0-9_]{1,64}")
FACILITATOR_REASON_HIDDEN = "facilitator error; details in the gateway log"


def public_facilitator_reason(reason) -> str:
    """The facilitator's reason if it is a short machine code, else fixed text."""
    if isinstance(reason, str) and _FACILITATOR_CODE_RE.fullmatch(reason):
        return reason
    return FACILITATOR_REASON_HIDDEN


def payment_state_unavailable_detail(work_ran: bool = False) -> dict:
    if work_ran:
        message = ("This request's work ran, but its payment cannot be settled right now, so the "
                   "result is withheld and this payment was not charged. A retry with the same "
                   "X-PAYMENT runs the work again; you are charged at most once.")
    else:
        message = ("Paid requests cannot be processed right now; this payment was not "
                   "charged. Retry later with the same X-PAYMENT.")
    return {"code": "PAYMENT_STATE_UNAVAILABLE", "message": message}


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

    The transfer may have been submitted. The write-ahead entry (of this kind,
    already on disk before the facilitator was asked) is the record, so nothing
    is written here and nothing can be lost to the cancellation. Audited like a
    settlement error, and marked on the request so the middleware neither says
    "not charged" nor releases the authorization (#553). Never raises: the
    caller re-raises.
    """
    try:
        request.state.x402_settlement_interrupted = kind
        logger.error(f"x402: settlement interrupted ({type(exc).__name__}) on {request.url.path}; "
                     f"outcome unknown")
        log_payment_failed(client_ip=get_client_ip(request),
                           reason=f"settlement interrupted ({type(exc).__name__})", stage="settle",
                           wallet_address=getattr(request.state, "x402_payer", None),
                           nonce=request_nonce(request), x402_version=paid_version(request))
        x402_settlements_total.labels(result="error", version=paid_version_label(request)).inc()
    except Exception as e:
        logger.error(f"x402: could not record an interrupted settlement: {e}")


def request_nonce(request) -> Optional[str]:
    """The canonical nonce of the request's payment authorization, for audit records."""
    key = getattr(getattr(request, "state", None), "x402_auth_key", None)
    return key[1] if key else None


def settlement_in_progress_refusal(auth_key: Tuple[str, str], request: Optional[Request] = None,
                                   work_ran: bool = False) -> HTTPException:
    """A parallel duplicate of a payment whose settlement this process is waiting on.

    Not an unknown outcome (an answer is coming), and not a 402 (an x402 client
    would sign and pay again). Audited with its own stage, duplicate_in_progress,
    which is not a reconciliation case (#553).
    """
    try:
        log_payment_failed(
            client_ip=get_client_ip(request) if request is not None else None,
            reason="409 PAYMENT_SETTLEMENT_IN_PROGRESS: a duplicate of a payment being settled",
            stage="duplicate_in_progress", wallet_address=auth_key[0], nonce=auth_key[1],
            x402_version=paid_version(request))
    except Exception as e:
        logger.error(f"x402: could not audit a duplicate payment: {e}")
    return HTTPException(
        status_code=409,
        detail={
            "code": "PAYMENT_SETTLEMENT_IN_PROGRESS",
            "message": ("Another request with this payment authorization is being processed. "
                        + ("This request's work ran, but its result is withheld and it was not charged. "
                           if work_ran else "This request was not run and not charged. ")
                        + "Do not resend this X-PAYMENT: "
                        "once that request has settled it is answered 402 \"already used\", and "
                        "paying from that would charge twice. Wait for that request's answer. If "
                        "that request was interrupted (it ended without a settled result, for "
                        "example with a 500 or 502), treat its payment as an unknown outcome: check on-chain "
                        "whether this authorization's nonce was used before paying again. With an "
                        "Idempotency-Key, get the stored result by sending the same key with a "
                        "newly signed payment (the stored result is returned and the new payment "
                        "is not charged). Without a key there is no safe way to fetch it again."),
            "nonce": auth_key[1],
            "x402_status": "settlement_in_progress",
        },
        # No Retry-After: retrying the same X-PAYMENT is exactly what must not happen.
        headers={"X-Payment-Mode": "failed", "X-Payment-Status": "settlement_in_progress"},
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
            stage="outcome_unknown_retry", wallet_address=auth_key[0], nonce=auth_key[1],
            x402_version=paid_version(request))
    except Exception as e:
        logger.error(f"x402: could not audit an outcome-unknown retry: {e}")
    detail = {
        "code": "PAYMENT_AUTHORIZATION_OUTCOME_UNKNOWN",
        "message": ("An earlier request with this payment authorization ended without a settlement "
                    "answer, so that transfer may already have been collected. "
                    + ("This request's work ran, but its result is withheld and it was not charged. "
                       if work_ran else "This request did nothing and was not charged. ")
                    + OUTCOME_UNKNOWN_ADVICE),
        "nonce": auth_key[1],
        "x402_status": "settlement_unknown",
    }
    if reason:
        detail["reason"] = public_facilitator_reason(reason)    # the audit record has it in full
    return HTTPException(status_code=409, detail=detail, headers=dict(OUTCOME_UNKNOWN_HEADERS))


_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}\Z")
_NONCE = re.compile(r"^(0x)?[0-9a-fA-F]{64}\Z")


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


def facilitator_requirements(request):
    """The requirements to send the facilitator for this request's payment:
    the gateway's own v2 offer for an x402 v2 payment, else the v1
    requirements (#607). The envelope's x402Version follows the payment."""
    state = getattr(request, "state", None)
    if getattr(state, "x402_version", None) == 2:
        return getattr(state, "x402_requirements_v2", None)
    return getattr(state, "x402_requirements", None)


# A settled transaction as the gateway passes it on: a 32-byte hex hash.
_TX_HASH = re.compile(r"^0x[0-9a-fA-F]{64}\Z")


def valid_transaction(value) -> Optional[str]:
    """`value` if it is a 0x-prefixed 32-byte hex transaction hash, else None.

    Anything a facilitator sends is checked with this before it reaches a
    header, a body, a log line or a stored record: a non-ASCII value made
    Starlette raise on the X-Payment-Transaction header, and a CR/LF or a
    huge value broke the response, each after the payment had settled.
    """
    if isinstance(value, str) and _TX_HASH.match(value):
        return value
    return None


def checked_settlement(result):
    """The facilitator's settle answer with an unusable `transaction` taken
    as unknown (#607): None on a v1 SettleResponse, "" on a v2
    SettleResponseV2 (its required field). Everything else is kept, so a paid
    result is still delivered with its receipt; readers show an unknown
    transaction as "unknown", as v1 always has. One place, applied where
    each settle call returns (settle_payment and the middleware fallback)."""
    tx = getattr(result, "transaction", None)
    if tx is None or tx == "" or valid_transaction(tx) is not None:
        return result
    length = len(tx) if isinstance(tx, str) else -1
    logger.warning(f"x402: facilitator transaction is not a 32-byte hex hash "
                   f"(type {type(tx).__name__}, length {length}); treated as unknown")
    from app.x402.models_v2 import SettleResponseV2
    unknown = "" if isinstance(result, SettleResponseV2) else None
    try:
        return result.model_copy(update={"transaction": unknown})
    except Exception:
        try:
            result.transaction = unknown
        except Exception:
            pass
        return result


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

    # The Bee call gate must never answer for this request with "nothing was
    # charged" from here on (#588).
    from app.services.bee_gate import mark_payment_attempted
    mark_payment_attempted()

    # For an Idempotency-Key: from here until the facilitator answers, the
    # outcome is unknown, and a retry must not be charged on the assumption
    # that nothing happened (idempotency.py).
    from app.x402.idempotency import arecord_settling, record_settlement, settlement_refused
    auth_key = getattr(request.state, "x402_auth_key", None)
    # Write-ahead: on disk before the facilitator is asked, so a kill while
    # waiting leaves an unknown outcome behind, not nothing (#553).
    prior, recorded, gen = await outcome_unknown.abegin_settlement(auth_key, BEFORE_WORK,
                                                                   version=paid_version(request))
    if not recorded:
        if outcome_unknown.in_flight(auth_key):
            raise settlement_in_progress_refusal(auth_key, request)
        raise HTTPException(status_code=503, headers={"Retry-After": "60"},
                            detail=payment_state_unavailable_detail())
    # The Idempotency-Key's "settling" record, off the event loop and bounded:
    # not on disk in time means no settlement, like a failed write-ahead.
    try:
        settling_recorded = await arecord_settling(request)
    except BaseException:
        outcome_unknown.give_up(auth_key, gen)
        raise
    if not settling_recorded:
        outcome_unknown.give_up(auth_key, gen)
        raise HTTPException(status_code=503, headers={"Retry-After": "60"},
                            detail=payment_state_unavailable_detail())
    outcome_unknown.mark_asked(auth_key, gen)
    try:
        result = checked_settlement(await _facilitator().settle(
            payment=payment, payment_requirements=facilitator_requirements(request)))
    except Exception as e:
        # The outcome is unknown: the facilitator may or may not have submitted
        # the transfer. Deliver nothing. The middleware then releases the
        # reservation, which is safe: a retry with the same authorization is
        # delivered only if its own settlement succeeds, and a transfer that did
        # go through spent the nonce, so it cannot.
        outcome_unknown.release(auth_key)    # an unknown outcome now, no longer in flight
        logger.error(f"x402: settlement error before delivery: {type(e).__name__}: {e}", exc_info=True)
        log_payment_failed(client_ip=client_ip, reason=f"{type(e).__name__}: {e}",
                           stage="settle", wallet_address=payer, nonce=request_nonce(request),
                           x402_version=paid_version(request))
        x402_settlements_total.labels(result="error", version=paid_version_label(request)).inc()
        # The write-ahead entry (BEFORE_WORK, on disk) is the record: no write here.
        raise HTTPException(status_code=502, detail=settlement_unavailable_detail(request),
                            headers=dict(OUTCOME_UNKNOWN_HEADERS))
    except BaseException as e:
        # Interrupted while waiting (CancelledError: a shutdown or a client
        # disconnect). Just as unknown: recorded, then re-raised (#553).
        outcome_unknown.release(auth_key)
        settlement_interrupted(request, BEFORE_WORK, e)
        raise
    # A definite answer: the request stays in flight until its resolution is
    # written (aresolve releases it), so a duplicate in between is told
    # "in progress", not "outcome unknown".

    if not getattr(result, "success", False):
        settlement_refused(request)
        reason = getattr(result, "error_reason", None) or "unknown"
        if prior is not None:
            # Probably refused because the earlier attempt spent the nonce, but
            # the reason does not say so reliably: no 402, no `accepts`.
            logger.warning(f"x402: settlement refused ({reason}) for an authorization whose "
                           f"earlier settlement outcome is unknown")
            log_payment_settled(client_ip=client_ip, payer=payer, transaction_hash=None,
                                network=to_v1(settings.X402_NETWORK), success=False, error_reason=reason,
                                nonce=request_nonce(request), x402_version=paid_version(request))
            x402_settlements_total.labels(result="refused_after_unknown", version=paid_version_label(request)).inc()
            outcome_unknown.release(auth_key)    # nothing to resolve: the entry stays
            raise outcome_unknown_refusal(auth_key, reason, where="settle", request=request)
        logger.warning(f"x402: settlement refused before delivery: {reason}")
        log_payment_settled(client_ip=client_ip, payer=payer, transaction_hash=None,
                            network=to_v1(settings.X402_NETWORK), success=False, error_reason=reason,
                            nonce=request_nonce(request), x402_version=paid_version(request))
        x402_settlements_total.labels(result="refused", version=paid_version_label(request)).inc()
        # Last: a cancellation while the removal is written loses no record.
        await outcome_unknown.aresolve(auth_key, None, settled=False, gen=gen, release=True)
        # The log and the audit record above keep the full reason; the caller
        # sees it only if it is a short machine code (#576).
        shown = public_facilitator_reason(reason)
        message = f"The payment could not be settled ({shown}). Nothing was delivered."
        detail = {
            "code": "PAYMENT_SETTLEMENT_FAILED",
            "message": message,
            "x402_status": "settlement_failed",
            "reason": shown,
        }
        # Also a well-formed x402 Payment Required body, so a client can sign a
        # fresh payment from it like from any other 402 (#372). It stays the v1
        # shape for a v2 payment too, as every 402 does until PAYMENT-REQUIRED
        # is sent (#608); what follows the request's version is the
        # facilitator call above (facilitator_requirements, #607).
        if hasattr(requirements, "model_dump"):
            detail.update({
                "x402Version": 1,
                "error": message,
                "accepts": [requirements.model_dump(by_alias=True)],
            })
        raise HTTPException(status_code=402, detail=detail)

    request.state.x402_settlement = result
    # The rest of this request's Bee work waits for a slot rather than being
    # refused: refusing it now would only start a refund (#588).
    from app.services.bee_gate import mark_committed
    mark_committed()
    # From here on a retry with the same Idempotency-Key must not pay again,
    # whatever happens to this request (#359).
    record_settlement(request, result)
    tx = getattr(result, "transaction", None)
    logger.info(f"x402: payment settled before delivery, tx={tx}")
    log_payment_settled(client_ip=client_ip, payer=payer, transaction_hash=tx,
                        network=to_v1(settings.X402_NETWORK), success=True, nonce=request_nonce(request),
                        x402_version=paid_version(request))
    x402_settlements_total.labels(result="settled", version=paid_version_label(request)).inc()
    # Last: the transfer happened once, by this attempt, so the write-ahead
    # entry goes. Records above come first, so a cancellation while this is
    # written loses none of them (the shielded write still finishes).
    await outcome_unknown.aresolve(auth_key, prior, settled=True, gen=gen, release=True)
