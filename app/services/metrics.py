# app/services/metrics.py
"""
Prometheus metrics for the Provenance Gateway.

Defines custom business metrics (gauges, counters, info) and a background
task that periodically polls wallet balances and stamp pool state.
"""
import asyncio
from typing import Optional
import logging
import math
import time

from prometheus_client import Counter, Gauge, Info

from app.api.models.stamp import SIZE_PRESETS
from app.core.config import settings

logger = logging.getLogger(__name__)

# ── Info metric ──────────────────────────────────────────────────────────────

gateway_info = Info("gateway", "Gateway version and configuration")

# ── Gauges (updated by background poller) ────────────────────────────────────

# The `wallet` label carries the Gnosis address the balance belongs to. Without
# it an alert on these metrics cannot name the wallet to fund, so the address
# had to be written into the alert text by hand — where it named one specific
# node, was therefore already wrong for the other environment, and became wrong
# for both when that node was replaced. An operator acting on it would have sent
# funds to a decommissioned wallet. Each environment has exactly one wallet, so
# this adds no meaningful cardinality.
wallet_bzz_balance = Gauge(
    "gateway_wallet_bzz_balance", "Bee node BZZ balance", ["wallet"]
)
wallet_xdai_balance = Gauge(
    "gateway_wallet_xdai_balance", "Bee node xDAI balance", ["wallet"]
)
chequebook_available_balance = Gauge(
    "gateway_chequebook_available_balance", "Chequebook available BZZ balance"
)
base_eth_balance = Gauge(
    "gateway_base_eth_balance", "Base chain ETH balance for x402 gas", ["wallet"]
)
stamp_pool_available = Gauge(
    "gateway_stamp_pool_available", "Available stamps in pool by size", ["size"]
)
stamp_pool_pending = Gauge(
    "gateway_stamp_pool_pending",
    "Pool stamps bought but not usable yet (held, not handed out) by size", ["size"]
)
stamp_pool_target = Gauge(
    "gateway_stamp_pool_target", "Target reserve by size", ["size"]
)
# NOTE: these two come from Bee /batches, which is the network-wide postage
# contract view — every batch on Swarm, not this node's. Their names are kept
# for metric history; the help text is what was wrong. For this node's own
# stamps see the gateway_node_stamp_* metrics below.
stamps_total = Gauge(
    "gateway_stamps_total", "Total postage batches on the Swarm network (Bee /batches)"
)
stamp_min_ttl_seconds = Gauge(
    "gateway_stamp_min_ttl_seconds",
    "Lowest TTL among all postage batches on the Swarm network (not this node's)"
)
node_stamps_total = Gauge(
    "gateway_node_stamps_total", "Postage stamps owned by this Bee node (Bee /stamps)"
)
node_stamp_min_ttl_seconds = Gauge(
    "gateway_node_stamp_min_ttl_seconds", "Lowest TTL among stamps owned by this Bee node"
)
pool_stamp_min_ttl_seconds = Gauge(
    "gateway_pool_stamp_min_ttl_seconds", "Lowest TTL among pooled stamps"
)
bzz_usd_rate_configured = Gauge(
    "gateway_bzz_usd_rate_configured",
    "BZZ/USD rate the gateway prices with (X402_BZZ_USD_RATE)",
)
bzz_usd_rate_market = Gauge(
    "gateway_bzz_usd_rate_market",
    "BZZ/USD market rate from X402_BZZ_PRICE_FEED_URL; 0 when there is no feed "
    "or it has failed repeatedly (the drift alert ignores 0)",
)
bzz_usd_rate_market_updated = Gauge(
    "gateway_bzz_usd_rate_market_updated_timestamp_seconds",
    "Unix time of the last successful BZZ/USD feed read (0 = never)",
)
uptime_seconds = Gauge(
    "gateway_uptime_seconds", "Process uptime in seconds"
)

# ── Application counters (incremented in endpoint handlers) ──────────────────

