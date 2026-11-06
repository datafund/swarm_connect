# app/x402/idempotency.py
"""
Idempotency-Key for paid requests: a retry returns the first result, unpaid (#359).

A paid stamp purchase can outlast a client's timeout (Bee buys on-chain, then
the payment settles). The client reports failure, the user retries with a fresh
payment, and pays twice for two batches. With an `Idempotency-Key` header the
retry is answered from the stored result of the first request instead.

How a retry is matched, and why in this order:

- The key is scoped to (payer, method, path, key). A retry carries a NEW
  X-PAYMENT (new nonce), so the payer is the only identity that survives it.
- The `from` field of an authorization can be written by anyone. So the lookup
  happens only after the facilitator has VERIFIED the new payment, which checks
  that its signature is valid for that payer, and after the replay guard has
  reserved it, so an authorization already in use cannot be presented. The
  authorization that paid for the original is also refused by hash: verify does
  not reject a spent nonce, and the guard forgets after an hour or a restart.
  The new payment is never settled: no second charge.
- The request itself must be the same: a hash of the query string and body is
  stored, and the same key with a different request is refused (422) rather
  than answered with a result for something else.
- While the first request is still running, a retry gets 409 rather than
  racing it to a second settlement.
- Once the first request's payment has SETTLED, the key is never released
  again. A "settled, outcome pending" entry is persisted at settlement; a 2xx
  result replaces it with the stored response. If the request fails after
  settlement, raises, is cancelled, or the gateway restarts mid-request, the
  entry stays and a retry gets 409 naming the transaction, not a second charge.
- The key is taken from the moment settlement is REQUESTED ("settling"). If
  the facilitator raises, times out or the request is cancelled while waiting,
  the outcome is unknown and a retry gets 409 naming the authorization to
  check on-chain. Only a definite refusal from the facilitator, or a failure
  before settlement, releases the key (a retry is then a new, paid attempt).
- A result too large to store is recorded as delivered, so a retry is told
  the first request succeeded rather than that it needs a refund.
- A /chunks/credit result is stored without its bearer token; a replay fills
  in the account's current token.
- The x402 version (#607, D4 in #596). The key does not depend on it, so a
  retry in the other version's header is still found (and not charged
  again). Entries of a v2 request record "version": 2 (no field: v1). A
  replay returns the stored receipt header, which is the original version's:
  X-PAYMENT-RESPONSE or PAYMENT-RESPONSE, never both. A completed result
  asked for in the other version's header is not served: 409
  IDEMPOTENCY_KEY_VERSION_MISMATCH, unpaid, telling the client to retry in
  the original header. Serving it would put a receipt in a header the
  client did not pay in (or make up one the facilitator never issued);
  "different request, use a new key" (422) would lead to a second paid
  purchase; a 402 invites paying again.

Entries live 24 hours, are persisted, and are capped at
X402_IDEMPOTENCY_MAX_ENTRIES (completed entries are evicted, oldest first,
before pending ones). The whole file is rewritten on each change, which is
fine at that size. An unreadable state file is kept as it is and the feature
fails closed: keyed paid requests get 503 until it is repaired, because
running with an empty store would charge every retry again. Unkeyed requests
are unaffected.

In-flight markers are in memory only. This assumes ONE gateway process (the
Dockerfile runs uvicorn without --workers). With several workers, two retries
could each claim a key in different processes.
"""
import base64
import glob
import hashlib
import json
import logging
import os
import secrets
import shutil
import asyncio
import time
from datetime import datetime, timezone
from threading import Lock, RLock
from typing import Dict, Optional, Tuple

from fastapi import HTTPException, Request
from starlette.datastructures import UploadFile
from starlette.responses import Response

from app.core.atomic_io import atomic_write_json
from app.core.io_lane import IOLane
from app.core.config import settings
from app.core.support import notice_sentence, report_advice, report_nonce_clause, support_contact
from app.x402.networks import to_v1

logger = logging.getLogger(__name__)

# While the state file cannot be written, retry (or probe) at most this often.
WRITE_RETRY_SECONDS = 5.0

# The store's own I/O lane (#569): its writes are whole-file rewrites, which
# can be large, and a hang here must refuse only keyed requests, not delay
# or trip the settlement-unknown registry's lane.
_idempotency_io = IOLane(2, "x402-idempotency-state")


def shutdown_idempotency_io(timeout: float = 5.0) -> None:
    """At gateway shutdown: wait, bounded, for pending idempotency writes."""
    _idempotency_io.shutdown(timeout)

IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"

TTL_SECONDS = 24 * 3600
# Longer than any paid request can run. Only matters if a request ends without
# the middleware seeing it end, which would otherwise block the key for 24 h.
IN_FLIGHT_TIMEOUT_SECONDS = 15 * 60
MAX_KEY_LENGTH = 255
# Paid routes answer with small JSON bodies. Anything larger is not stored, so
# the state file cannot be grown by one request.
MAX_STORED_BODY_BYTES = 64 * 1024
# What a replay needs to look like the original: its body type and the
# settlement headers of the payment that paid for it.
# PAYMENT-RESPONSE is the v2 receipt (#607); a response carries only the one
# of its version, so a replay sends exactly that one.
_STORED_HEADERS = ("content-type", "x-payment-response", "x-payment-mode", "x-payment-transaction",
                   "payment-response")

