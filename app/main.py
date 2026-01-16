# app/main.py
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from app.core.config import settings
from app.core.version import VERSION
from app.api.endpoints import stamps, data, wallet
import logging

# Configure basic logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title=settings.PROJECT_NAME,
    version=VERSION,
    openapi_url=f"{settings.API_V1_STR}/openapi.json" # Standard location for OpenAPI spec
)

# Include the API router(s)
# The prefix ensures all routes start with /api/v1
app.include_router(stamps.router, prefix=f"{settings.API_V1_STR}/stamps", tags=["stamps"])
app.include_router(data.router, prefix=f"{settings.API_V1_STR}/data", tags=["data"])
app.include_router(wallet.router, prefix=f"{settings.API_V1_STR}", tags=["wallet"])

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
            "gnosis_wallet": {
                "can_accept": gnosis.get("can_accept"),
                "xbzz_ok": gnosis.get("xbzz_ok"),
                "xdai_ok": gnosis.get("xdai_ok"),
                "chequebook_ok": gnosis.get("chequebook_ok"),
                "balances": gnosis.get("balances"),
            },
            "warnings": warnings,
            "errors": errors,
        }

        # Return 503 if critical
        if response_data["status"] == "critical":
            return JSONResponse(status_code=503, content=response_data)

    return response_data

# --- Placeholder for Future Enhancements ---

# TODO: Add CORS middleware if the API needs to be accessed from browser frontends on different domains
# from fastapi.middleware.cors import CORSMiddleware
# origins = [
#     "http://localhost",
#     "http://localhost:8080",
#     # Add your frontend domains here
# ]
# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=origins,
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )

# TODO: Add Security Dependencies when implementing Auth
# (e.g., app.include_router(..., dependencies=[Depends(verify_api_key)]))

# TODO: Add HTTPS handling - typically done via a reverse proxy (Nginx, Caddy) in production.
# Uvicorn can handle it directly for dev/testing (--ssl-keyfile, --ssl-certfile)