uploads_total = Counter(
    "gateway_uploads_total", "Total upload attempts", ["status"]
)
upload_bytes_total = Counter(
    "gateway_upload_bytes_total", "Total bytes uploaded"
)
downloads_total = Counter(
    "gateway_downloads_total", "Total download attempts", ["status"]
)
stamp_purchases_total = Counter(
    "gateway_stamp_purchases_total", "Stamp purchases", ["size", "status"]
)
pool_acquires_total = Counter(
    "gateway_pool_acquires_total", "Pool stamp acquisitions", ["size", "status"]
)
notary_signatures_total = Counter(
    "gateway_notary_signatures_total",
    "Notary signatures: success = a signed document stored on Swarm, "
    "error = signing refused or failed",
    ["status"],
)

# The KPI series exist from startup (#518). A series created on its first event
# first appears at 1, and rate()/increase() have no earlier sample to measure
# from, so they count that event as 0 — for a rare one such as a paid acquire
# that could not be delivered, the rate panel would miss it entirely. A fresh
# gateway also shows 0 instead of "No data". (A restart of a series that already
# existed is not the problem: Prometheus counts the drop as a reset.) Only label
# values known and bounded at startup are created here; gateway_stamp_purchases_total
# is left alone because direct purchases take any depth, so its size can be depth-N.
for _status in ("success", "error"):
    uploads_total.labels(status=_status)
    notary_signatures_total.labels(status=_status)
for _status in ("success", "error", "too_large"):
    downloads_total.labels(status=_status)
# The pool serves only the preset sizes; the acquire request rejects any other depth.
for _size in SIZE_PRESETS:
    for _status in ("success", "error", "settled_not_delivered"):
        pool_acquires_total.labels(size=_size, status=_status)

# Free pool acquires refused because a daily allowance is spent (#554). Not a
# status of gateway_pool_acquires_total: that counter's "error" is an empty pool
# or a lost reservation race (409), and a dashboard panel read it as allowance
# refusals for months. Every label is from a fixed set, never from the caller:
#   size     - the size whose allowance was spent: a preset name, or "other"
#              for a batch of any other depth (see allowance_refusal_size_label);
#   scope    - WHICH allowance ran out: "origin" = the per-application budget,
#              "client" = the per-client cap;
#   response - the answer given: "402" when paying would bypass the allowance,
#              "429" otherwise, "409" when the requested size was out of stock
#              and the allowance for the larger size that would stand in is spent.
# No origin, IP or wallet is a label. 4 x 2 x 3 = 24 series, all created at 0
# so the first refusal after a deploy is counted by increase().
pool_allowance_refusals_total = Counter(
    "gateway_pool_allowance_refusals_total",
    "Free pool acquires refused because a daily allowance was used up",
    ["size", "scope", "response"],
)
POOL_ALLOWANCE_SIZES = tuple(SIZE_PRESETS) + ("other",)
POOL_ALLOWANCE_SCOPES = ("origin", "client")
POOL_ALLOWANCE_RESPONSES = ("402", "429", "409")
for _size in POOL_ALLOWANCE_SIZES:
    for _scope in POOL_ALLOWANCE_SCOPES:
        for _response in POOL_ALLOWANCE_RESPONSES:
            pool_allowance_refusals_total.labels(size=_size, scope=_scope, response=_response)


def allowance_refusal_size_label(size: str) -> str:
    """Bound the size label to the presets plus "other".

    The size charged is taken from the batch handed out, and the pool can hold
    a batch of a non-preset depth: sync_from_bee_node adopts a known batch at
    whatever depth Bee reports, and a free acquire may be served any larger
    batch in stock. depth_to_size_name() then returns "depth-N", which as a
    label would add a series for every such depth.
    """
    return size if size in SIZE_PRESETS else "other"


def count_pool_allowance_refusal(size: str, scope: str, response: str) -> None:
    pool_allowance_refusals_total.labels(
        size=allowance_refusal_size_label(size), scope=scope, response=response).inc()

# Failed saves of the pool state file and the ownership registry (#506). Both
# saves log at ERROR and carry on: they run after payment has settled, and
# raising there would turn a paid delivery into a 500. The change then exists
# in memory only, and a restart loses it; if both files fall behind, a sold
# batch is re-imported as pool inventory. `file` is "pool" or "ownership",
# never a path. An ownership save refused because the pre-migration copy
# failed (#558) counts too: that change is not on disk either. Both series
# exist from startup, so the first failure after a deploy is counted.
state_save_failures_total = Counter(
    "gateway_state_save_failures_total",
    "Failed saves of a gateway state file; the change is kept in memory only "
    "(file=pool: the stamp pool state, file=ownership: the ownership registry)",
    ["file"],
)
STATE_SAVE_FILES = ("pool", "ownership")
for _file in STATE_SAVE_FILES:
    state_save_failures_total.labels(file=_file)