# Entry states. SETTLING: the facilitator was asked to settle and has not
# answered (or the request died waiting), so the payment may have been taken.
# SETTLED: paid, result not stored yet (or never, if the request then failed).
# DONE: paid and the response is stored. DELIVERED: paid and delivered, but the
# response was too large to store.
SETTLING = "settling"
SETTLED = "settled"
DONE = "done"
DELIVERED = "delivered_not_stored"
_STATES = (SETTLING, SETTLED, DONE, DELIVERED)
# A /chunks/credit response carries the account's bearer token. It is not
# written to disk, and a replay gets the account's CURRENT token instead (it
# may have been rotated since).
_CREDIT_TOKEN = "credit_token"


class IdempotentReplay(Exception):
    """Raised by the dependency to answer with a stored response.

    A dependency cannot return a response itself; the x402 middleware catches
    this and returns .response.
    """

    def __init__(self, response: Response):
        super().__init__("idempotent replay")
        self.response = response


def _stored_transaction(entry: dict) -> Optional[str]:
    """An entry's transaction, only if it is a valid hash (#607). Entries hold
    only checked ones (settlement.checked_settlement); this also covers an
    entry stored before that check existed."""
    from app.x402.settlement import valid_transaction
    return valid_transaction(entry.get("transaction"))


def _stored_version(entry: dict) -> Optional[int]:
    """The x402 version an entry records: 1 when absent, 2 only as the int 2.
    Any other value (a string, a list, None, 3, a bool, a float) means the
    entry is damaged: None, answered like an unreadable stored response."""
    if "version" not in entry:
        return 1
    value = entry["version"]
    return 2 if type(value) is int and value == 2 else None


def _sha(value) -> str:
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def entry_id(payer: str, method: str, path: str, key: str) -> str:
    # Hashed so the state file holds no raw keys or addresses.
    return _sha([payer.lower(), method.upper(), path.rstrip("/"), key])


def auth_hash(auth_key: Tuple[str, str]) -> str:
    return _sha([auth_key[0].lower(), auth_key[1].lower()])


