"""IP blocklist for the whole gateway (#379).

X402_BLACKLIST_IPS (IPs and CIDR ranges, comma-separated) was documented as a
way to block an abusive client, but nothing read it. This middleware refuses
every request from a listed address with 403, before any other work. Changing
the list takes a restart; for an immediate block use the proxy (see the
mainnet runbook).
"""
import logging

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.core.client_ip import get_client_ip
from app.x402.access import ip_matches_list, parse_ip_list

logger = logging.getLogger(__name__)


class AccessListMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, blocked: str):
        super().__init__(app)
        self._blocked = parse_ip_list(blocked)

    async def dispatch(self, request: Request, call_next):
        ip = get_client_ip(request)
        if self._blocked and ip_matches_list(ip, self._blocked):
            logger.warning(f"Refused blocked address {ip}: {request.method} {request.url.path}")
            return JSONResponse(status_code=403, content={
                "code": "ACCESS_BLOCKED", "message": "Access from this address is blocked."})
        return await call_next(request)