# 1 while a state file is behind memory after a failed save, until a save
# succeeds (#506). The counter above shows that a save failed; this shows that
# the change is still unsaved, so the alert stays firing while it is. Nothing
# retries a failed ownership save, and the pool retries only on its next sync,
# so an increase() window alone would resolve while the change is still in
# memory only. Read at scrape time from each store's own flag.
state_unsaved = Gauge(
    "gateway_state_unsaved",
    "1 while a state file is behind the in-memory state after a failed save, "
    "until a save succeeds (file=pool|ownership)",
    ["file"],
)


def _pool_unsaved() -> bool:
    if not settings.STAMP_POOL_ENABLED:
        return False
    from app.services.stamp_pool import stamp_pool_manager
    return stamp_pool_manager.save_failed()


def _ownership_unsaved() -> bool:
    from app.services.stamp_ownership import stamp_ownership_manager
    return stamp_ownership_manager.changes_unsaved()


def _flag_reader(name: str, check):
    """A scrape-time reader that can never fail the scrape: 0 on any error.

    The stores are imported on first use, not here, so this module does not
    import them at load time (app.x402.settlement imports this module).
    """
    def read() -> float:
        try:
            return 1.0 if check() else 0.0
        except Exception as e:
            logger.warning(f"Metrics: could not read the {name} flag: {e}")
            return 0.0
    return read


for _file, _check in (("pool", _pool_unsaved), ("ownership", _ownership_unsaved)):
    state_unsaved.labels(file=_file).set_function(_flag_reader(f"{_file} unsaved-state", _check))


def count_state_save_failure(file: str) -> None:
    """Count one failed save; never raises, since the callers must not fail."""
    try:
        state_save_failures_total.labels(file=file).inc()
    except Exception as e:  # pragma: no cover - metrics must never break a save path
        logger.debug(f"Metrics: could not count a failed {file} state save: {e}")


# State files that fail closed while unreadable (#468). Each store keeps
# running but refuses what it guards until an operator restores or moves the
# file and restarts: the pool pauses its spending (#547; for an unreadable
# hourly spend record, pool_spend_times, purchases and top-ups for an hour by itself), the x402
# settlement-unknown registry refuses every paid request, the idempotency store
# refuses keyed paid requests, and the spend budget and pool allowance refuse
# limited spending and free acquires until the next UTC day. 1 while unreadable,
# else 0. Read from each store's own flag at scrape time, so the gauge needs no
# traffic to show it. The ownership registry and the bandwidth credit ledger
# are not here: an unreadable one stops startup, which "Gateway Down" reports.
state_file_unreadable = Gauge(
    "gateway_state_file_unreadable",
    "1 while a state file that fails closed cannot be read and what it guards is "
    "refused (file=pool|pool_spend_times|settlement_unknown|idempotency|spend_budget|pool_allowance)",
    ["file"],
)


def _pool_state_unreadable() -> bool:
    if not settings.STAMP_POOL_ENABLED:
        return False
    from app.services.stamp_pool import stamp_pool_manager
    return stamp_pool_manager.state_file_unreadable()


def _pool_spend_times_unreadable() -> bool:
    if not settings.STAMP_POOL_ENABLED:
        return False
    from app.services.stamp_pool import stamp_pool_manager
    return stamp_pool_manager.spend_times_unreadable()


def _settlement_state_unreadable() -> bool:
    from app.x402.settlement import outcome_unknown
    return outcome_unknown.state_unreadable()


def _idempotency_state_unreadable() -> bool:
    from app.x402.idempotency import idempotency_store
    return idempotency_store.state_unreadable()


def _spend_budget_state_unreadable() -> bool:
    from app.services.spend_budget import spend_budget_tracker
    return spend_budget_tracker.state_unreadable()


def _pool_allowance_state_unreadable() -> bool:
    from app.services.pool_allowance import pool_allowance_tracker
    return pool_allowance_tracker.state_unreadable()


