# app/services/pool_allowance.py
"""Daily allowance of pooled batches, per calling origin.

The pool pre-buys postage batches so callers do not wait a minute for an
on-chain purchase, and pays to keep them alive. `POST /api/v1/pool/acquire` had
no gate at all, so anyone who could resolve the hostname took them: on the
staging gateway, 3,866 acquire calls in one day drove 40 replacement purchases.

An allowance keyed on `Origin` is the control that fits what is actually
knowable here. The main consumer is a static browser app with no backend and no
identity of its own — it borrows the visitor's wallet, so there is no single
address to allow-list, and no server-held key it could sign with.

## What Origin is and is not worth

A browser sets `Origin` itself and a page cannot forge another site's. So this
DOES stop another website spending your postage.

Anything that is not a browser can claim any origin it likes — `curl -H
"Origin: https://example.app"` is indistinguishable from the real thing. So this
does NOT authenticate.

The budget is therefore doing the protecting, and the origin only decides which
budget applies. A script forging the header consumes that origin's allowance and
no more. Treat this as attribution with a cap, not as authorisation: if you need
to know who is spending, require a payment or a signature instead.
"""
import json
import logging
import os
from datetime import datetime, timezone
from threading import Lock
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

from app.core.config import settings

logger = logging.getLogger(__name__)

# Sentinel for "no limit", so 0 can mean "deny" rather than being ambiguous.
UNLIMITED = -1


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# Every caller not named in the configuration shares this one bucket.
UNLISTED = "(unlisted)"


def _key(origin: Optional[str], size: str) -> str:
    """Budget key: one bucket per CONFIGURED origin, one shared bucket for the rest.

    Per size in both cases. Sizes differ in cost by powers of two — a depth-20
    batch costs eight times a depth-17 one — so a count shared across sizes would
    let a caller spend eight times its budget by asking for a larger batch.

    Unlisted origins share a single bucket rather than each receiving one. Giving
    every distinct Origin its own allowance makes the budget decorative: a caller
    sends a header nobody has ever seen, gets a fresh allowance, and repeats. The
    header is attacker-controlled, so the number of buckets would be unbounded
    and so would the spend.

    Only origins the operator has named in POOL_DAILY_ALLOWANCES get a bucket of
    their own, which is the point of naming them.
    """
    key = _normalise(origin)
    if key not in settings.get_pool_daily_allowances():
        key = UNLISTED
    return f"{key}|{size}"


def _normalise(origin: Optional[str]) -> str:
    """Reduce an Origin header to scheme://host, lowercased.

    Ports and trailing slashes are dropped so a configured entry matches whether
    the caller sends `https://app.example` or `https://app.example:443/`.
    Requests with no Origin — CLIs, SDKs, server-to-server — map to "" and draw
    on the default allowance.
    """
    if not origin:
        return ""
    try:
        p = urlparse(origin.strip())
        if not p.scheme or not p.hostname:
            return ""
        return f"{p.scheme.lower()}://{p.hostname.lower()}"
    except Exception:
        return ""


class PoolAllowanceTracker:
    """Counts batches handed out per origin per UTC day."""

    def __init__(self, state_file: Optional[str] = None):
        self._lock = Lock()
        self._state_file = state_file
        self._day = _today()
        self._used: Dict[str, int] = {}
        self._load()

    # --- persistence -------------------------------------------------------
    #
    # Persisted so a restart does not hand out a fresh allowance. Without this a
    # crash-looping gateway would give away a full budget per restart, which is
    # the shape of the incident this exists to prevent.

    def _path(self) -> str:
        return self._state_file or settings.POOL_ALLOWANCE_STATE_FILE

    def _load(self) -> None:
        try:
            path = self._path()
            if not os.path.exists(path):
                return
            with open(path) as f:
                data = json.load(f)
            if data.get("day") == self._day:
                self._used = {k: int(v) for k, v in (data.get("used") or {}).items()}
                logger.info("Loaded pool allowance state for %s: %s", self._day, self._used)
        except Exception as e:
            # Never fail startup over a counter. Worst case the allowance resets,
            # and the hourly purchase ceiling still bounds the damage.
            logger.warning("Could not load pool allowance state: %s", e)

    def _save(self) -> None:
        try:
            path = self._path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.tmp"
            with open(tmp, "w") as f:
                json.dump({"day": self._day, "used": self._used}, f)
            os.replace(tmp, path)
        except Exception as e:
            logger.warning("Could not persist pool allowance state: %s", e)

    # --- allowance ---------------------------------------------------------

    def _roll_day(self) -> None:
        today = _today()
        if today != self._day:
            logger.info("Pool allowance day rolled %s -> %s, resetting counters", self._day, today)
            self._day = today
            self._used = {}
            self._save()

    def allowance_for(self, origin: Optional[str]) -> int:
        """Configured daily allowance for an origin, or the default.

        The limit is per size, not shared across sizes — see _key().
        """
        origin_key = _normalise(origin)
        configured = settings.get_pool_daily_allowances()
        if origin_key in configured:
            return configured[origin_key]
        return settings.POOL_DEFAULT_DAILY_ALLOWANCE

    def check(self, origin: Optional[str], size: str = "small") -> Tuple[bool, dict]:
        """Whether this origin may take another batch, and the numbers behind it.

        Does not consume. Call `consume` only once a batch has actually been
        handed over, so a failed acquire does not spend someone's allowance.
        """
        key = _key(origin, size)
        limit = self.allowance_for(origin)
        with self._lock:
            self._roll_day()
            used = self._used.get(key, 0)

        is_named = _normalise(origin) in settings.get_pool_daily_allowances()
        info = {
            "origin": _normalise(origin) or "(none)",
            # Says which bucket was actually charged. An unlisted caller sharing
            # the common budget may find it already spent by someone else, and
            # that is far less confusing when the response says so.
            "bucket": _normalise(origin) if is_named else UNLISTED,
            "size": size,
            "allowance": limit,
            "used": used,
            "remaining": UNLIMITED if limit == UNLIMITED else max(0, limit - used),
            "resets_at": f"{_today()}T24:00:00Z",
        }
        if limit == UNLIMITED:
            return True, info
        return used < limit, info

    def consume(self, origin: Optional[str], size: str = "small") -> None:
        key = _key(origin, size)
        with self._lock:
            self._roll_day()
            self._used[key] = self._used.get(key, 0) + 1
            self._save()

    def snapshot(self) -> dict:
        with self._lock:
            self._roll_day()
            return {"day": self._day, "used": dict(self._used)}


pool_allowance_tracker = PoolAllowanceTracker()