class IdempotencyStore:
    """Settled and completed paid requests by entry id, plus those still running."""

    def __init__(self, state_file: Optional[str] = None):
        # entry id -> x402 version of the request holding it, while 2 (memory only).
        self._versions: Dict[str, int] = {}
        # `_lock` guards memory only and is never held across disk I/O, so
        # begin() and peek() on the event loop never wait for a write. Writes
        # snapshot under `_lock`, then write under `_write_lock`; a sequence
        # number keeps an older snapshot from overwriting a newer one.
        self._lock = RLock()
        self._write_lock = Lock()
        self._state_file = state_file
        self._entries: Dict[str, dict] = {}
        # entry id -> (request hash, marker expiry, token of the request holding it)
        self._pending: Dict[str, Tuple[str, float, str]] = {}
        # Final results that arrived before their interim result was stored.
        self._resolved: Dict[str, Tuple[int, bytes]] = {}
        # Why the state file could not be read (never written then), why the
        # last write failed, or that a write did not finish in time. Any of
        # them makes `unavailable` true: keyed paid requests are refused.
        self._unreadable: Optional[str] = None
        self._write_error: Optional[str] = None
        self._stalled: Optional[str] = None
        self._seq = 0
        self._written_seq = 0
        self._last_write_attempt = 0.0
        self._retrying = False
        self._queued_write = None     # the queued, not yet started write (coalescing)
        self._load()

    @property
    def unavailable(self) -> Optional[str]:
        """Why keyed paid requests must be refused, or None. Never writes on the
        caller's thread: while a write failed or stalled, a retry (or probe) runs
        in the background at most every WRITE_RETRY_SECONDS."""
        if self._unreadable:
            return self._unreadable
        if self._write_error or self._stalled:
            self._retry_in_background()
            return self._write_error or self._stalled
        return None

    @unavailable.setter
    def unavailable(self, value: Optional[str]) -> None:
        self._unreadable = value

    def state_unreadable(self) -> bool:
        """Whether the state file could not be read at startup (#468). Fixed
        until a restart: the file is read only then."""
        return bool(self._unreadable)

    def write_failed(self) -> bool:
        """Whether the last write failed and keyed paid requests are refused
        until a write of the latest state succeeds (#585). Reads the flag only."""
        return bool(self._write_error)

    def write_stalled(self) -> bool:
        """Whether a write did not finish in time and keyed paid requests are
        refused until a write completes (#585). Reads the flag only."""
        return bool(self._stalled)

    def public_cause(self) -> Optional[str]:
        """Short and safe to publish (/health): base name and unreadable,
        unwritable or unresponsive. Details stay in the log."""
        cause = ("unreadable" if self._unreadable else "unwritable" if self._write_error
                 else "unresponsive" if self.unavailable else None)
        return f"{os.path.basename(self._path())} is {cause}" if cause else None

    def _path(self) -> str:
        return self._state_file or settings.X402_IDEMPOTENCY_STATE_FILE

    def _load(self) -> None:
        path = self._path()
        try:
            with open(path) as f:
                data = json.load(f)
            entries = data.get("entries") if isinstance(data, dict) else None
            if not isinstance(entries, dict):
                raise ValueError("expected {\"entries\": {...}}")
            for k, v in entries.items():
                if (not isinstance(v, dict) or not isinstance(v.get("request_hash"), str)
                        or v.get("state") not in _STATES):
                    raise ValueError(f"malformed entry {k[:12]}")
        except FileNotFoundError:
            return
        except Exception as e:
            # Fail closed, and never overwrite the file: an empty store would
            # charge every retry in the window again. Same semantics as
            # load_json_state/unreadable_state for the other state stores.
            self.unavailable = f"{type(e).__name__}: {e}"
            logger.error("Idempotency state file %s is unreadable (%s); %s. Keyed paid requests "
                         "are refused until it is repaired or moved away.",
                         path, self.unavailable, _keep_copy(path))
            return
        now = time.time()
        self._entries = {k: v for k, v in entries.items()
                         if isinstance(v, dict) and v.get("expires", 0) > now}

    # --- persistence ----------------------------------------------------------

    def _snapshot(self) -> Tuple[int, Dict[str, dict]]:
        with self._lock:
            self._seq += 1
            return self._seq, {k: dict(v) for k, v in self._entries.items()}

    def _write(self, seq: int, snapshot: Dict[str, dict]) -> bool:
        """Write `snapshot` (number `seq`) unless a newer one is on disk already;
        True if its state is on disk. No `_lock` held during the I/O."""
        if self._unreadable:
            return False    # never overwrite the only copy
        with self._write_lock:
            if seq <= self._written_seq:
                return True
            self._last_write_attempt = time.monotonic()
            try:
                atomic_write_json(self._path(), {"entries": snapshot})
            except Exception as e:
                with self._lock:
                    self._stalled = None
                    if not self._write_error:
                        logger.error("Could not persist idempotency state (%s); keyed paid requests "
                                     "get 503 IDEMPOTENCY_UNAVAILABLE until a write succeeds", e)
                    self._write_error = f"{type(e).__name__}: {e}"
                return False
            self._written_seq = seq
            with self._lock:
                if self._stalled:
                    logger.warning("Idempotency state written again after a stall")
                    self._stalled = None
                if seq == self._seq and self._write_error:
                    logger.warning("Idempotency state written again; keyed paid requests resume")
                    self._write_error = None
            return True

    def _persist(self) -> bool:
        return self._write(*self._snapshot())

    def _after_change(self, persist: str) -> None:
        """persist: "sync" writes now on this thread (not for the event loop),
        "background" queues the write on the registry I/O pool, "none" leaves it
        to the caller (apersist)."""
        if persist == "sync":
            self._persist()
        elif persist == "background":
            self.schedule_persist()

    def schedule_persist(self):
        """Queue a write of the current state on the store's lane. Coalesced: while
        a queued write has not started, it is returned instead of a new one; it
        snapshots when it starts, so it includes every change made before then.
        A burst of changes during a long write therefore costs one more write."""
        with self._lock:
            if self._queued_write is not None:
                return self._queued_write
            job = {}
            fut = _idempotency_io.submit(self._run_queued_write, job)
            job["future"] = fut
            self._queued_write = fut
            return fut

    def _run_queued_write(self, job: dict) -> bool:
        # schedule_persist holds `_lock` until job["future"] is set, so it is
        # set by the time this acquires it.
        with self._lock:
            # Started: later changes need a write of their own.
            if self._queued_write is job.get("future"):
                self._queued_write = None
        return self._persist()

    async def apersist(self, timeout: float) -> bool:
        """Write the current state off the event loop; wait at most `timeout`.
        A write that takes longer marks the store stalled (unavailable) until a
        write completes; it is not interrupted."""
        inner = asyncio.wrap_future(self.schedule_persist())
        try:
            return await asyncio.wait_for(asyncio.shield(inner), timeout=timeout)
        except asyncio.TimeoutError:
            self.mark_stalled(f"write did not finish in {timeout}s")
            return False

    def mark_stalled(self, why: str) -> None:
        with self._lock:
            if not self._stalled:
                logger.error("Idempotency state disk I/O is not responding (%s); keyed paid requests "
                             "get 503 until a write completes", why)
            self._stalled = why

    def _retry_in_background(self) -> None:
        if self._stalled and not self._write_error and _idempotency_io.backlog():
            return      # this store's pending writes clear the stall when they complete
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

        _idempotency_io.submit(run)

    def _save(self) -> None:
        """Kept for callers outside this module: writes now, on this thread."""
        self._persist()

    def _prune(self, now: float) -> bool:
        expired = [k for k, v in self._entries.items() if v.get("expires", 0) <= now]
        for k in expired:
            del self._entries[k]
        for k in [k for k, p in self._pending.items() if p[1] <= now]:
            del self._pending[k]
        # A final result whose interim one was never stored has nothing to replace.
        for k in [k for k in self._resolved if k not in self._pending]:
            del self._resolved[k]
        # A version marker belongs to a pending claim only (#607).
        for k in [k for k in self._versions if k not in self._pending]:
            del self._versions[k]
        return bool(expired)

    def _enforce_cap(self) -> None:
        excess = len(self._entries) - max(1, settings.X402_IDEMPOTENCY_MAX_ENTRIES)
        if excess <= 0:
            return
        # Completed entries first: losing one only means a late retry pays
        # again. A settled-pending entry is the record that a payment was taken.
        order = sorted(self._entries, key=lambda k: (self._entries[k].get("state") != DONE,
                                                     self._entries[k].get("created", 0)))
        for k in order[:excess]:
            del self._entries[k]
        logger.warning("Idempotency store over X402_IDEMPOTENCY_MAX_ENTRIES; evicted %d oldest", excess)

    def begin(self, eid: str, request_hash: str, auth: str,
              version: int = 1) -> Tuple[str, Optional[dict], Optional[str]]:
        """Claim eid for a new request, or say why not.

        Returns (state, entry, token). state is one of "new" (token set),
        "in_progress", "mismatch", "original_auth", "unavailable", or the
        state of the stored entry (DONE, SETTLED, SETTLING, DELIVERED).
        """
        if self.unavailable:
            return "unavailable", None, None
        now = time.time()
        pruned = False
        with self._lock:
            pruned = self._prune(now)
            result = self._begin_locked(eid, request_hash, auth, now, version)
        if pruned:
            self._after_change("background")    # never on the caller's thread
        return result

    def _begin_locked(self, eid: str, request_hash: str, auth: str, now: float, version: int = 1):
        pending = self._pending.get(eid)
        if pending is not None:
            return ("in_progress" if pending[0] == request_hash else "mismatch"), None, None
        entry = self._entries.get(eid)
        if entry is not None:
            if entry["request_hash"] != request_hash:
                return "mismatch", None, None
            if entry.get("auth") == auth:
                # The authorization that paid for the original. Unless its
                # settlement got no answer: then "sign a new payment" (a
                # 402 with accepts) could charge twice (#553).
                if entry.get("state") == SETTLING:
                    return SETTLING, entry, None
                return "original_auth", None, None
            if entry["state"] == DONE:
                stored = _stored_version(entry)
                if stored is None:
                    return "damaged", entry, None
                if stored != version:
                    return "version_mismatch", entry, None
            return entry["state"], entry, None
        token = secrets.token_hex(8)
        self._pending[eid] = (request_hash, now + IN_FLIGHT_TIMEOUT_SECONDS, token)
        # Set for this claim, or cleared, so no earlier claim's marker survives.
        if version == 2:
            self._versions[eid] = 2
        else:
            self._versions.pop(eid, None)
        return "new", None, token

    def _note_version(self, eid: str, previous: Optional[dict] = None) -> None:
        """Record the request's x402 version on its entry (under `_lock`): only
        when 2, so v1 entries are exactly as before (#607)."""
        if self._versions.get(eid) == 2 or _stored_version(previous or {}) == 2:
            self._entries[eid]["version"] = 2

    def peek(self, eid: str) -> Optional[dict]:
        """The stored entry for eid, if any and not expired. Changes nothing."""
        with self._lock:
            entry = self._entries.get(eid)
            if entry is None or entry.get("expires", 0) <= time.time():
                return None
            return dict(entry)

    def _owns(self, eid: str, token: str) -> bool:
        pending = self._pending.get(eid)
        if pending is not None:
            return pending[2] == token
        entry = self._entries.get(eid)
        return entry is not None and entry.get("token") == token

    def mark_settling(self, eid: str, token: str, auth: Optional[str], nonce: Optional[str],
                      persist: str = "sync") -> bool:
        """Record that this request's payment is being settled: outcome unknown until
        it answers. True if this request holds the key (and the entry was set)."""
        with self._lock:
            pending = self._pending.get(eid)
            if pending is None or pending[2] != token:
                return False
            now = time.time()
            self._entries[eid] = {
                "state": SETTLING, "request_hash": pending[0], "auth": auth, "nonce": nonce,
                "token": token, "created": now, "expires": now + TTL_SECONDS,
            }
            self._note_version(eid)
            self._enforce_cap()
        self._after_change(persist)
        return True

    def clear_settling(self, eid: str, token: str, persist: str = "sync") -> None:
        """The facilitator refused the payment: nothing was taken, the key can be freed."""
        with self._lock:
            entry = self._entries.get(eid)
            if not (entry is not None and entry.get("state") == SETTLING and entry.get("token") == token):
                return
            del self._entries[eid]
        self._after_change(persist)

    def mark_settled(self, eid: str, token: str, auth: Optional[str], tx: Optional[str],
                     persist: str = "sync") -> None:
        """Record that this request's payment settled; from now on the key stays taken."""
        with self._lock:
            pending = self._pending.get(eid)
            if pending is None or pending[2] != token:
                return
            now = time.time()
            self._entries[eid] = {
                "state": SETTLED, "request_hash": pending[0], "auth": auth, "token": token,
                "transaction": tx, "created": now, "expires": now + TTL_SECONDS,
            }
            self._note_version(eid)
            self._enforce_cap()
        self._after_change(persist)

    def complete(self, eid: str, token: str, response: Response, body: Optional[bytes] = None,
                 fill: Optional[str] = None, storable: bool = True, persist: str = "sync") -> None:
        """Store a successful response for eid (body, if given, in place of response.body)."""
        if self._complete_locked(eid, token, response, body, fill, storable):
            self._after_change(persist)

    def _complete_locked(self, eid, token, response, body, fill, storable) -> bool:
        """The change for complete(); True if anything is to be written."""
        with self._lock:
            if not self._owns(eid, token):
                return False
            pending = self._pending.pop(eid, None)
            entry = self._entries.get(eid) or {}
            request_hash = pending[0] if pending else entry.get("request_hash")
            status_code = response.status_code
            if body is None:
                body = getattr(response, "body", None)
            if eid in self._resolved:
                status_code, body = self._resolved.pop(eid)
                storable = True
            if not storable or body is None or len(body) > MAX_STORED_BODY_BYTES:
                # Delivered and paid once: the key stays taken, and a retry is
                # told that it succeeded rather than sent to ask for a refund.
                logger.warning("x402: %s result not stored for its Idempotency-Key (%s); a retry "
                               "will not get it back", response.status_code,
                               "not storable" if not storable else "no body" if body is None
                               else f"{len(body)} bytes over the cap")
                if entry:
                    entry["state"] = DELIVERED
                    return True
                return False
            now = time.time()
            self._entries[eid] = {
                "state": DONE, "request_hash": request_hash, "auth": entry.get("auth"), "token": token,
                "transaction": entry.get("transaction"), "created": entry.get("created", now),
                "expires": now + TTL_SECONDS,
                "status": status_code,
                "headers": {k: v for k, v in response.headers.items() if k.lower() in _STORED_HEADERS},
                "body": base64.b64encode(body).decode("ascii"),
                "fill": fill,
            }
            self._note_version(eid, entry)
            self._versions.pop(eid, None)
            self._enforce_cap()
            return True

    def resolve(self, eid: str, token: str, status: int, body: bytes, persist: str = "sync") -> None:
        """Replace a stored interim result (a 202) with the final one.

        If the request is still running, its interim result is replaced when
        it is stored. If it ended without storing one (the caller disconnected
        before the 202), the paid entry is finalised with this result, so a
        retry gets the outcome rather than SETTLED_PENDING.
        """
        with self._lock:
            if not self._owns(eid, token):
                return
            entry = self._entries.get(eid)
            if eid in self._pending or entry is None:
                self._resolved[eid] = (status, body)
                return
            self._resolve_entry_locked(entry, status, body)
        self._after_change(persist)

    def _resolve_entry_locked(self, entry: dict, status: int, body: bytes) -> None:
        if entry.get("state") != DONE:
            entry["state"] = DONE
            entry["headers"] = {"content-type": "application/json"}
            if _stored_transaction(entry):
                entry["headers"]["x-payment-transaction"] = _stored_transaction(entry)
        entry["status"] = status
        entry["body"] = base64.b64encode(body).decode("ascii")

    def abandon(self, eid: Optional[str], token: Optional[str]) -> None:
        """End a request that did not succeed.

        Frees the key only if nothing was settled; a settled entry stays.
        """
        if eid is None:
            return
        with self._lock:
            pending = self._pending.get(eid)
            if pending is not None and pending[2] == token:
                del self._pending[eid]
                self._resolved.pop(eid, None)
                self._versions.pop(eid, None)

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()
            self._pending.clear()
            self._resolved.clear()
            self._versions.clear()
            self._unreadable = None
            self._write_error = None
            self._stalled = None
            self._queued_write = None
            self._last_write_attempt = 0.0


