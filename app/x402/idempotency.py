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
import secrets
import shutil
import time
from datetime import datetime, timezone
from threading import Lock
from typing import Dict, Optional, Tuple

from fastapi import HTTPException, Request
from starlette.datastructures import UploadFile
from starlette.responses import Response

from app.core.atomic_io import atomic_write_json
from app.core.config import settings

logger = logging.getLogger(__name__)

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
_STORED_HEADERS = ("content-type", "x-payment-response", "x-payment-mode", "x-payment-transaction")

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
        self._lock = Lock()
        self._state_file = state_file
        self._entries: Dict[str, dict] = {}
        # entry id -> (request hash, marker expiry, token of the request holding it)
        self._pending: Dict[str, Tuple[str, float, str]] = {}
        # Final results that arrived before their interim result was stored.
        self._resolved: Dict[str, Tuple[int, bytes]] = {}
        # Why the state file could not be read; None when it could.
        self.unavailable: Optional[str] = None
        self._load()

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

    def _save(self) -> None:
        if self.unavailable:
            return
        try:
            atomic_write_json(self._path(), {"entries": self._entries})
        except Exception as e:
            logger.error("Could not persist idempotency state: %s", e)

    def _prune(self, now: float) -> bool:
        expired = [k for k, v in self._entries.items() if v.get("expires", 0) <= now]
        for k in expired:
            del self._entries[k]
        for k in [k for k, p in self._pending.items() if p[1] <= now]:
            del self._pending[k]
        # A final result whose interim one was never stored has nothing to replace.
        for k in [k for k in self._resolved if k not in self._pending]:
            del self._resolved[k]
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

    def begin(self, eid: str, request_hash: str, auth: str) -> Tuple[str, Optional[dict], Optional[str]]:
        """Claim eid for a new request, or say why not.

        Returns (state, entry, token). state is one of "new" (token set),
        "in_progress", "mismatch", "original_auth", "unavailable", or the
        state of the stored entry (DONE, SETTLED, SETTLING, DELIVERED).
        """
        if self.unavailable:
            return "unavailable", None, None
        now = time.time()
        with self._lock:
            if self._prune(now):
                self._save()
            pending = self._pending.get(eid)
            if pending is not None:
                return ("in_progress" if pending[0] == request_hash else "mismatch"), None, None
            entry = self._entries.get(eid)
            if entry is not None:
                if entry["request_hash"] != request_hash:
                    return "mismatch", None, None
                if entry.get("auth") == auth:
                    return "original_auth", None, None
                return entry["state"], entry, None
            token = secrets.token_hex(8)
            self._pending[eid] = (request_hash, now + IN_FLIGHT_TIMEOUT_SECONDS, token)
            return "new", None, token

    def _owns(self, eid: str, token: str) -> bool:
        pending = self._pending.get(eid)
        if pending is not None:
            return pending[2] == token
        entry = self._entries.get(eid)
        return entry is not None and entry.get("token") == token

    def mark_settling(self, eid: str, token: str, auth: Optional[str], nonce: Optional[str]) -> None:
        """Persist that this request's payment is being settled: outcome unknown until it answers."""
        with self._lock:
            pending = self._pending.get(eid)
            if pending is None or pending[2] != token:
                return
            now = time.time()
            self._entries[eid] = {
                "state": SETTLING, "request_hash": pending[0], "auth": auth, "nonce": nonce,
                "token": token, "created": now, "expires": now + TTL_SECONDS,
            }
            self._enforce_cap()
            self._save()

    def clear_settling(self, eid: str, token: str) -> None:
        """The facilitator refused the payment: nothing was taken, the key can be freed."""
        with self._lock:
            entry = self._entries.get(eid)
            if entry is not None and entry.get("state") == SETTLING and entry.get("token") == token:
                del self._entries[eid]
                self._save()

    def mark_settled(self, eid: str, token: str, auth: Optional[str], tx: Optional[str]) -> None:
        """Persist that this request's payment settled; from now on the key stays taken."""
        with self._lock:
            pending = self._pending.get(eid)
            if pending is None or pending[2] != token:
                return
            now = time.time()
            self._entries[eid] = {
                "state": SETTLED, "request_hash": pending[0], "auth": auth, "token": token,
                "transaction": tx, "created": now, "expires": now + TTL_SECONDS,
            }
            self._enforce_cap()
            self._save()

    def complete(self, eid: str, token: str, response: Response, body: Optional[bytes] = None,
                 fill: Optional[str] = None, storable: bool = True) -> None:
        """Store a successful response for eid (body, if given, in place of response.body)."""
        with self._lock:
            if not self._owns(eid, token):
                return
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
                    self._save()
                return
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
            self._enforce_cap()
            self._save()

    def resolve(self, eid: str, token: str, status: int, body: bytes) -> None:
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
            if entry.get("state") != DONE:
                entry["state"] = DONE
                entry["headers"] = {"content-type": "application/json"}
                if entry.get("transaction"):
                    entry["headers"]["x-payment-transaction"] = entry["transaction"]
            entry["status"] = status
            entry["body"] = base64.b64encode(body).decode("ascii")
            self._save()

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

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()
            self._pending.clear()
            self._resolved.clear()
            self.unavailable = None


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


