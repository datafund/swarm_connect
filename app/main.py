# app/main.py
from fastapi import FastAPI
from app.core.config import settings
from app.api.endpoints import stamps, data, wallet
import logging

# Configure basic logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json" # Standard location for OpenAPI spec
)

# Include the API router(s)
# The prefix ensures all routes start with /api/v1
app.include_router(stamps.router, prefix=f"{settings.API_V1_STR}/stamps", tags=["stamps"])
app.include_router(data.router, prefix=f"{settings.API_V1_STR}/data", tags=["data"])
app.include_router(wallet.router, prefix=f"{settings.API_V1_STR}", tags=["wallet"])

@app.get("/", summary="Health Check", tags=["default"])
def read_root():
    """ Basic health check endpoint. """
    logger.info("Root endpoint '/' accessed.")
    return {"status": "ok", "message": f"Welcome to {settings.PROJECT_NAME}"}

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