def _keep_copy(path: str) -> str:
    """Copy an unreadable state file aside once (not on every restart)."""
    try:
        with open(path, "rb") as f:
            content = f.read()
        for existing in sorted(glob.glob(f"{glob.escape(path)}.corrupt-*")):
            with open(existing, "rb") as f:
                if f.read() == content:
                    return f"a copy already exists at {existing}"
        backup = f"{path}.corrupt-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        shutil.copy2(path, backup)
        return f"a copy was saved to {backup}"
    except Exception as e:
        return f"could not save a copy ({e})"


idempotency_store = IdempotencyStore()


async def _request_hash(request: Request) -> str:
    """Hash of what the request asks for: query string and body."""
    h = hashlib.sha256()
    h.update(request.url.query.encode())
    h.update(b"\0")
    ctype = request.headers.get("content-type", "")
    if ctype.startswith(("multipart/form-data", "application/x-www-form-urlencoded")):
        # FastAPI has already parsed the form, which consumes the raw body but
        # caches the form. Hash its fields and file contents instead.
        form = await request.form()
        for name, value in form.multi_items():
            h.update(name.encode() + b"\0")
            if isinstance(value, UploadFile):
                h.update((value.filename or "").encode() + b"\0")
                while chunk := await value.read(1 << 20):
                    h.update(chunk)
                await value.seek(0)
            else:
                h.update(str(value).encode())
            h.update(b"\0")
    else:
        h.update(await request.body())
    return h.hexdigest()