STATE_FILE_CHECKS = {
    "pool": _pool_state_unreadable,
    "pool_spend_times": _pool_spend_times_unreadable,
    "settlement_unknown": _settlement_state_unreadable,
    "idempotency": _idempotency_state_unreadable,
    "spend_budget": _spend_budget_state_unreadable,
    "pool_allowance": _pool_allowance_state_unreadable,
}


def _state_file_reader(file: str, check):
    """A scrape-time reader that can never fail the scrape.

    The stores are imported on first use, not here: app.x402.settlement imports
    this module, so a module-level import would be circular.
    """
    def read() -> float:
        try:
            return 1.0 if check() else 0.0
        except Exception as e:
            logger.warning(f"Metrics: could not read the {file} state-file flag: {e}")
            return 0.0
    return read


for _file, _check in STATE_FILE_CHECKS.items():
    state_file_unreadable.labels(file=_file).set_function(_state_file_reader(_file, _check))


x402_settlements_total = Counter(
    "gateway_x402_settlements_total",
    "x402 settlement outcomes (#375): settled, refused (nothing delivered), error "
    "(outcome unknown, nothing delivered), settled_not_delivered (refund needed), "
    "refused_after_unknown (a retry of an authorization whose earlier settlement got no "
    "answer was refused: the earlier transfer probably went through)",
    ["result"],
)
# Every result exists from startup. A series first seen at 1 gives increase() of
# 0 for that event, and the process restarts on every deploy, so without this
# the first failure of each kind after a restart would never alert.
for _result in ("settled", "refused", "error", "settled_not_delivered", "refused_after_unknown"):
    x402_settlements_total.labels(result=_result)
x402_payments_total = Counter(
    "gateway_x402_payments_total", "x402 payment mode breakdown", ["mode"]
)
rate_limit_hits_total = Counter(
    "gateway_rate_limit_hits_total", "Rate limit rejections"
)
bee_api_errors_total = Counter(
    "gateway_bee_api_errors_total", "Upstream Bee node errors", ["endpoint"]
)
# Shared outbound HTTP client pool (#587). A PoolTimeout means a request
# waited the whole pool timeout without getting a connection. Replacements
# are counted by outcome: replaced, replaced_unverified (the pool could not be
# read, so the Bee probe alone decided), probe_failed (Bee did not answer on a
# new connection either), not_stuck (timeouts, but the pool is busy rather
# than holding leaked slots). All series exist from startup.
bee_http_pool_timeouts_total = Counter(
    "gateway_bee_http_pool_timeouts_total",
    "Outbound requests of the shared HTTP client that got no pool connection in time (PoolTimeout)",
)
BEE_HTTP_REPLACEMENT_RESULTS = ("replaced", "replaced_unverified", "probe_failed", "not_stuck")
bee_http_client_replacements_total = Counter(
    "gateway_bee_http_client_replacements_total",
    "Recovery checks of the shared HTTP client after repeated PoolTimeouts, by outcome "
    "(result=replaced|replaced_unverified|probe_failed|not_stuck)",
    ["result"],
)
for _result in BEE_HTTP_REPLACEMENT_RESULTS:
    bee_http_client_replacements_total.labels(result=_result)

# Connections in the shared client's pool, read at scrape time. state=active
# (serving a request), idle (kept alive), stuck (neither idle nor closed and
# held by no request: the slots #587 leaks). NaN when the pool cannot be read.
bee_http_pool_connections = Gauge(
    "gateway_bee_http_pool_connections",
    "Connections in the shared HTTP client's pool (state=active|idle|stuck)",
    ["state"],
)
bee_http_pool_requests = Gauge(
    "gateway_bee_http_pool_requests",
    "Requests in the shared HTTP client's pool, waiting for or holding a connection",
)


def _pool_reader(key: str):
    """A scrape-time reader of the shared client's pool; never fails the scrape."""
    def read() -> float:
        try:
            from app.services.http_client import _client, current_pool_snapshot
            if _client is None:
                return 0.0
            snapshot = current_pool_snapshot()
            return float("nan") if snapshot is None else float(snapshot[key])
        except Exception as e:
            logger.debug(f"Metrics: could not read the HTTP client pool: {e}")
            return float("nan")
    return read


for _state in ("active", "idle", "stuck"):
    bee_http_pool_connections.labels(state=_state).set_function(_pool_reader(_state))
