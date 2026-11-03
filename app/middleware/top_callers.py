# app/middleware/top_callers.py
"""Feed every request to the top-caller tracker (#588).

Counts / and /health too: those carried the 2026-11-03 flood. Skips /metrics,
which only the monitoring scraper calls. Plain ASGI; never fails a request.
"""
import logging

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.client_ip import client_key
from app.middleware.client_metrics import _caller_from_scope
from app.services.top_callers import top_caller_tracker

logger = logging.getLogger(__name__)

SKIPPED_PATHS = frozenset({"/metrics"})


class TopCallersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in SKIPPED_PATHS:
            await self.app(scope, receive, send)
            return

        status_holder = {"code": 0}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            try:
                caller = client_key(_caller_from_scope(scope, Headers(scope=scope)) or "unknown")
                top_caller_tracker.observe(caller, status_holder["code"] or 500, scope.get("path", ""))
            except Exception as e:
                logger.debug("Could not record a top-caller observation: %s", e)