def _replay_response(entry: dict, payer: str) -> Response:
    headers = dict(entry.get("headers") or {})
    headers[REPLAYED_HEADER] = "true"
    body = base64.b64decode(entry["body"])
    if entry.get("fill") == _CREDIT_TOKEN:
        # The account's current token, never a new or rotated one.
        from app.services.bandwidth_credit import bandwidth_credit_manager
        parsed = json.loads(body)
        parsed["token"] = bandwidth_credit_manager.issue_token(payer)
        body = json.dumps(parsed).encode()
    return Response(content=body, status_code=entry["status"], headers=headers)


def _log_replay(request: Request, payer: str, entry: dict) -> None:
    # A verify with no settle: reconciliation needs to see why.
    from app.core.client_ip import get_client_ip
    from app.x402.audit import AuditEventType, log_audit_event
    log_audit_event(
        event_type=AuditEventType.PAYMENT_IDEMPOTENT_REPLAY,
        data={"method": request.method, "path": request.url.path,
              "transaction_hash": entry.get("transaction"), "network": settings.X402_NETWORK},
        client_ip=get_client_ip(request),
        wallet_address=payer,
    )


async def begin_idempotent_request(request: Request, payer: str,
                                   auth_key: Tuple[str, str]) -> Optional[Tuple[str, str]]:
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
    state, entry, token = idempotency_store.begin(eid, await _request_hash(request), auth_hash(auth_key))
    if state == "new":
        return eid, token
    if state == DONE:
        logger.info(f"x402: idempotent replay for payer {payer} on {request.url.path}; new payment not settled")
        _log_replay(request, payer, entry)
        raise IdempotentReplay(_replay_response(entry, payer))
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
        raise HTTPException(status_code=409, detail={
            "code": "IDEMPOTENCY_KEY_SETTLEMENT_UNKNOWN",
            "message": ("The payment for the first request with this Idempotency-Key was sent for "
                        "settlement but no answer came back, so it may or may not have been "
                        "collected. This payment was not charged. Before paying again, check "
                        "whether the original authorization (nonce below) was used on-chain; if "
                        "it was, contact the operator."),
            "nonce": entry.get("nonce"),
            "x402_status": "settlement_unknown",
        })
    if state == DELIVERED:
        tx = entry.get("transaction") or "unknown"
        raise HTTPException(status_code=409, headers={"X-Payment-Transaction": tx}, detail={
            "code": "IDEMPOTENCY_KEY_DELIVERED_NOT_STORED",
            "message": ("The first request with this Idempotency-Key succeeded and was paid once, "
                        "but its response was too large to keep for a retry. This payment was not "
                        "charged. Look the result up through the resource itself."),
            "transaction": tx,
            "x402_status": "delivered",
        })
    if state == SETTLED:
        tx = entry.get("transaction") or "unknown"
        raise HTTPException(status_code=409, headers={"X-Payment-Transaction": tx}, detail={
            "code": "IDEMPOTENCY_KEY_SETTLED_PENDING",
            "message": ("The first request with this Idempotency-Key was paid, but its result is "
                        "not available (it failed or was interrupted after payment). This payment "
                        "was not charged. Contact the operator with this transaction for the "
                        "result or a refund."),
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
    taken unless the facilitator definitely refuses it."""
    idem = getattr(request.state, "x402_idempotency_id", None)
    if idem is None:
        return
    auth_key = getattr(request.state, "x402_auth_key", None)
    idempotency_store.mark_settling(idem[0], idem[1], auth_hash(auth_key) if auth_key else None,
                                    auth_key[1] if auth_key else None)


def settlement_refused(request: Request) -> None:
    """The facilitator refused the payment (a definite answer): nothing was taken."""
    idem = getattr(request.state, "x402_idempotency_id", None)
    if idem is not None:
        idempotency_store.clear_settling(idem[0], idem[1])


def record_settlement(request: Request, settlement) -> None:
    """The request's payment settled: keep its key taken from now on."""
    idem = getattr(request.state, "x402_idempotency_id", None)
    if idem is None:
        return
    auth_key = getattr(request.state, "x402_auth_key", None)
    idempotency_store.mark_settled(idem[0], idem[1], auth_hash(auth_key) if auth_key else None,
                                   getattr(settlement, "transaction", None))


def resolve_idempotent_result(idem: Optional[Tuple[str, str]], status: int, body: bytes) -> None:
    """A request answered 202 has its final result: retries get that from now on."""
    if idem is not None:
        idempotency_store.resolve(idem[0], idem[1], status, body)


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
        idempotency_store.complete(idem[0], idem[1], response, body=body, fill=fill, storable=storable)
    else:
        idempotency_store.abandon(idem[0], idem[1])
