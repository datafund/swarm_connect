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
import json
import logging
import os
from datetime import datetime, timezone
from threading import Lock
from typing import Dict, Optional, Tuple

from app.core.config import settings

logger = logging.getLogger(__name__)

# Keys for the gateway-wide totals, kept alongside per-caller keys. Not valid
# IPs, so they cannot collide with a caller.
GLOBAL_KEY = "(gateway total)"
GIVEAWAY_KEY = "(gateway giveaway)"
TOTAL_KEYS = (GLOBAL_KEY, GIVEAWAY_KEY)


class Hold:
    """Amounts reserved together for one spend, on one UTC day (#363).

    Released only if the spend certainly did not happen, and only on the day it
    was taken: after midnight the day's counters have been reset, and taking a
    stale hold off them would erase real spend of the new day.
    """

    def __init__(self, day: str, charges):
        self.day = day
        self.charges = list(charges)  # [(key, cost_bzz)]
        self.released = False


def spend_certainly_did_not_happen(exc: BaseException) -> bool:
    """Whether a failed spend definitely spent nothing, so its hold can go back.

    Only when the refusal came before the money moved: a check of our own
    (HTTPException), a connection that was never made, or Bee answering with a
    4xx refusal. A timeout after the request was sent, a cancellation, a 5xx or
    anything unrecognised may have spent money, so the hold is kept and the
    limits fail closed.
    """
    import httpx
    from fastapi import HTTPException
    if isinstance(exc, HTTPException):
        return True
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return 400 <= exc.response.status_code < 500
    return False

# Sentinel for "no limit", matching pool_allowance so the two read alike.
UNLIMITED = -1.0


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class SpendBudgetTracker:
    """Tracks BZZ spent per caller per UTC day."""

    def __init__(self, state_file: Optional[str] = None):
        self._lock = Lock()
        self._state_file = state_file
        self._day = _today()
        self._spent: Dict[str, float] = {}
        self._load()

    # --- persistence -------------------------------------------------------
    #
    # Persisted for the same reason as the pool allowance: without it a
    # crash-looping gateway grants a fresh budget on every restart, which is
    # precisely the shape of the incident this exists to prevent.

    def _path(self) -> str:
        return self._state_file or settings.STAMP_SPEND_BUDGET_STATE_FILE

    def _load(self) -> None:
        try:
            path = self._path()
            if not os.path.exists(path):
                return
            with open(path) as f:
                data = json.load(f)
            if data.get("day") == self._day:
                self._spent = {k: float(v) for k, v in (data.get("spent") or {}).items()}
                logger.info("Loaded spend budget state for %s: %s", self._day, self._spent)
        except Exception as e:
            # Never fail startup over a counter.
            logger.warning("Could not load spend budget state: %s", e)

    def _save(self) -> None:
        try:
            path = self._path()
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            tmp = f"{path}.tmp"
            with open(tmp, "w") as f:
                json.dump({"day": self._day, "spent": self._spent}, f)
            os.replace(tmp, path)
        except Exception as e:
            logger.warning("Could not persist spend budget state: %s", e)

    # --- budget ------------------------------------------------------------

    def _roll_day(self) -> None:
        today = _today()
        if today != self._day:
            logger.info("Spend budget day rolled %s -> %s, resetting", self._day, today)
            self._day = today
            self._spent = {}
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
        return (spent + cost_bzz) <= limit, info

    def reserve_all(self, charges) -> Tuple[Optional[Hold], Optional[str], dict]:
        """Reserve several (key, cost, limit) charges atomically: all or none.

        One lock and one write for the whole set, so a request that will be
        refused on one limit never holds another in the meantime. Returns
        (hold, None, {}) on success, or (None, refusing_key, info) on refusal.
        """
        with self._lock:
            self._roll_day()
            for key, cost, limit in charges:
                spent = self._spent.get(key, 0.0)
                if limit != UNLIMITED and spent + cost > limit:
                    remaining = max(0.0, limit - spent)
                    return None, key, {
                        "caller": key,
                        "daily_budget_bzz": limit,
                        "spent_bzz": round(spent, 6),
                        "remaining_bzz": round(remaining, 6),
                        "request_cost_bzz": round(cost, 6),
                        "resets_at": f"{self._day}T24:00:00Z",
                    }
            for key, cost, _ in charges:
                self._spent[key] = self._spent.get(key, 0.0) + cost
            self._save()
            return Hold(self._day, [(k, c) for k, c, _ in charges]), None, {}

    def release_hold(self, hold: Optional[Hold]) -> None:
        """Give back a hold whose spend certainly did not happen (see Hold)."""
        if hold is None or hold.released:
            return
        with self._lock:
            self._roll_day()
            hold.released = True
            if hold.day != self._day:
                return
            for key, cost in hold.charges:
                if key in self._spent:
                    left = self._spent[key] - cost
                    if left <= 1e-12:
                        del self._spent[key]
                    else:
                        self._spent[key] = left
            self._save()

    def reserve_spend(self, cost_bzz: float, caller: Optional[str]) -> Tuple[Optional[Hold], Optional[str], dict]:
        """Reserve a gateway spend against every limit that applies to it.

        Always the gateway-wide ceiling. When `caller` is given (the spend is a
        giveaway: unpaid, or paid on a test network) also the free-spending
        ceiling and that caller's daily budget. All or nothing, one write.
        """
        charges = [(GLOBAL_KEY, cost_bzz, settings.GATEWAY_DAILY_BZZ_CEILING)]
        if caller is not None:
            charges += [(GIVEAWAY_KEY, cost_bzz, settings.GATEWAY_DAILY_BZZ_FREE_CEILING),
                        (caller, cost_bzz, self.budget())]
        return self.reserve_all(charges)

    def reserve_gateway(self, cost_bzz: float) -> Tuple[Optional[Hold], dict]:
        """Reserve against GATEWAY_DAILY_BZZ_CEILING only (pool, for-owner)."""
        hold, _, info = self.reserve_all([(GLOBAL_KEY, cost_bzz, settings.GATEWAY_DAILY_BZZ_CEILING)])
        return hold, info

    def reserve(self, caller: str, cost_bzz: float, limit: Optional[float] = None) -> Tuple[bool, dict]:
        """Check and charge in one step (#363).

        check() followed later by consume() let concurrent requests all pass
        the check before any of them was charged. This charges immediately,
        under the lock; call release() if the spend then does not happen.
        """
        limit = self.budget() if limit is None else limit
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
                "resets_at": f"{self._day}T24:00:00Z",
            }
            if limit != UNLIMITED and spent + cost_bzz > limit:
                return False, info
            self._spent[caller] = spent + cost_bzz
            self._save()
            return True, info

    def consume(self, caller: str, cost_bzz: float) -> None:
        with self._lock:
            self._roll_day()
            self._spent[caller] = self._spent.get(caller, 0.0) + cost_bzz
            self._save()

    def snapshot(self) -> dict:
        with self._lock:
            self._roll_day()
            return {
                "day": self._day,
                "spent": {k: v for k, v in self._spent.items() if k not in TOTAL_KEYS},
                "gateway_spent": self._spent.get(GLOBAL_KEY, 0.0),
                "giveaway_spent": self._spent.get(GIVEAWAY_KEY, 0.0),
            }


spend_budget_tracker = SpendBudgetTracker()