bee_http_pool_requests.set_function(_pool_reader("requests"))

bee_poll_total = Counter(
    "gateway_bee_poll_total", "Balance poll attempts", ["status"]
)
chunk_uploads_total = Counter(
    "gateway_chunk_uploads_total", "Pre-stamped chunk forwarding attempts", ["status", "mode"]
)
chunk_upload_bytes_total = Counter(
    "gateway_chunk_upload_bytes_total", "Total bytes forwarded as pre-stamped chunks"
)
bandwidth_topups_total = Counter(
    "gateway_bandwidth_topups_total", "Bandwidth credit top-ups", ["status"]
)
bandwidth_topup_bytes_total = Counter(
    "gateway_bandwidth_topup_bytes_total", "Total bytes of bandwidth credit sold via top-ups"
)

# ── Who we are serving, and whether we are serving them (#347) ──────────────
#
# Production traffic turned out to be almost entirely one client — the MCP
# plugin, run by AI agents in cloud sandboxes — and 37 of its 84 production
# requests were refused with 429. That was invisible until someone read the
# reverse-proxy access logs by hand.
#
# Deliberately a NEW counter rather than a client_type label on the existing
# ones. A dashboard panel graphs rate(gateway_rate_limit_hits_total[5m])
# unaggregated; adding a label there would turn one line into six and silently
# change what the panel means. Existing series stay as they are.
#
# client_type comes from a fixed table (app/services/client_type.py), never from
# the User-Agent directly — a label must not take a value the caller chooses, or
# anyone can mint unlimited series. Eight client types by five outcomes bounds
# this at 40 series per environment.
requests_by_client_total = Counter(
    "gateway_requests_by_client_total",
    "API requests by client type and outcome",
    ["client_type", "outcome"],
)

# A COUNT of callers seen today, not their identities. Read from the tracker in
# app/middleware/client_metrics.py at scrape time, so it resets at midnight UTC
# without waiting for a request.
distinct_callers = Gauge(
    "gateway_distinct_callers",
    "Distinct callers seen so far today",
)

# The final count of the previous UTC day. The first scrape after midnight
# already reads the new day, so callers seen after the last scrape before
# midnight appear in no sample of distinct_callers. Exported through the next
# day so the per-day panel can attribute it to the day it belongs to.
distinct_callers_previous_day = Gauge(
    "gateway_distinct_callers_previous_day",
    "Distinct callers on the previous UTC day, final count",
)

# ── Spending limits (#102) ──────────────────────────────────────────────────
#
# Refusals are the signal that matters. The daily budget is a number chosen
# without usage data, so the only way to tell "correctly bounding abuse" from
# "turning away a legitimate integration" is to watch how often it fires and
# whether it is always the same caller. Without these, we would learn about a
# limit set too low from a complaint rather than a dashboard.
#
# No caller identity is exposed as a label: an IP is high-cardinality and would
# multiply the series, and it is personal data going to a third-party metrics
# store. The logs already name the caller for anyone diagnosing a specific case.
stamp_spend_refusals_total = Counter(
    "gateway_stamp_spend_refusals_total",
    "Stamp purchases and extensions refused by a spending limit",
    ["operation", "limit"],
)
stamp_spend_bzz_total = Counter(
    "gateway_stamp_spend_bzz_total",
    "BZZ committed through the stamp endpoints, by whether it was charged to a budget",
    ["operation", "charged"],
)
stamp_spend_callers = Gauge(
    "gateway_stamp_spend_callers",
    "Distinct callers holding a non-zero spend balance today",
)
gateway_spend_bzz_today = Gauge(
    "gateway_spend_bzz_today",
    "BZZ reserved against the gateway-wide daily ceilings today (#363): "
    "scope=total (everything) or scope=free (unpaid spending)",
    ["scope"],
)
gateway_spend_uncertain_bzz_total = Counter(
    "gateway_spend_uncertain_bzz_total",
    "BZZ kept charged against the spending limits after a failure whose outcome "
    "was uncertain (timeout after sending, cancellation, 5xx); may or may not "
    "have been spent (#363). A paid purchase answered 202 is counted once, when "
    "its background half ends without a batch or a definite refusal (#468)",
    ["operation"],
)
gateway_spend_ceiling_bzz = Gauge(
    "gateway_spend_ceiling_bzz",
    "Configured gateway-wide daily ceilings (-1 = unlimited)",
    ["scope"],
)
stamp_spend_bzz_today = Gauge(
    "gateway_stamp_spend_bzz_today",
    "Total BZZ charged to daily budgets today, across all callers",
)

