# app/middleware/bee_gate.py
"""Per-request side of the Bee call gate (#588); see app/services/bee_gate.py.

For each HTTP request:

1. Marks the request's context, so its Bee calls take request slots rather
   than background ones.
2. On a paid route (x402 PROTECTED_ENDPOINTS), refuses a request that carries
   an X-PAYMENT header with 503 BEE_BUSY before anything runs, payment
   included, when no request slot is free at that moment. A request without
   a payment cannot be charged, so it goes on and gets its usual answer.
3. If one of THIS request's own Bee calls was refused by the gate, replaces
   an error response (status 400 or above) with 503 BEE_BUSY and Retry-After,
   which tells the client nothing was charged. Never when that may be untrue
   or hide what the client needs about a payment:
   - the request carries X-PAYMENT;
   - a state-changing Bee call (any method but GET and HEAD) of this request
     got a slot and was sent, whatever its outcome;
   - settlement of its payment was started (marked immediately before every
     facilitator settle call) or completed;
   - the response carries a payment header (X-Payment-Status,
     X-PAYMENT-RESPONSE, X-Payment-Transaction, X-Payment-Mode paid/failed);
   - its body has a payment, idempotency, delivery or purchase code, or x402
     fields.
   A response below 400 passes through unchanged, as does every response of a
   request none of whose Bee calls was refused.

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


# Headers of the replaced response that still hold for the 503: the caller's
# rate-limit state. Every other header of the replaced response is dropped
# (CORS headers are added outside this middleware, so they are kept).
_KEPT_HEADER_PREFIXES = (b"x-ratelimit-",)


async def send_busy(send: Send, replaced: Message = None) -> None:
    body = busy_body()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        (b"retry-after", str(settings.BEE_BUSY_RETRY_AFTER_SECONDS).encode()),
    ]
    if replaced is not None:
        headers += [(k, v) for k, v in replaced.get("headers", [])
                    if k.lower().startswith(_KEPT_HEADER_PREFIXES)]
    await send({
        "type": "http.response.start",
        "status": 503,
        "headers": headers,
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

        offers_payment = _offers_payment(scope)

        def may_replace() -> bool:
            # Never for a request that carries a payment, or whose payment
            # settlement was started or completed: the 503 says nothing was
            # charged, and its own answer (an outcome-unknown 502 with its
            # X-Payment-Status header, a settled-not-delivered error, a 409)
            # is what the client needs to avoid paying twice.
            # Nor once a state-changing Bee call (not GET or HEAD) was sent:
            # it may have spent the gateway's BZZ even if it failed, and the
            # refusal was not what made the request fail.
            return (state.refused and not offers_payment and not state.change_sent
                    and not state.committed and not state.payment_attempted)

        sent = {"start": False}
        held: dict = {"start": None, "body": []}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                sent["start"] = True
                if message["status"] >= 400 and may_replace() and not _has_payment_header(message):
                    # Hold it until the body shows whether it is about a payment.
                    held["start"] = message
                    return
                await send(message)
                return
            if message["type"] == "http.response.body" and held["start"] is not None:
                held["body"].append(message.get("body", b""))
                if message.get("more_body", False):
                    return
                start, body = held["start"], b"".join(held["body"])
                held["start"] = None
                if _is_payment_body(body) or not may_replace():
                    await send(start)
                    await send({"type": "http.response.body", "body": body})
                else:
                    await send_busy(send, replaced=start)
                return
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # Unhandled, so it would become a 500. If this request's own Bee
            # call was refused and nothing was sent yet, answer 503 instead.
            if may_replace() and not sent["start"]:
                logger.debug("Bee call refused by the gate; answering 503 BEE_BUSY")
                await send_busy(send)
                return
            raise


# Response headers the x402 code sets on answers about a payment.
_PAYMENT_HEADERS = (b"x-payment-status", b"x-payment-response", b"x-payment-transaction")
# Codes of answers about a payment or its delivery (docs/error-codes.md).
_PAYMENT_CODE_PREFIXES = ("PAYMENT_", "IDEMPOTENCY_", "DELIVERY_", "PURCHASE_")


def _has_payment_header(start: Message) -> bool:
    for name, value in start.get("headers", []):
        name = name.lower()
        if name in _PAYMENT_HEADERS:
            return True
        if name == b"x-payment-mode" and value.lower() in (b"paid", b"failed"):
            return True
    return False


def _is_payment_body(body: bytes) -> bool:
    """The error body is about a payment: a payment or settlement code, or x402 fields."""
    try:
        data = json.loads(body)
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    for part in (data, data.get("detail")):
        if not isinstance(part, dict):
            continue
        code = part.get("code")
        if isinstance(code, str) and code.startswith(_PAYMENT_CODE_PREFIXES):
            return True
        if any(k in part for k in ("x402_status", "x402Version", "transaction")):
            return True
    return False
