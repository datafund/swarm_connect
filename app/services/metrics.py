# app/services/metrics.py
"""
Prometheus metrics for the Provenance Gateway.

Defines custom business metrics (gauges, counters, info) and a background
task that periodically polls wallet balances and stamp pool state.
"""
import asyncio
import logging
import time

from prometheus_client import Counter, Gauge, Info

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
    "gateway_notary_signatures_total", "Notary signing operations", ["status"]
)
x402_payments_total = Counter(
    "gateway_x402_payments_total", "x402 payment mode breakdown", ["mode"]
)
rate_limit_hits_total = Counter(
    "gateway_rate_limit_hits_total", "Rate limit rejections"
)
bee_api_errors_total = Counter(
    "gateway_bee_api_errors_total", "Upstream Bee node errors", ["endpoint"]
)
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


async def _poll_balances():
    """Periodically poll wallet balances and update Prometheus gauges."""
    while True:
        try:
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