# ── Bandwidth credit gauges (updated by background poller) ───────────────────

bandwidth_credit_accounts = Gauge(
    "gateway_bandwidth_credit_accounts", "Bandwidth credit accounts with a non-zero balance"
)
bandwidth_credit_bytes_total = Gauge(
    "gateway_bandwidth_credit_bytes_total", "Total outstanding (unspent) bandwidth credit in bytes"
)

# ── Flow B: buy-batch-for-owner (#231) ──────────────────────────────────────
for_owner_batches_total = Counter(
    "gateway_for_owner_batches_total", "createBatch-for-owner attempts", ["status"]
)
for_owner_bzz_spent_total = Counter(
    "gateway_for_owner_bzz_spent_total", "Total PLUR spent creating batches for owners"
)
gnosis_signer_xbzz_balance = Gauge(
    "gateway_gnosis_signer_xbzz_balance", "Gnosis signer wallet xBZZ balance (BZZ)"
)
gnosis_signer_xdai_balance = Gauge(
    "gateway_gnosis_signer_xdai_balance", "Gnosis signer wallet xDAI balance"
)

# ── Background task ──────────────────────────────────────────────────────────

_background_task = None
_start_time = None


async def update_node_stamp_metrics():
    """Update the gauges describing stamps owned by THIS Bee node (Bee /stamps).

    Kept separate from the network-wide /batches gauges so that a failure in
    either source cannot blank the other, and so the node-owned view is testable
    without driving the polling loop.

    Never raises.
    """
    try:
        from app.services.swarm_api import get_local_stamps
        node_stamps = await get_local_stamps()
        node_stamps_total.set(len(node_stamps))

        node_min_ttl = float("inf")
        for s in node_stamps:
            ttl = s.get("batchTTL", 0)
            if isinstance(ttl, (int, float)) and ttl > 0:
                node_min_ttl = min(node_min_ttl, ttl)
        node_stamp_min_ttl_seconds.set(
            node_min_ttl if node_min_ttl < float("inf") else 0
        )
    except Exception as e:
        logger.debug(f"Metrics: failed to get node-owned stamp info: {e}")


# None means never fetched. NOT 0.0: the throttle below compares against
# time.monotonic(), which is time since boot, so a sentinel of 0.0 reads as
# "fetched at boot" and suppresses the first read for a whole interval on any
# host whose uptime is under X402_BZZ_PRICE_FEED_INTERVAL_SECONDS. That is every
# freshly booted machine, and every CI runner — which is how this was found.
_last_price_fetch: Optional[float] = None
_price_feed_failures = 0
# After this many consecutive failed reads the market gauge goes back to 0, so a
# dead feed stops the drift alert from judging against a stale price.
PRICE_FEED_MAX_FAILURES = 3


def _first_usd_value(data):
    """The first numeric "usd" value anywhere in a JSON document."""
    if isinstance(data, dict):
        v = data.get("usd")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
        for child in data.values():
            found = _first_usd_value(child)
            if found is not None:
                return found
    elif isinstance(data, list):
        for child in data:
            found = _first_usd_value(child)
            if found is not None:
                return found
    return None


