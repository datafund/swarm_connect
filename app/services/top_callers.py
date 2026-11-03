# app/services/top_callers.py
"""
Who is sending the most requests, without saying who they are (#588).

During the 2026-11-03 flood the metrics could not show where the traffic came
from. This counts requests per caller over fixed windows of
TOP_CALLERS_WINDOW_SECONDS. A caller is its get_client_key() value (IPv4
address, or IPv6 /64), and it is stored and shown only as a keyed hash tag:

- metrics carry ranks only (gateway_top_caller_requests{rank="1".."N"}), so the
  series count is fixed and no caller value ever becomes a label;
- one WARNING log line per window names the heaviest callers by tag, when the
  heaviest sent at least TOP_CALLERS_LOG_MIN_REQUESTS requests.

The hash key is random per process, so a tag cannot be reversed or matched
against a table of addresses, and does not link callers across restarts or
between environments. An operator who needs to check a suspected address sets
TOP_CALLERS_HASH_KEY (a secret, in the environment only); tags are then stable
and caller_tag() gives the tag of an address.

The number of callers held per window is capped at TOP_CALLERS_MAX_TRACKED;
requests from further callers are counted as untracked. Windows roll over on
the next request or metrics scrape after they end.
"""
import hashlib
import logging
import os
import re
import time
from threading import Lock
from typing import Dict, List, Optional, Tuple

from app.core.config import settings

logger = logging.getLogger(__name__)

TAG_BYTES = 6  # 12 hex characters


def _key_bytes(secret: str) -> bytes:
    return hashlib.sha256(secret.encode()).digest()


def caller_tag(client_key: str, secret: Optional[str] = None) -> str:
    """The tag of a caller key under TOP_CALLERS_HASH_KEY (or `secret`).

    For an operator checking a suspected address: pass the value
    get_client_key() gives for it (the IPv4 address, or the IPv6 /64).
    """
    key = _key_bytes(secret if secret is not None else settings.TOP_CALLERS_HASH_KEY)
    return hashlib.blake2b(client_key.encode(), key=key, digest_size=TAG_BYTES).hexdigest()


_SEGMENT = re.compile(r"[A-Za-z0-9._-]{1,32}")


def route_group(path: str) -> str:
    """A short, bounded form of the path for the log line: /api/v1/<area>.

    The path is the caller's text, so a segment that is not a plain word is
    shown as "other" rather than written to the log.
    """
    parts = [p for p in path.split("/") if p][:3 if path.startswith("/api/") else 1]
    if not parts:
        return "/"
    return "/" + "/".join(p if _SEGMENT.fullmatch(p) else "other" for p in parts)


def status_class(code: int) -> str:
    if code == 429:
        return "429"
    if code == 402:
        return "402"
    return f"{code // 100}xx" if 100 <= code < 600 else "other"


class _Caller:
    __slots__ = ("count", "statuses", "routes")

    def __init__(self) -> None:
        self.count = 0
        self.statuses: Dict[str, int] = {}
        self.routes: Dict[str, int] = {}


class TopCallerTracker:
    def __init__(self) -> None:
        self._lock = Lock()
        secret = settings.TOP_CALLERS_HASH_KEY
        self._key = _key_bytes(secret) if secret else os.urandom(32)
        self._window_start = self._now()
        self._callers: Dict[str, _Caller] = {}
        self._untracked = 0
        # Results of the last complete window.
        self._last_ranked: List[Tuple[str, int]] = []
        self._last_distinct = 0

    @staticmethod
    def _now() -> float:
        return time.monotonic()

    def tag(self, client_key: str) -> str:
        return hashlib.blake2b(client_key.encode(), key=self._key, digest_size=TAG_BYTES).hexdigest()

    def observe(self, client_key: str, status: int, path: str) -> None:
        tag = self.tag(client_key)
        untracked = False
        with self._lock:
            self._roll_over_locked()
            caller = self._callers.get(tag)
            if caller is None:
                if len(self._callers) >= settings.TOP_CALLERS_MAX_TRACKED:
                    self._untracked += 1
                    untracked = True
                else:
                    caller = self._callers[tag] = _Caller()
            if caller is not None:
                caller.count += 1
                sc = status_class(status)
                caller.statuses[sc] = caller.statuses.get(sc, 0) + 1
                rg = route_group(path)
                if rg in caller.routes or len(caller.routes) < 16:
                    caller.routes[rg] = caller.routes.get(rg, 0) + 1
        if untracked:
            try:
                from app.services.metrics import top_callers_untracked_total
                top_callers_untracked_total.inc()
            except Exception:  # pragma: no cover - defensive
                pass

    def ranked_count(self, rank: int) -> int:
        """Requests by the rank-th heaviest caller of the last complete window."""
        with self._lock:
            self._roll_over_locked()
            return self._last_ranked[rank - 1][1] if 0 < rank <= len(self._last_ranked) else 0

    def last_distinct(self) -> int:
        with self._lock:
            self._roll_over_locked()
            return self._last_distinct

    def last_ranked(self) -> List[Tuple[str, int]]:
        """[(tag, requests)] of the last complete window, heaviest first."""
        with self._lock:
            self._roll_over_locked()
            return list(self._last_ranked)

    def _roll_over_locked(self) -> None:
        window = settings.TOP_CALLERS_WINDOW_SECONDS
        now = self._now()
        if now - self._window_start < window:
            return
        callers = self._callers
        ranked = sorted(callers.items(), key=lambda kv: kv[1].count, reverse=True)
        top = ranked[:settings.TOP_CALLERS_N]
        # A window followed by an idle one leaves nothing to report for the
        # idle one: only the window that just ended counts as "last".
        ended_long_ago = now - self._window_start >= 2 * window
        if ended_long_ago:
            self._last_ranked = []
            self._last_distinct = 0
        else:
            self._last_ranked = [(tag, c.count) for tag, c in top]
            self._last_distinct = len(callers)
        if top and not ended_long_ago and top[0][1].count >= settings.TOP_CALLERS_LOG_MIN_REQUESTS:
            self._log(top, len(callers), self._untracked, window)
        self._callers = {}
        self._untracked = 0
        # Windows are aligned to the first one, so they stay fixed-length.
        self._window_start = now - ((now - self._window_start) % window)

    @staticmethod
    def _log(top, distinct: int, untracked: int, window: int) -> None:
        parts = []
        for tag, c in top:
            statuses = ",".join(f"{k}:{v}" for k, v in sorted(c.statuses.items()))
            route = max(c.routes.items(), key=lambda kv: kv[1])[0] if c.routes else "-"
            parts.append(f"caller={tag} requests={c.count} statuses={statuses} top_route={route}")
        logger.warning(
            f"Top callers in the last {window} s ({distinct} callers"
            f"{f', {untracked} requests untracked' if untracked else ''}): " + "; ".join(parts)
        )


top_caller_tracker = TopCallerTracker()