def _safe_header_value(value) -> bool:
    """Printable ASCII only: no control character (CR, LF, NUL), nothing a
    header cannot carry."""
    return isinstance(value, str) and all(" " <= c <= "~" for c in value)


def _replay_response(entry: dict, payer: str) -> Optional[Response]:
    """The stored response, or None if the entry is damaged (#607 review).

    Stored headers are replayed only if their name is one the store keeps and
    their value is printable ASCII; X-Payment-Transaction only if it is a
    valid hash (settlement.valid_transaction), else "unknown". An entry
    written before those checks, or damaged on disk, can never make a replay
    raise or carry a CR/LF: a status, headers or body that cannot be used
    give None, and the caller answers "result not stored" instead.
    """
    from app.x402.settlement import valid_transaction
    stored = entry.get("headers")
    status = entry.get("status")
    if stored is not None and not isinstance(stored, dict):
        return None
    if type(status) is not int or not 200 <= status < 600:
        return None
    try:
        body = base64.b64decode(entry["body"], validate=True)
    except Exception:
        return None
    headers = {}
    for name, value in (stored or {}).items():
        if not isinstance(name, str) or name.lower() not in _STORED_HEADERS:
            continue
        if name.lower() == "x-payment-transaction":
            value = valid_transaction(value) or "unknown"
        elif not _safe_header_value(value):
            continue
        headers[name] = value
    headers[REPLAYED_HEADER] = "true"
    if entry.get("fill") == _CREDIT_TOKEN:
        # The account's current token, never a new or rotated one.
        from app.services.bandwidth_credit import bandwidth_credit_manager
        try:
            parsed = json.loads(body)
            parsed["token"] = bandwidth_credit_manager.issue_token(payer)
            body = json.dumps(parsed).encode()
        except Exception:
            return None
    try:
        return Response(content=body, status_code=status, headers=headers)
    except Exception:
        return None