async def _update_bzz_rates() -> None:
    """Configured rate always; market rate from the feed when one is set (#364)."""
    global _last_price_fetch, _price_feed_failures
    bzz_usd_rate_configured.set(settings.X402_BZZ_USD_RATE)
    url = settings.X402_BZZ_PRICE_FEED_URL
    if not url:
        return
    if (_last_price_fetch is not None
            and time.monotonic() - _last_price_fetch < settings.X402_BZZ_PRICE_FEED_INTERVAL_SECONDS):
        return
    _last_price_fetch = time.monotonic()
    try:
        import httpx
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0)) as client:
            response = await client.get(url)
            response.raise_for_status()
            if len(response.content) > 64 * 1024:
                raise ValueError("price feed response too large")
            price = _first_usd_value(response.json())
        if price is None or not math.isfinite(price) or price <= 0:
            raise ValueError(f"no usable positive 'usd' value in the feed response ({price!r})")
    except Exception as e:
        _price_feed_failures += 1
        logger.warning(f"BZZ price feed read failed ({_price_feed_failures} in a row): {e}")
        if _price_feed_failures >= PRICE_FEED_MAX_FAILURES:
            bzz_usd_rate_market.set(0)
        return
    _price_feed_failures = 0
    bzz_usd_rate_market.set(price)
    bzz_usd_rate_market_updated.set(time.time())
    ratio = settings.X402_BZZ_USD_RATE / price
    if ratio > 2 or ratio < 0.5:
        logger.warning(
            f"BZZ/USD pricing rate {settings.X402_BZZ_USD_RATE} is {ratio:.1f}x the market "
            f"price {price}; review X402_BZZ_USD_RATE"
        )


