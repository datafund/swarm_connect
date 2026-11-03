# app/middleware/bee_gate.py
"""Per-request side of the Bee call gate (#588); see app/services/bee_gate.py.

For each HTTP request:

1. Marks the request's context, so its Bee calls take request slots rather
   than background ones.
2. On a paid route (x402 PROTECTED_ENDPOINTS), refuses a request that carries
   an X-PAYMENT header with 503 BEE_BUSY before anything runs, payment
   included, when no request slot is free at that moment. A request without
   a payment cannot be charged, so it goes on and gets its usual answer.
3. If one of THIS request's own Bee calls was refused by the gate, and its
   payment has not settled, replaces an error response (status 400 or above)
   with 503 BEE_BUSY and Retry-After. A response below 400 passes through
   unchanged, as does every response of a request none of whose Bee calls was
   refused. A request whose payment settled keeps its own response, which
   carries what the client needs about the payment.

Plain ASGI, not BaseHTTPMiddleware (see ClientMetricsMiddleware for why).
With BEE_CALL_GATE_ENABLED false it passes everything through untouched.
"""
import json
import logging

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import settings
from app.services import bee_gate

logger = logging.getLogger(__name__)

BEE_BUSY_MESSAGE = ("The gateway is serving as many Bee node requests as it can. "
                    "Nothing was charged; retry after the Retry-After delay.")


def busy_body() -> bytes:
    """The error envelope of app.main.error_envelope, for 503 BEE_BUSY."""
    detail = {"code": "BEE_BUSY", "message": BEE_BUSY_MESSAGE}
    return json.dumps({"detail": detail, "code": detail["code"], "message": BEE_BUSY_MESSAGE}).encode()


async def send_busy(send: Send) -> None:
    body = busy_body()
    await send({
        "type": "http.response.start",
        "status": 503,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"retry-after", str(settings.BEE_BUSY_RETRY_AFTER_SECONDS).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})


def _is_paid_route(method: str, path: str) -> bool:
    if not settings.X402_ENABLED:
        return False
    from app.x402.middleware import is_protected_endpoint
    return is_protected_endpoint(method, path)


def _offers_payment(scope: Scope) -> bool:
    """The request carries an x402 payment, so it could be charged."""
    return any(name.lower() == b"x-payment" for name, _ in scope.get("headers", []))


class BeeGateMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not settings.BEE_CALL_GATE_ENABLED:
            await self.app(scope, receive, send)
            return

        state = bee_gate.begin_request()

        if (_is_paid_route(scope.get("method", ""), scope.get("path", ""))
                and _offers_payment(scope)
                and bee_gate.bee_call_gate.request_pool_full()):
            bee_gate._count_rejection("admission")
            await send_busy(send)
            return

        started = {"replaced": False, "sent": False}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                if state.refused and not state.committed and message["status"] >= 400:
                    started["replaced"] = True
                    started["sent"] = True
                    await send_busy(send)
                    return
                started["sent"] = True
            elif message["type"] == "http.response.body" and started["replaced"]:
                return  # the replaced response's body
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # Unhandled, so it would become a 500. If this request's own Bee
            # call was refused and nothing was sent yet, answer 503 instead.
            if state.refused and not state.committed and not started["sent"]:
                logger.debug("Bee call refused by the gate; answering 503 BEE_BUSY")
                await send_busy(send)
                return
            raise
