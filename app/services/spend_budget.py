# app/services/spend_budget.py
"""Daily cap on how much of the gateway's BZZ one caller can spend.

Two endpoints spend the operator's money on behalf of whoever asks:

- `POST /api/v1/stamps/` buys a postage batch.
- `PATCH /api/v1/stamps/{id}/extend` tops one up.

Neither had a spending bound. The pool got a daily allowance (#320, #326) and
these did not, which made them the cheaper way to spend the operator's money.
`X402_MAX_STAMP_BZZ` existed in configuration and was referenced nowhere, so the
only thing standing between a caller and the whole wallet was the wallet running
out. Measured on staging, an anonymous free-tier request reached the point of
the gateway costing a 243,074 BZZ batch and was refused for insufficient funds
rather than by any policy.

## Why a BZZ budget rather than a count of batches

The pool hands out fixed inventory, so counting batches per size bounds the
spend. These endpoints take a depth and a duration and cost `amount x 2^depth`,
which is continuous and spans orders of magnitude — a count would let a caller
stay inside its allowance and still spend arbitrarily by asking for bigger
batches. Counting the money directly is the only bound that means the same thing
whatever shape the request takes.

## Why the client IP

The pool keys on `Origin` because its consumer is a browser app. The callers
here are CLIs, SDKs and the MCP plugin, which send no `Origin` at all, so it
would collapse every one of them into a single shared bucket. The IP is what
distinguishes them.

An IP is not an identity: it is shared behind NAT and cheap to change with a
proxy. This is the same bargain the free tier already makes elsewhere in the
gateway (`bandwidth_free_tier.py` bounds chunk uploads the same way). It bounds
casual and accidental spending — which is what actually happened, twice — and
raises the cost of deliberate spending without pretending to prevent it. A
caller who wants a real allowance pays, and paying bypasses this entirely.
"""
import math
import logging
from datetime import datetime, timezone
from threading import Lock
from typing import Dict, Optional, Tuple

from app.core.config import settings
from app.core.atomic_io import StateLoadError, atomic_write_json, load_json_state, unreadable_state

logger = logging.getLogger(__name__)

# Sentinel for "no limit", matching pool_allowance so the two read alike.
UNLIMITED = -1.0


def _is_amount(v) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v) and v >= 0)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class SpendBudgetTracker:
    """Tracks BZZ spent per caller per UTC day."""

    def __init__(self, state_file: Optional[str] = None):
        self._lock = Lock()
        self._state_file = state_file
        self._day = _today()
        self._spent: Dict[str, float] = {}
        # Set when today's file could not be read; see _load.
        self._unreadable: Optional[str] = None
        self._load()

    # --- persistence -------------------------------------------------------
    #
    # Persisted for the same reason as the pool allowance: without it a
    # crash-looping gateway grants a fresh budget on every restart, which is
    # precisely the shape of the incident this exists to prevent.

    def _path(self) -> str:
        return self._state_file or settings.STAMP_SPEND_BUDGET_STATE_FILE

    def _load(self) -> None:
        # Only a missing file means a fresh day. An unreadable one is not
        # treated as empty (#378): that would reset every caller's BZZ budget
        # and the next save would overwrite today's record. Instead a copy is
        # kept, nothing is written, and every limited check is refused until
        # the next UTC day or until an operator restores or removes the file
        # and restarts. Stopping the process instead would take paid traffic
        # down with it over one day of counters.
        path = self._path()
        try:
            data = load_json_state(path)
            if data is None:
                return
            if "day" not in data:
                logger.warning("Spend budget state %s has no day; ignoring it", path)
                return
            if data["day"] != self._day:
                return
            spent = data.get("spent", {})
            if not isinstance(spent, dict) or not all(_is_amount(v) for v in spent.values()):
                raise unreadable_state(path, "spent must map callers to non-negative numbers")
        except StateLoadError as e:
            self._unreadable = str(e)
            logger.error("Spend budget refused until the next UTC day: %s", e)
            return
        self._spent = {k: float(v) for k, v in spent.items()}
        # Totals only: keys are client addresses, which do not belong in logs.
        logger.info("Loaded spend budget state for %s: %d callers, %.4f BZZ",
                    self._day, len(self._spent), sum(self._spent.values()))

    def _save(self) -> None:
        if self._unreadable:
            return  # leave the unreadable file for the operator
        try:
            atomic_write_json(self._path(), {"day": self._day, "spent": self._spent})
        except Exception as e:
            logger.warning("Could not persist spend budget state: %s", e)

    # --- budget ------------------------------------------------------------

    def _roll_day(self) -> None:
        today = _today()
        if today != self._day:
            logger.info("Spend budget day rolled %s -> %s, resetting", self._day, today)
            self._day = today
            self._spent = {}
            self._unreadable = None
            self._save()

    def budget(self) -> float:
        return settings.STAMP_DAILY_BZZ_PER_CALLER

    def check(self, caller: str, cost_bzz: float) -> Tuple[bool, dict]:
        """Whether this caller may spend `cost_bzz`, and the numbers behind it.

        Does not consume — call `consume` once the money has actually been
        spent, so a purchase that fails downstream does not cost the caller
        their budget.

        The check is against the cost of THIS request, not merely whether any
        budget remains: a caller with 0.01 BZZ left must not be allowed to start
        a 5 BZZ purchase.
        """
        limit = self.budget()
        with self._lock:
            self._roll_day()
            spent = self._spent.get(caller, 0.0)

        remaining = UNLIMITED if limit == UNLIMITED else max(0.0, limit - spent)
        info = {
            "caller": caller,
            "daily_budget_bzz": limit,
            "spent_bzz": round(spent, 6),
            "remaining_bzz": remaining if limit == UNLIMITED else round(remaining, 6),
            "request_cost_bzz": round(cost_bzz, 6),
            "resets_at": f"{_today()}T24:00:00Z",
        }
        if limit == UNLIMITED:
            return True, info
        if self._unreadable:
            info["state_unreadable"] = True
            return False, info
        return (spent + cost_bzz) <= limit, info

    def consume(self, caller: str, cost_bzz: float) -> None:
        with self._lock:
            self._roll_day()
            self._spent[caller] = self._spent.get(caller, 0.0) + cost_bzz
            self._save()

    def snapshot(self) -> dict:
        with self._lock:
            self._roll_day()
            return {"day": self._day, "spent": dict(self._spent)}


spend_budget_tracker = SpendBudgetTracker()