async def _poll_balances():
    """Periodically poll wallet balances and update Prometheus gauges."""
    while True:
        try:
            # Isolated: a pricing-metric problem must not skip the balance polls.
            try:
                await _update_bzz_rates()
            except Exception as e:
                logger.debug(f"Metrics: BZZ rate update failed: {e}")
            # Update uptime
            if _start_time is not None:
                uptime_seconds.set(time.monotonic() - _start_time)

            # Wallet balances — use preflight checks which parse all balances
            # correctly (BZZ from bzzBalance, xDAI from nativeTokenBalance).
            # Only update gauges on success — keep last known value on failure
            # to avoid false 0-balance alerts from transient Bee node issues.
            try:
                from app.x402.preflight import (
                    check_xbzz_balance,
                    check_xdai_balance,
                    check_chequebook_balance,
                )
                xbzz = await check_xbzz_balance()
                if xbzz.get("ok") or xbzz.get("balance_bzz", 0) > 0:
                    wallet_bzz_balance.labels(
                        wallet=xbzz.get("wallet_address") or "unknown"
                    ).set(xbzz["balance_bzz"])

                xdai = await check_xdai_balance()
                if xdai.get("ok") or xdai.get("balance_xdai", 0) > 0:
                    wallet_xdai_balance.labels(
                        wallet=xdai.get("wallet_address") or "unknown"
                    ).set(xdai["balance_xdai"])

                cheque = await check_chequebook_balance()
                if cheque.get("ok") or cheque.get("available_bzz", 0) > 0:
                    chequebook_available_balance.set(cheque["available_bzz"])

                bee_poll_total.labels(status="success").inc()
            except Exception as e:
                bee_poll_total.labels(status="error").inc()
                logger.debug(f"Metrics: failed to get wallet balances: {e}")

            # Base ETH balance (only when x402 enabled)
            if settings.X402_ENABLED:
                try:
                    from app.x402.base_balance import check_base_eth_balance
                    base = await check_base_eth_balance()
                    if base.get("ok") or base.get("balance_eth", 0) > 0:
                        base_eth_balance.labels(
                            wallet=base.get("address") or "unknown"
                        ).set(base["balance_eth"])
                except Exception as e:
                    logger.debug(f"Metrics: failed to get base ETH balance: {e}")

            # Network-wide batch count and min TTL (Bee /batches — every batch on
            # Swarm, so the minimum TTL is almost always someone else's batch about
            # to expire). Kept for continuity; see the node-owned metrics below for
            # anything describing THIS node.
            try:
                from app.services.swarm_api import get_all_stamps
                all_stamps = await get_all_stamps()
                stamps_total.set(len(all_stamps))

                # Find minimum TTL
                min_ttl = float("inf")
                for s in all_stamps:
                    ttl = s.get("batchTTL", 0)
                    if isinstance(ttl, (int, float)) and ttl > 0:
                        min_ttl = min(min_ttl, ttl)
                if min_ttl < float("inf"):
                    stamp_min_ttl_seconds.set(min_ttl)
                else:
                    stamp_min_ttl_seconds.set(0)
            except Exception as e:
                logger.debug(f"Metrics: failed to get stamp info: {e}")

            await update_node_stamp_metrics()

            # Stamp pool state
            if settings.STAMP_POOL_ENABLED:
                try:
                    from app.services.stamp_pool import stamp_pool_manager
                    DEPTH_TO_SIZE = {17: "small", 20: "medium", 22: "large"}
                    status = stamp_pool_manager.get_status()
                    for depth, target in status.reserve_config.items():
                        size_name = DEPTH_TO_SIZE.get(int(depth), f"depth-{depth}")
                        stamp_pool_available.labels(size=size_name).set(
                            status.current_levels.get(int(depth), 0)
                        )
                        stamp_pool_target.labels(size=size_name).set(target)
                        stamp_pool_pending.labels(size=size_name).set(
                            status.pending_stamps.get(int(depth), 0)
                        )

                    # Pool stamp min TTL — check TTL of all available pool stamps
                    pool_min_ttl = float("inf")
                    for depth_ids in status.available_stamps.values():
                        for batch_id in depth_ids:
                            try:
                                ttl = await stamp_pool_manager._get_stamp_ttl(batch_id)
                                if ttl is not None and ttl > 0:
                                    pool_min_ttl = min(pool_min_ttl, ttl)
                            except Exception:
                                pass
                    if pool_min_ttl < float("inf"):
                        pool_stamp_min_ttl_seconds.set(pool_min_ttl)
                    else:
                        pool_stamp_min_ttl_seconds.set(0)
                except Exception as e:
                    logger.debug(f"Metrics: failed to get pool status: {e}")

            # Bandwidth credit ledger (chunk forwarding feature)
            if settings.CHUNK_UPLOAD_ENABLED:
                try:
                    from app.services.bandwidth_credit import bandwidth_credit_manager
                    bandwidth_credit_accounts.set(bandwidth_credit_manager.account_count())
                    bandwidth_credit_bytes_total.set(bandwidth_credit_manager.total_outstanding_bytes())
                except Exception as e:
                    logger.debug(f"Metrics: failed to get bandwidth credit state: {e}")

            # Daily spend budgets on the stamp endpoints (#102).
            #
            # Polled from the tracker rather than incremented at the call site,
            # because the day rolls over inside it: a counter would keep
            # climbing while the underlying balances reset at midnight UTC.
            try:
                from app.services.spend_budget import spend_budget_tracker
                snap = spend_budget_tracker.snapshot()
                spent = snap["spent"]
                stamp_spend_callers.set(len(spent))
                stamp_spend_bzz_today.set(sum(spent.values()))
                gateway_spend_bzz_today.labels(scope="total").set(snap["gateway_spent"])
                gateway_spend_bzz_today.labels(scope="free").set(snap["giveaway_spent"])
                gateway_spend_ceiling_bzz.labels(scope="total").set(settings.GATEWAY_DAILY_BZZ_CEILING)
                gateway_spend_ceiling_bzz.labels(scope="free").set(settings.GATEWAY_DAILY_BZZ_FREE_CEILING)
            except Exception as e:
                logger.debug(f"Metrics: failed to get spend budget state: {e}")

            # Gnosis signer wallet balances (buy-batch-for-owner feature)
            if settings.STAMP_PURCHASE_FOR_OTHERS_ENABLED:
                try:
                    from app.services.gnosis_chain import gnosis_chain_client
                    if gnosis_chain_client.is_configured:
                        bals = await gnosis_chain_client.get_balances()
                        gnosis_signer_xbzz_balance.set(bals["xbzz_plur"] / 1e16)
                        gnosis_signer_xdai_balance.set(bals["xdai_wei"] / 1e18)
                except Exception as e:
                    logger.debug(f"Metrics: failed to get Gnosis signer balances: {e}")

        except Exception as e:
            logger.warning(f"Metrics background poll error: {e}")

        await asyncio.sleep(settings.METRICS_BALANCE_POLL_SECONDS)


async def start_metrics_background_task():
    """Start the background balance polling task."""
    global _background_task, _start_time
    _start_time = time.monotonic()
    _background_task = asyncio.create_task(_poll_balances())
    logger.info(
        f"Metrics background task started (poll interval: {settings.METRICS_BALANCE_POLL_SECONDS}s)"
    )


async def stop_metrics_background_task():
    """Stop the background balance polling task."""
    global _background_task
    if _background_task is not None:
        _background_task.cancel()
        try:
            await _background_task
        except asyncio.CancelledError:
            pass
        _background_task = None
        logger.info("Metrics background task stopped")
