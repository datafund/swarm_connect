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
  that its signature is valid for that payer. The new payment is never settled:
  no second charge, and its authorization stays unused.
- The request itself must be the same: a hash of the query string and body is
  stored, and the same key with a different request is refused (422) rather
  than answered with a result for something else.
- While the first request is still running, a retry gets 409 rather than
  racing it to a second settlement.
- Only 2xx results are stored. A failure is not cached, so a retry after one
  is a new attempt (and a new payment).

Stored results live 24 hours and are persisted, so a restart between the
timeout and the retry does not turn the retry into a second purchase.
In-flight markers are in memory only: a restart ends the request they guard.
"""
import base64
import hashlib
import json
import logging
import os
import time
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


class IdempotentReplay(Exception):
    """Raised by the dependency to answer with a stored response.

    A dependency cannot return a response itself; the x402 middleware catches
    this and returns .response.
    """

    def __init__(self, response: Response):
        super().__init__("idempotent replay")
        self.response = response


def entry_id(payer: str, method: str, path: str, key: str) -> str:
    # Hashed so the state file holds no raw keys or addresses.
    raw = json.dumps([payer.lower(), method.upper(), path.rstrip("/"), key])
    return hashlib.sha256(raw.encode()).hexdigest()


class IdempotencyStore:
    """Completed paid responses by entry id, plus the requests still running."""

    def __init__(self, state_file: Optional[str] = None):
        self._lock = Lock()
        self._state_file = state_file
        self._done: Dict[str, dict] = {}
        self._pending: Dict[str, Tuple[str, float]] = {}
        self._load()

    def _path(self) -> str:
        return self._state_file or settings.X402_IDEMPOTENCY_STATE_FILE

    def _load(self) -> None:
        try:
            path = self._path()
            if not os.path.exists(path):
                return
            with open(path) as f:
                data = json.load(f)
            now = time.time()
            self._done = {k: v for k, v in (data.get("entries") or {}).items()
                          if v.get("expires", 0) > now}
        except Exception as e:
            # Never fail startup over a cache.
            logger.warning("Could not load idempotency state: %s", e)

    def _save(self) -> None:
        try:
            atomic_write_json(self._path(), {"entries": self._done})
        except Exception as e:
            logger.warning("Could not persist idempotency state: %s", e)

    def _prune(self, now: float) -> bool:
        expired = [k for k, v in self._done.items() if v["expires"] <= now]
        for k in expired:
            del self._done[k]
        for k in [k for k, (_, until) in self._pending.items() if until <= now]:
            del self._pending[k]
        return bool(expired)

    def begin(self, eid: str, request_hash: str) -> Tuple[str, Optional[dict]]:
        """Claim eid for a new request, or say why not.

        Returns ("new", None), ("replay", entry), ("in_progress", None) or
        ("mismatch", None).
        """
        now = time.time()
        with self._lock:
            if self._prune(now):
                self._save()
            done = self._done.get(eid)
            if done is not None:
                return ("replay", done) if done["request_hash"] == request_hash else ("mismatch", None)
            pending = self._pending.get(eid)
            if pending is not None:
                return ("in_progress", None) if pending[0] == request_hash else ("mismatch", None)
            self._pending[eid] = (request_hash, now + IN_FLIGHT_TIMEOUT_SECONDS)
            return "new", None

    def complete(self, eid: str, response: Response) -> None:
        """Store a successful response for eid."""
        with self._lock:
            pending = self._pending.pop(eid, None)
            body = getattr(response, "body", None)
            if pending is None or body is None or len(body) > MAX_STORED_BODY_BYTES:
                return
            self._done[eid] = {
                "request_hash": pending[0],
                "expires": time.time() + TTL_SECONDS,
                "status": response.status_code,
                "headers": {k: v for k, v in response.headers.items() if k.lower() in _STORED_HEADERS},
                "body": base64.b64encode(body).decode("ascii"),
            }
            self._save()

    def abandon(self, eid: Optional[str]) -> None:
        """Forget a request that did not succeed, so the key can be retried."""
        if eid is None:
            return
        with self._lock:
            self._pending.pop(eid, None)

    def reset(self) -> None:
        with self._lock:
            self._done.clear()
            self._pending.clear()


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


def _replay_response(entry: dict) -> Response:
    headers = dict(entry.get("headers") or {})
    headers[REPLAYED_HEADER] = "true"
    return Response(content=base64.b64decode(entry["body"]), status_code=entry["status"], headers=headers)


async def begin_idempotent_request(request: Request, payer: str) -> Optional[str]:
    """Check the Idempotency-Key of a VERIFIED paid request.

    Returns the entry id to complete or abandon once the request ends, or None
    when no key was sent. Raises IdempotentReplay with the stored response for
    a repeat, and HTTPException for a key that is malformed, in use, or reused
    for a different request.

    Call only after the facilitator has verified the payment: that is what
    makes `payer` an identity rather than a claim.
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
    state, entry = idempotency_store.begin(eid, await _request_hash(request))
    if state == "replay":
        logger.info(f"x402: idempotent replay for payer {payer} on {request.url.path}; new payment not settled")
        raise IdempotentReplay(_replay_response(entry))
    if state == "in_progress":
        raise HTTPException(status_code=409, headers={"Retry-After": "5"}, detail={
            "code": "IDEMPOTENCY_KEY_IN_PROGRESS",
            "message": ("A request with this Idempotency-Key is still being processed. "
                        "Retry with the same key shortly; this payment was not charged."),
        })
    if state == "mismatch":
        raise HTTPException(status_code=422, detail={
            "code": "IDEMPOTENCY_KEY_REUSED",
            "message": ("This Idempotency-Key was already used for a different request. "
                        "Use a new key for a new request; this payment was not charged."),
        })
    return eid


def finish_idempotent_request(request: Request, response: Optional[Response]) -> None:
    """Store a 2xx response for the request's key; release the key otherwise."""
    eid = getattr(request.state, "x402_idempotency_id", None)
    if eid is None:
        return
    if response is not None and 200 <= response.status_code < 300:
        idempotency_store.complete(eid, response)
    else:
        idempotency_store.abandon(eid)
