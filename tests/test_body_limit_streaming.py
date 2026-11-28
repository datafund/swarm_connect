"""
BodyLimitMiddleware stops reading a JSON body once it is over the limit (#696).

It used to call request.body(), which reads the whole body, and compare the
length afterwards. A JSON body with chunked transfer encoding (no
Content-Length, so the fast refusal does not apply) was buffered whatever its
size, on every JSON POST route.
"""
import json

from fastapi import FastAPI, Request

from app.core.config import settings
from app.main import app
from app.middleware.body_limit import BodyLimitMiddleware
from tests.asgi_body import CountingBody, call

PIECE = 65536
CHUNKED_JSON = {"Content-Type": "application/json", "Transfer-Encoding": "chunked"}


def _echo_app():
    echo = FastAPI()

    @echo.post("/echo")
    async def handler(request: Request):
        raw = await request.body()
        return {"length": len(raw), "parsed": json.loads(raw)}

    echo.add_middleware(BodyLimitMiddleware)
    return echo


def test_chunked_json_over_the_limit_is_refused_after_at_most_one_more_message():
    limit = settings.MAX_JSON_BODY_BYTES
    receive = CountingBody(20_000_000, PIECE)
    status, body = call(app, "/api/v1/stamps/", receive, CHUNKED_JSON)
    assert status == 413
    assert body["code"] == "BODY_TOO_LARGE"
    assert limit < receive.sent <= limit + PIECE


def test_content_length_over_the_limit_is_still_refused_unread():
    receive = CountingBody(20_000_000, PIECE, refuse=True)
    status, body = call(app, "/api/v1/stamps/", receive,
                        {"Content-Type": "application/json", "Content-Length": "20000000"})
    assert (status, body["code"], receive.calls) == (413, "BODY_TOO_LARGE", 0)


def _serve(data, piece):
    """A receive that serves `data` in `piece`-sized messages."""
    parts = [data[i:i + piece] for i in range(0, len(data), piece)] or [b""]
    state = {"i": 0}

    async def receive():
        i = state["i"]
        state["i"] += 1
        return {"type": "http.request", "body": parts[i], "more_body": i + 1 < len(parts)}

    return receive


def test_chunked_json_within_the_limit_reaches_the_route_unchanged():
    payload = json.dumps({"items": ["x" * 100] * 50}).encode()
    status, body = call(_echo_app(), "/echo", _serve(payload, 97), CHUNKED_JSON)
    assert status == 200
    assert body == {"length": len(payload), "parsed": json.loads(payload)}


def test_content_length_json_within_the_limit_reaches_the_route_unchanged():
    payload = json.dumps({"a": [1, 2, {"b": "c"}]}).encode()
    status, body = call(_echo_app(), "/echo", _serve(payload, len(payload)),
                        {"Content-Type": "application/json", "Content-Length": str(len(payload))})
    assert status == 200
    assert body == {"length": len(payload), "parsed": json.loads(payload)}


def test_body_exactly_at_the_limit_reaches_the_route(monkeypatch):
    monkeypatch.setattr(settings, "MAX_JSON_BODY_BYTES", 1000)
    payload = b'"' + b"x" * 998 + b'"'
    status, body = call(_echo_app(), "/echo", _serve(payload, 100), CHUNKED_JSON)
    assert status == 200
    assert body["length"] == 1000


def test_body_one_byte_over_the_limit_is_refused(monkeypatch):
    monkeypatch.setattr(settings, "MAX_JSON_BODY_BYTES", 1000)
    receive = CountingBody(1001, 1)
    status, body = call(_echo_app(), "/echo", receive, CHUNKED_JSON)
    assert (status, body["code"], receive.sent) == (413, "BODY_TOO_LARGE", 1001)


def test_depth_check_still_applies_to_a_chunked_body():
    depth = settings.MAX_JSON_DEPTH + 5
    deep = b"[" * depth + b"]" * depth
    status, body = call(_echo_app(), "/echo", _serve(deep, 7), CHUNKED_JSON)
    assert (status, body["code"]) == (400, "JSON_TOO_DEEP")