def _log_replay(request: Request, payer: str, entry: dict, retry_nonce: Optional[str] = None) -> None:
    # A verify with no settle: reconciliation needs to see why.
    from app.core.client_ip import get_client_ip
    from app.x402.audit import AuditEventType, log_audit_event
    log_audit_event(
        event_type=AuditEventType.PAYMENT_IDEMPOTENT_REPLAY,
        data={"method": request.method, "path": request.url.path,
              "transaction_hash": _stored_transaction(entry), "network": to_v1(settings.X402_NETWORK),
              # The retry's own authorization: verified, never settled.
              "nonce": retry_nonce},
        client_ip=get_client_ip(request),
        wallet_address=payer,
    )


def _settlement_unknown_409(entry: dict) -> HTTPException:
    """The answer for a key whose first request's settlement got no answer."""
    return HTTPException(status_code=409, detail={
        "code": "IDEMPOTENCY_KEY_SETTLEMENT_UNKNOWN",
        "message": ("The payment for the first request with this Idempotency-Key was sent for "
                    "settlement but no answer came back, so it may or may not have been "
                    "collected. This request was not charged, and this key stays taken. "
                    "Do not pay again from this response. Check on-chain whether the "
                    f"original authorization (nonce below) was used. If it was, {report_nonce_clause()}. "
                    "If it was not, the facilitator may still submit "
                    "it, so wait until its validBefore has passed, or cancel it on-chain with "
                    "EIP-3009 cancelAuthorization, then retry with a new payment and a new "
                    "Idempotency-Key." + notice_sentence()),
        "nonce": entry.get("nonce"),
        "x402_status": "settlement_unknown",
    })


def _damaged_entry_409(entry: dict) -> HTTPException:
    """A paid, completed entry whose stored response (or version) cannot be used."""
    logger.error("x402: stored result for an Idempotency-Key is damaged; answering not stored")
    tx = _stored_transaction(entry) or "unknown"
    return HTTPException(status_code=409, headers={"X-Payment-Transaction": tx}, detail={
        "code": "IDEMPOTENCY_KEY_DELIVERED_NOT_STORED",
        "message": ("The first request with this Idempotency-Key succeeded and was paid once, "
                    "but its stored response cannot be read. This payment was not charged. "
                    "Look the result up through the resource itself."),
        "transaction": tx,
        "x402_status": "delivered",
    })


