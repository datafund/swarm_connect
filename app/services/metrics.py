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

wallet_bzz_balance = Gauge(
    "gateway_wallet_bzz_balance", "Bee node BZZ balance"
)
wallet_xdai_balance = Gauge(
    "gateway_wallet_xdai_balance", "Bee node xDAI balance"
)
chequebook_available_balance = Gauge(
    "gateway_chequebook_available_balance", "Chequebook available BZZ balance"
)
base_eth_balance = Gauge(
    "gateway_base_eth_balance", "Base chain ETH balance for x402 gas"
)
stamp_pool_available = Gauge(
    "gateway_stamp_pool_available", "Available stamps in pool by size", ["size"]
)
stamp_pool_target = Gauge(
    "gateway_stamp_pool_target", "Target reserve by size", ["size"]
)
stamps_total = Gauge(
    "gateway_stamps_total", "Total stamps on Bee node"
)
stamp_min_ttl_seconds = Gauge(
    "gateway_stamp_min_ttl_seconds", "Lowest TTL among active stamps"
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

# ── Background task ──────────────────────────────────────────────────────────

_background_task = None
_start_time = None


async def _poll_balances():
    """Periodically poll wallet balances and update Prometheus gauges."""
    while True:
        try:
            # Update uptime
            if _start_time is not None:
                uptime_seconds.set(time.monotonic() - _start_time)

            # Wallet balances (requires HTTP client to be initialized)
            try:
                from app.services.swarm_api import get_wallet_info
                wallet = await get_wallet_info()
                bzz_raw = wallet.get("bzzBalance", "0")
                # BZZ balance is in PLUR (1 BZZ = 1e16 PLUR)
                bzz = int(bzz_raw) / 1e16 if bzz_raw else 0
                wallet_bzz_balance.set(bzz)
            except Exception as e:
                logger.debug(f"Metrics: failed to get wallet info: {e}")

            # Chequebook balance
            try:
                from app.services.swarm_api import get_chequebook_info
                cheque = await get_chequebook_info()
                available_raw = cheque.get("availableBalance", "0")
                available = int(available_raw) / 1e16 if available_raw else 0
                chequebook_available_balance.set(available)
            except Exception as e:
                logger.debug(f"Metrics: failed to get chequebook info: {e}")

            # Base ETH balance (only when x402 enabled)
            if settings.X402_ENABLED:
                try:
                    from app.x402.base_balance import check_base_eth_balance
                    base = await check_base_eth_balance()
                    base_eth_balance.set(base.get("balance_eth", 0))
                except Exception as e:
                    logger.debug(f"Metrics: failed to get base ETH balance: {e}")

            # Stamp counts and min TTL
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

            # Stamp pool state
            if settings.STAMP_POOL_ENABLED:
                try:
                    from app.services.stamp_pool import stamp_pool_manager
                    DEPTH_TO_SIZE = {17: "small", 20: "medium", 22: "large"}
                    status = stamp_pool_manager.get_status()
                    current_levels = status.get("current_levels", {})
                    reserve_config = status.get("reserve_config", {})
                    for depth, target in reserve_config.items():
                        size_name = DEPTH_TO_SIZE.get(int(depth), f"depth-{depth}")
                        stamp_pool_available.labels(size=size_name).set(
                            current_levels.get(int(depth), 0)
                        )
                        stamp_pool_target.labels(size=size_name).set(target)
                except Exception as e:
                    logger.debug(f"Metrics: failed to get pool status: {e}")

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
