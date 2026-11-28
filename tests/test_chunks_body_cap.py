"""
POST /api/v1/chunks/ refuses an oversized body without buffering it (#696).

The endpoint used to call `request.body()`, which reads the whole body, before
comparing its length with CHUNK_UPLOAD_MAX_BYTES_PER_REQUEST. These tests drive
the production app over raw ASGI with a receive callable that counts what it
hands out, so they see exactly how much of the body the app pulled in.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.main import app
from app.services.bandwidth_credit import BandwidthCreditManager
from app.services.bandwidth_free_tier import BandwidthFreeTierTracker
from tests.asgi_body import CountingBody, call

VALID_STAMP = "a" * 226
CAP = 4104


def _settings(x402=False):
    ms = MagicMock()
    ms.CHUNK_UPLOAD_ENABLED = True
    ms.CHUNK_UPLOAD_MAX_BYTES_PER_REQUEST = CAP
    ms.X402_ENABLED = x402
    ms.CHUNK_UPLOAD_FREE_TIER_ENABLED = True
    ms.CHUNK_UPLOAD_FREE_TIER_MB_PER_DAY = 100
    ms.BANDWIDTH_CREDIT_MIN_TOPUP_MB = 100
    ms.X402_BANDWIDTH_USD_PER_GB = 0.10
    return ms


def _Body(total, piece, refuse=False):
    return CountingBody(total, piece, refuse=refuse)


def _call(receive, headers):
    return call(app, "/api/v1/chunks/", receive, headers)


def _code(body):
    return body["detail"]["code"]


def test_oversized_content_length_is_refused_without_reading_the_body():
    receive = _Body(12_000_000, 65536, refuse=True)
    with patch("app.api.endpoints.chunks.settings", _settings()):
        status, body = _call(receive, {"Swarm-Postage-Stamp": VALID_STAMP,
                                       "Content-Type": "application/octet-stream",
                                       "Content-Length": str(12_000_000)})
    assert status == 413
    assert _code(body) == "CHUNK_TOO_LARGE"
    assert receive.calls == 0


def test_content_length_one_over_the_cap_is_refused_without_reading():
    receive = _Body(CAP + 1, CAP + 1, refuse=True)
    with patch("app.api.endpoints.chunks.settings", _settings()):
        status, body = _call(receive, {"Swarm-Postage-Stamp": VALID_STAMP,
                                       "Content-Length": str(CAP + 1)})
    assert (status, _code(body), receive.calls) == (413, "CHUNK_TOO_LARGE", 0)


def test_chunked_body_over_the_cap_stops_at_cap_plus_one_byte():
    """No Content-Length (chunked transfer encoding): one-byte messages, so the
    bound is exact. The app stops at the first byte over the cap."""
    receive = _Body(1_000_000, 1)
    with patch("app.api.endpoints.chunks.settings", _settings()):
        status, body = _call(receive, {"Swarm-Postage-Stamp": VALID_STAMP,
                                       "Transfer-Encoding": "chunked"})
    assert (status, _code(body)) == (413, "CHUNK_TOO_LARGE")
    assert receive.sent == CAP + 1


def test_chunked_body_over_the_cap_reads_at_most_one_message_past_it():
    """With larger messages the read stops at the message that crosses the cap,
    so at most cap plus one message is held."""
    piece = 1000
    receive = _Body(12_000_000, piece)
    with patch("app.api.endpoints.chunks.settings", _settings()):
        status, body = _call(receive, {"Swarm-Postage-Stamp": VALID_STAMP,
                                       "Transfer-Encoding": "chunked"})
    assert (status, _code(body)) == (413, "CHUNK_TOO_LARGE")
    assert CAP < receive.sent <= CAP + piece


def test_understated_content_length_is_still_capped():
    receive = _Body(1_000_000, 1)
    with patch("app.api.endpoints.chunks.settings", _settings()):
        status, body = _call(receive, {"Swarm-Postage-Stamp": VALID_STAMP,
                                       "Content-Length": "10"})
    assert (status, _code(body)) == (413, "CHUNK_TOO_LARGE")
    assert receive.sent == CAP + 1


@pytest.mark.parametrize("headers", [
    pytest.param({"Content-Length": str(CAP)}, id="content-length"),
    pytest.param({"Transfer-Encoding": "chunked"}, id="chunked"),
])
def test_exact_cap_body_is_accepted(headers):
    receive = _Body(CAP, 1000)
    forward = AsyncMock(return_value="ab" * 32)
    with patch("app.api.endpoints.chunks.settings", _settings()), \
            patch("app.api.endpoints.chunks.upload_chunk_to_swarm", new=forward):
        status, body = _call(receive, {"Swarm-Postage-Stamp": VALID_STAMP, **headers})
    assert status == 201, body
    assert receive.sent == CAP
    assert len(forward.await_args.args[0]) == CAP


@pytest.mark.parametrize("headers", [
    pytest.param({"Content-Length": str(CAP + 1)}, id="content-length"),
    pytest.param({"Transfer-Encoding": "chunked"}, id="chunked"),
])
def test_refused_body_debits_no_credit(tmp_path, headers):
    ledger = BandwidthCreditManager(state_file=str(tmp_path / "credit.json"))
    ledger.credit("0xpayer", 1_000_000)
    token = ledger.issue_token("0xpayer")
    with patch("app.api.endpoints.chunks.settings", _settings(x402=True)), \
            patch("app.api.endpoints.chunks.bandwidth_credit_manager", ledger), \
            patch("app.api.endpoints.chunks.upload_chunk_to_swarm",
                  new=AsyncMock(side_effect=AssertionError("forwarded"))):
        status, body = _call(_Body(CAP + 1, 1), {"Swarm-Postage-Stamp": VALID_STAMP,
                                                 "X-Bandwidth-Credit-Token": token, **headers})
    assert (status, _code(body)) == (413, "CHUNK_TOO_LARGE")
    assert ledger.balance("0xpayer") == 1_000_000


@pytest.mark.parametrize("headers", [
    pytest.param({"Content-Length": str(CAP + 1)}, id="content-length"),
    pytest.param({"Transfer-Encoding": "chunked"}, id="chunked"),
])
def test_refused_body_uses_no_free_quota(headers):
    tracker = BandwidthFreeTierTracker()
    with patch("app.api.endpoints.chunks.settings", _settings(x402=True)), \
            patch("app.api.endpoints.chunks.free_tier_tracker", tracker), \
            patch("app.api.endpoints.chunks.upload_chunk_to_swarm",
                  new=AsyncMock(side_effect=AssertionError("forwarded"))):
        status, body = _call(_Body(CAP + 1, 1), {"Swarm-Postage-Stamp": VALID_STAMP,
                                                 "X-Payment-Mode": "free", **headers})
    assert (status, _code(body)) == (413, "CHUNK_TOO_LARGE")
    assert tracker._usage == {}


@pytest.mark.parametrize("content_type", ["application/json", "text/json", "application/x+json"])
def test_json_content_type_does_not_send_a_chunk_through_the_json_read(content_type):
    """BodyLimitMiddleware skipped the chunk path only without a Content-Type,
    so a JSON one with chunked encoding was read in full first (#696)."""
    piece = 1000
    receive = _Body(20_000_000, piece)
    with patch("app.api.endpoints.chunks.settings", _settings()):
        status, body = _call(receive, {"Swarm-Postage-Stamp": VALID_STAMP,
                                       "Content-Type": content_type,
                                       "Transfer-Encoding": "chunked"})
    assert (status, _code(body)) == (413, "CHUNK_TOO_LARGE")
    assert CAP < receive.sent <= CAP + piece


def test_malformed_content_length_does_not_lift_the_cap():
    """A Content-Length that is not a number is ignored, and the streamed read
    still stops at the first message over the cap.

    Uvicorn refuses such a request with 400 before the app sees it; this covers
    the app's own handling. Over raw ASGI the metrics instrumentator then
    raises ValueError on int("abc") after the response has been sent, so the
    exception is tolerated here only once the whole 413 is out.
    """
    import asyncio
    import json as _json
    from app.main import app as gateway

    piece = 1000
    receive = _Body(12_000_000, piece)
    out = {"status": None, "body": b"", "done": False}

    async def send(message):
        if message["type"] == "http.response.start":
            out["status"] = message["status"]
        elif message["type"] == "http.response.body":
            out["body"] += message.get("body", b"")
            out["done"] = not message.get("more_body", False)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": "/api/v1/chunks/",
        "raw_path": b"/api/v1/chunks/", "root_path": "", "query_string": b"",
        "headers": [(b"swarm-postage-stamp", VALID_STAMP.encode()), (b"content-length", b"abc")],
        "client": ("127.0.0.1", 50000), "server": ("testserver", 80),
    }

    async def run():
        try:
            await asyncio.wait_for(gateway(scope, receive, send), timeout=10)
        except ValueError:
            assert out["done"], "raised before the response was complete"

    with patch("app.api.endpoints.chunks.settings", _settings()):
        asyncio.run(run())
    assert out["status"] == 413
    assert _code(_json.loads(out["body"])) == "CHUNK_TOO_LARGE"
    assert CAP < receive.sent <= CAP + piece