async def raise_if_key_settlement_unknown(request: Request, payer: str) -> None:
    """Raise the key's settlement-unknown 409 if this request's Idempotency-Key has one.

    For a request refused before the key is looked up (its authorization is
    reserved because it settled under another key): the key's own answer is
    the 409, not a 402 "already used" that clients pay from (#553).
    """
    key = request.headers.get(IDEMPOTENCY_HEADER)
    if key is None or not (0 < len(key) <= MAX_KEY_LENGTH):
        return
    entry = idempotency_store.peek(entry_id(payer, request.method, request.url.path, key))
    if entry is not None and entry.get("state") == SETTLING \
            and entry.get("request_hash") == await _request_hash(request):
        raise _settlement_unknown_409(entry)


async def begin_idempotent_request(request: Request, payer: str,
                                   auth_key: Tuple[str, str], version: int = 1) -> Optional[Tuple[str, str]]:
    """Check the Idempotency-Key of a VERIFIED, reserved paid request.

    Returns (entry id, token) to settle, complete or abandon the request with,
    or None when no key was sent. Raises IdempotentReplay with the stored
    response for a repeat, and HTTPException for a key that is malformed, in
    use, reused for a different request, or cannot be checked.

    Call only after the facilitator has verified the payment and the replay
    guard has reserved it: that is what makes `payer` an identity rather than a
    claim, and the authorization an unused one.
    """
    key = request.headers.get(IDEMPOTENCY_HEADER)
    if key is None:
        return None
    if not (0 < len(key) <= MAX_KEY_LENGTH) or not key.isascii() or not key.isprintable():
        raise HTTPException(status_code=400, detail={
            "code": "IDEMPOTENCY_KEY_INVALID",
            "message": f"{IDEMPOTENCY_HEADER} must be 1-{MAX_KEY_LENGTH} printable ASCII characters.",
        })
    eid = entry_id(payer, request.method, request.url.path, key)
    state, entry, token = idempotency_store.begin(eid, await _request_hash(request), auth_hash(auth_key),
                                                  version)
    if state == "new":
        return eid, token
    if state == DONE:
        logger.info(f"x402: idempotent replay for payer {payer} on {request.url.path}; new payment not settled")
        _log_replay(request, payer, entry, auth_key[1])
        replay = _replay_response(entry, payer)
        if replay is not None:
            raise IdempotentReplay(replay)
        raise _damaged_entry_409(entry)
    if state == "damaged":
        raise _damaged_entry_409(entry)
    if state == "version_mismatch":
        from app.x402.payment_headers import version_enabled
        original = _stored_version(entry)
        header = "X-PAYMENT" if original == 1 else "PAYMENT-SIGNATURE"
        raise HTTPException(status_code=409, detail={
            "code": "IDEMPOTENCY_KEY_VERSION_MISMATCH",
            "message": (f"The first request with this Idempotency-Key was paid once, with x402 "
                        f"v{original}, and its result is stored. This request uses x402 "
                        f"v{version}, so it is not answered with that result. This payment was "
                        f"not charged. "
                        + (f"To get the result, retry with the same key and a newly signed x402 "
                           f"v{original} payment in the {header} header."
                           if version_enabled(original) else
                           f"This gateway no longer accepts x402 v{original} payments, so the "
                           f"stored result cannot be fetched with this key. Do not pay again "
                           f"for it: look the result up through the resource itself, or "
                           + (f"report it to {support_contact()}."
                              if support_contact() else "contact the operator."))),
            "original_x402_version": original,
            "original_x402_version_accepted": version_enabled(original),
        })
    if state == "unavailable":
        raise HTTPException(status_code=503, detail={
            "code": "IDEMPOTENCY_UNAVAILABLE",
            "message": ("Idempotency-Key requests cannot be processed right now; this payment "
                        "was not charged. Retry later with the same key."),
        })
    if state == "in_progress":
        raise HTTPException(status_code=409, headers={"Retry-After": "5"}, detail={
            "code": "IDEMPOTENCY_KEY_IN_PROGRESS",
            "message": ("A request with this Idempotency-Key is still being processed. "
                        "Retry with the same key shortly; this payment was not charged."),
        })
    if state == SETTLING:
        raise _settlement_unknown_409(entry)
    if state == DELIVERED:
        tx = _stored_transaction(entry) or "unknown"
        raise HTTPException(status_code=409, headers={"X-Payment-Transaction": tx}, detail={
            "code": "IDEMPOTENCY_KEY_DELIVERED_NOT_STORED",
            "message": ("The first request with this Idempotency-Key succeeded and was paid once, "
                        "but its response was too large to keep for a retry. This payment was not "
                        "charged. Look the result up through the resource itself."),
            "transaction": tx,
            "x402_status": "delivered",
        })
    if state == SETTLED:
        tx = _stored_transaction(entry) or "unknown"
        raise HTTPException(status_code=409, headers={"X-Payment-Transaction": tx}, detail={
            "code": "IDEMPOTENCY_KEY_SETTLED_PENDING",
            "message": ("The first request with this Idempotency-Key was paid, but its result is "
                        "not available (it failed or was interrupted after payment). This payment "
                        "was not charged. " + report_advice("transaction")),
            "transaction": tx,
            "x402_status": "settled_not_delivered",
        })
    if state == "original_auth":
        requirements = getattr(request.state, "x402_requirements", None)
        raise HTTPException(status_code=402, detail={
            "x402Version": 1,
            "error": "This payment authorization has already been used. Sign a new payment.",
            "accepts": [requirements.model_dump(by_alias=True)] if hasattr(requirements, "model_dump") else [],
        })
    raise HTTPException(status_code=422, detail={
        "code": "IDEMPOTENCY_KEY_REUSED",
        "message": ("This Idempotency-Key was already used for a different request. "
                    "Use a new key for a new request; this payment was not charged."),
    })


