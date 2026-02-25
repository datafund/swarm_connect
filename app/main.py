# app/main.py
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from app.core.config import settings
from app.core.version import VERSION
from app.api.endpoints import stamps, data, wallet, pool, notary
import logging

# Configure basic logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler for startup/shutdown tasks."""
    # === Startup ===
    # Start stamp pool background task if enabled
    if settings.STAMP_POOL_ENABLED:
        from app.services.stamp_pool import stamp_pool_manager
        logger.info("Starting stamp pool background task")
        await stamp_pool_manager.start_background_task()

    yield

    # === Shutdown ===
    # Stop stamp pool background task if running
    if settings.STAMP_POOL_ENABLED:
        from app.services.stamp_pool import stamp_pool_manager
        logger.info("Stopping stamp pool background task")
        await stamp_pool_manager.stop_background_task()


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=VERSION,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",  # Standard location for OpenAPI spec
    lifespan=lifespan
)

# Add x402 payment middleware if enabled (must be added before CORS)
if settings.X402_ENABLED:
    from app.x402.middleware import X402Middleware
    app.add_middleware(X402Middleware)
    logger.info("x402 middleware enabled")

# Add CORS middleware for browser-based SDK usage
# IMPORTANT: Add CORS last so it wraps all other middleware.
# This ensures CORS headers are added to ALL responses, including 402s from x402.
cors_origins = settings.get_cors_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=settings.CORS_ALLOW_CREDENTIALS,
    allow_methods=["*"],
    allow_headers=["*"],
)
logger.info(f"CORS enabled for origins: {cors_origins}")

# Include the API router(s)
# The prefix ensures all routes start with /api/v1
app.include_router(stamps.router, prefix=f"{settings.API_V1_STR}/stamps", tags=["stamps"])
app.include_router(data.router, prefix=f"{settings.API_V1_STR}/data", tags=["data"])
app.include_router(wallet.router, prefix=f"{settings.API_V1_STR}", tags=["wallet"])
app.include_router(pool.router, prefix=f"{settings.API_V1_STR}/pool", tags=["pool"])
app.include_router(notary.router, prefix=f"{settings.API_V1_STR}/notary", tags=["notary"])

@app.get("/", summary="Health Check", tags=["default"])
@app.get("/health", summary="Health Check", tags=["default"], include_in_schema=False)
def read_root():
    """
    Health check endpoint.

    When X402_ENABLED=true, includes x402 wallet status:
    - status: "ok" | "degraded" | "critical"
    - x402: detailed wallet balances and warnings

    Returns 503 when x402 is enabled and gateway wallet is critically low on ETH.
    """
    logger.info("Health check endpoint accessed.")

    response_data = {
        "status": "ok",
        "message": f"Welcome to {settings.PROJECT_NAME}"
    }

    # If x402 is enabled, include wallet status
    if settings.X402_ENABLED:
        from app.x402.base_balance import check_base_eth_balance
        from app.x402.preflight import check_preflight_balances

        base_eth = check_base_eth_balance()
        gnosis = check_preflight_balances()

        warnings = []
        errors = []

        # Collect warnings and errors
        if base_eth.get("warning"):
            if base_eth.get("is_critical"):
                errors.append(base_eth["warning"])
            else:
                warnings.append(base_eth["warning"])

        warnings.extend(gnosis.get("warnings", []))
        errors.extend(gnosis.get("errors", []))

        # Determine overall status
        # Never return 503 — the gateway should always be reachable so
        # operators can see what's wrong. Docker healthcheck and reverse
        # proxy rely on 200; returning 503 causes a cascading failure
        # where the container is marked unhealthy and users see nothing.
        if base_eth.get("is_critical") or len(errors) > 0:
            response_data["status"] = "critical"
        elif not base_eth["ok"] or not gnosis["can_accept"]:
            response_data["status"] = "degraded"

        # Add x402 details
        response_data["x402"] = {
            "enabled": True,
            "base_wallet": {
                "address": base_eth.get("address"),
                "balance_eth": base_eth.get("balance_eth"),
                "threshold_eth": base_eth.get("threshold_eth"),
                "critical_eth": base_eth.get("critical_eth"),
                "ok": base_eth.get("ok"),
                "is_critical": base_eth.get("is_critical"),
            },
            "bee_gnosis_wallet": {
                "wallet_address": gnosis.get("wallet_address"),
                "chequebook_address": gnosis.get("chequebook_address"),
                "can_accept": gnosis.get("can_accept"),
                "xbzz_ok": gnosis.get("xbzz_ok"),
                "xdai_ok": gnosis.get("xdai_ok"),
                "chequebook_ok": gnosis.get("chequebook_ok"),
                "balances": gnosis.get("balances"),
            },
            "warnings": warnings,
            "errors": errors,
        }

    return response_data

# --- Placeholder for Future Enhancements ---

# TODO: Add Security Dependencies when implementing Auth
# (e.g., app.include_router(..., dependencies=[Depends(verify_api_key)]))

# TODO: Add HTTPS handling - typically done via a reverse proxy (Nginx, Caddy) in production.
# Uvicorn can handle it directly for dev/testing (--ssl-keyfile, --ssl-certfile)