def record_settling(request: Request) -> None:
    """The request's payment is about to be settled: from now on the key stays
    taken unless the facilitator definitely refuses it. Blocking (writes on
    this thread); the request path uses arecord_settling."""
    idem = getattr(request.state, "x402_idempotency_id", None)
    if idem is None:
        return
    auth_key = getattr(request.state, "x402_auth_key", None)
    idempotency_store.mark_settling(idem[0], idem[1], auth_hash(auth_key) if auth_key else None,
                                    auth_key[1] if auth_key else None)


async def arecord_settling(request: Request) -> bool:
    """record_settling off the event loop, bounded, fail-closed: True when the
    "settling" entry is on disk (or the request has no key). False when it
    could not be written within X402_SETTLEMENT_STATE_WRITE_TIMEOUT_SECONDS,
    or the store is unavailable: the caller must not settle (503), like a
    failed write-ahead. The entry is then taken back, so a retry with the key
    is not told the outcome is unknown."""
    idem = getattr(request.state, "x402_idempotency_id", None)
    if idem is None:
        return True
    timeout = settings.X402_SETTLEMENT_STATE_WRITE_TIMEOUT_SECONDS
    if idempotency_store.unavailable:
        return False
    if _idempotency_io.oldest_age() > timeout:
        idempotency_store.mark_stalled(f"a state write has been pending for over {timeout}s")
        return False
    auth_key = getattr(request.state, "x402_auth_key", None)
    if not idempotency_store.mark_settling(idem[0], idem[1], auth_hash(auth_key) if auth_key else None,
                                           auth_key[1] if auth_key else None, persist="none"):
        return True     # the key is no longer this request's: nothing to record
    try:
        written = await idempotency_store.apersist(timeout)
    except BaseException:
        idempotency_store.clear_settling(idem[0], idem[1], persist="background")
        raise
    if not written:
        idempotency_store.clear_settling(idem[0], idem[1], persist="background")
    return written


def settlement_refused(request: Request) -> None:
    """The facilitator refused the payment (a definite answer): nothing was taken."""
    idem = getattr(request.state, "x402_idempotency_id", None)
    if idem is not None:
        idempotency_store.clear_settling(idem[0], idem[1], persist="background")


def record_settlement(request: Request, settlement) -> None:
    """The request's payment settled: keep its key taken from now on."""
    idem = getattr(request.state, "x402_idempotency_id", None)
    if idem is None:
        return
    auth_key = getattr(request.state, "x402_auth_key", None)
    idempotency_store.mark_settled(idem[0], idem[1], auth_hash(auth_key) if auth_key else None,
                                   getattr(settlement, "transaction", None), persist="background")


def resolve_idempotent_result(idem: Optional[Tuple[str, str]], status: int, body: bytes) -> None:
    """A request answered 202 has its final result: retries get that from now on."""
    if idem is not None:
        idempotency_store.resolve(idem[0], idem[1], status, body, persist="background")


def finish_idempotent_request(request: Request, response: Optional[Response]) -> None:
    """Store a 2xx response for the request's key; otherwise end the claim.

    Ending the claim frees the key only when nothing was settled.
    """
    idem = getattr(request.state, "x402_idempotency_id", None)
    if idem is None:
        return
    if response is not None and 200 <= response.status_code < 300:
        body, fill, storable = getattr(response, "body", None), None, True
        if request.url.path.rstrip("/").endswith("/chunks/credit") and body:
            try:
                parsed = json.loads(body)
                if isinstance(parsed, dict) and "token" in parsed:
                    parsed["token"] = None
                    body, fill = json.dumps(parsed).encode(), _CREDIT_TOKEN
            except ValueError:
                storable = False    # never stored with a secret in it
        idempotency_store.complete(idem[0], idem[1], response, body=body, fill=fill, storable=storable,
                                   persist="background")
    else:
        idempotency_store.abandon(idem[0], idem[1])
