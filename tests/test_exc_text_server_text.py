"""Log lines name an httpx2 error by its type and status, not its text (#659).

str() of an httpx2 HTTPStatusError holds the server's reason phrase and the
full request URL with its query, which can carry an RPC or price-feed key;
str() of a RemoteProtocolError quotes the server's bytes ("illegal status
line: ..."). exc_text() shortens both, and the Bee and RPC log sites that
formatted such an error with {e} use it. The deploy job prints the
container log into its public log.
"""
import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import main
from app.core.config import settings
from app.services import metrics
from app.services.http_client import exc_text
from tests.test_paid_purchase_lost_response import _run_pending, _slow_bee, env  # noqa: F401
from tests.test_pricing_endpoint import x402_on  # noqa: F401

MARKER = "LEAKMARK"
PLANTED = "0123abcdKEY4567"  # a made-up key
BEE_URL = "http://bee.internal:1633"


def _messages(caplog, name=None):
    return [r.getMessage() for r in caplog.records if name is None or r.name.startswith(name)]


def _status_error(status=503, url=f"{BEE_URL}/chainstate?apikey={PLANTED}"):
    """An HTTPStatusError as raise_for_status() makes it, with a hostile reason phrase."""
    request = httpx2.Request("GET", url)
    response = httpx2.Response(status, request=request,
                               extensions={"reason_phrase": f"{MARKER} reason".encode()})
    try:
        response.raise_for_status()
    except httpx2.HTTPStatusError as e:
        return e
    raise AssertionError("raise_for_status did not raise")


# --------------------------------------------------------------------------- #
# exc_text(): exception text for log lines
# --------------------------------------------------------------------------- #
def test_exc_text_of_a_status_error_is_type_and_status():
    text = exc_text(_status_error(503))
    assert text == "HTTPStatusError: HTTP 503"


def test_exc_text_drops_server_bytes_of_a_protocol_error():
    e = httpx2.RemoteProtocolError(f"illegal status line: bytearray(b'HTTP/1.1 {MARKER}')")
    assert exc_text(e) == "RemoteProtocolError"
    assert exc_text(httpx2.LocalProtocolError(f"bad {MARKER}")) == "LocalProtocolError"
    assert exc_text(httpx2.DecodingError(f"bad gzip {MARKER}")) == "DecodingError"


@pytest.mark.parametrize("message", ["Server disconnected without sending a response.",
                                     "Server disconnected"])
def test_exc_text_keeps_httpcore2s_fixed_protocol_messages(message):
    assert exc_text(httpx2.RemoteProtocolError(message)) == f"RemoteProtocolError: {message}"


def test_exc_text_of_other_exceptions_is_unchanged():
    assert exc_text(httpx2.ReadTimeout("")) == "ReadTimeout"
    assert exc_text(httpx2.ConnectError("All connection attempts failed")) == \
        "ConnectError: All connection attempts failed"
    assert exc_text(ValueError("no reference")) == "ValueError: no reference"


# --------------------------------------------------------------------------- #
# Log sites that formatted a Bee or RPC error with {e}
# --------------------------------------------------------------------------- #
def _assert_clean(lines):
    text = "\n".join(lines)
    for leak in (MARKER, PLANTED, "bee.internal", "1633", "price.example"):
        assert leak not in text, text


def test_price_feed_status_error_logs_type_and_status_only(monkeypatch, caplog):
    monkeypatch.setattr(metrics, "_last_price_fetch", None)
    monkeypatch.setattr(metrics, "_price_feed_failures", 0)
    monkeypatch.setattr(settings, "X402_BZZ_PRICE_FEED_URL",
                        f"https://price.example/simple?ids=swarm-bzz&x_cg_key={PLANTED}")

    def handler(request):
        return httpx2.Response(503, text="down", extensions={"reason_phrase": f"{MARKER} x".encode()})

    real = httpx2.AsyncClient
    with patch("httpx2.AsyncClient", lambda **kw: real(transport=httpx2.MockTransport(handler), **kw)), \
            caplog.at_level(logging.WARNING, logger=metrics.logger.name):
        asyncio.run(metrics._update_bzz_rates())
    lines = [m for m in _messages(caplog, metrics.logger.name) if "price feed" in m]
    assert lines == ["BZZ price feed read failed (1 in a row): HTTPStatusError: HTTP 503"]
    _assert_clean(lines)


def test_chunk_forward_failure_logs_no_bee_text(caplog):
    ms = MagicMock()
    ms.CHUNK_UPLOAD_ENABLED = True
    ms.CHUNK_UPLOAD_MAX_BYTES_PER_REQUEST = 4104
    ms.X402_ENABLED = False
    with patch("app.api.endpoints.chunks.settings", ms), \
            patch("app.api.endpoints.chunks.upload_chunk_to_swarm",
                  new=AsyncMock(side_effect=_status_error(500, f"{BEE_URL}/chunks"))), \
            caplog.at_level(logging.ERROR):
        r = TestClient(main.app).post("/api/v1/chunks/", content=b"data",
                                      headers={"Swarm-Postage-Stamp": "a" * 226})
    assert r.status_code == 502
    lines = [m for m in _messages(caplog) if "forward chunk" in m]
    assert lines == ["Failed to forward chunk to Swarm API: HTTPStatusError: HTTP 500"]
    _assert_clean(lines)


def test_chunk_forward_protocol_error_logs_no_server_bytes(caplog):
    ms = MagicMock()
    ms.CHUNK_UPLOAD_ENABLED = True
    ms.CHUNK_UPLOAD_MAX_BYTES_PER_REQUEST = 4104
    ms.X402_ENABLED = False
    err = httpx2.RemoteProtocolError(f"illegal status line: bytearray(b'HTTP/1.1 {MARKER}')")
    with patch("app.api.endpoints.chunks.settings", ms), \
            patch("app.api.endpoints.chunks.upload_chunk_to_swarm", new=AsyncMock(side_effect=err)), \
            caplog.at_level(logging.ERROR):
        r = TestClient(main.app).post("/api/v1/chunks/", content=b"data",
                                      headers={"Swarm-Postage-Stamp": "a" * 226})
    assert r.status_code == 502
    lines = [m for m in _messages(caplog) if "forward chunk" in m]
    assert lines == ["Failed to forward chunk to Swarm API: RemoteProtocolError"]


def test_pricing_chainstate_failure_logs_no_bee_text(x402_on, caplog):  # noqa: F811
    from app.api.endpoints import pricing
    app = FastAPI()
    app.include_router(pricing.router, prefix="/api/v1")
    with patch("app.services.swarm_api.get_chainstate", new=AsyncMock(side_effect=_status_error(502))), \
            caplog.at_level(logging.ERROR):
        r = TestClient(app).get("/api/v1/pricing")
    assert r.status_code == 503
    lines = [m for m in _messages(caplog) if "chain price" in m]
    assert lines == ["Pricing: failed to read the chain price: HTTPStatusError: HTTP 502"]
    _assert_clean(lines)


def test_for_owner_chainstate_failure_logs_no_bee_text(caplog):
    from tests.test_stamps_for_owner import OWNER, _settings
    with patch("app.api.endpoints.stamps_for_owner.settings", _settings()), \
            patch("app.services.swarm_api.get_chainstate", new=AsyncMock(side_effect=_status_error(500))), \
            caplog.at_level(logging.ERROR):
        r = TestClient(main.app).post("/api/v1/stamps/for-owner", json={"owner": OWNER, "size": "small"})
    assert r.status_code == 502
    lines = [m for m in _messages(caplog) if "chainstate" in m]
    assert lines == ["for-owner: failed to read chainstate: HTTPStatusError: HTTP 500"]
    _assert_clean(lines)


def test_bee_refusal_of_a_paid_purchase_logs_no_bee_text(env, monkeypatch, caplog):  # noqa: F811
    app, fac, buy, find = env
    monkeypatch.setattr(settings, "SWARM_STAMP_PURCHASE_TIMEOUT_SECONDS", 0.05)
    buy.side_effect = _slow_bee(_status_error(400, f"{BEE_URL}/stamps/1/17?label={PLANTED}"))
    with caplog.at_level(logging.ERROR):
        first, retry = _run_pending(app, None, key="k-refused-logged")
    assert first.status_code == 202
    lines = [m for m in _messages(caplog) if "Bee refused a paid purchase" in m]
    assert len(lines) == 1 and lines[0].endswith("): HTTPStatusError: HTTP 400")
    _assert_clean(lines)


def test_late_delivery_expiry_read_logs_no_bee_text(env, caplog):  # noqa: F811
    """The price read for a late-delivered batch's expiry is a Bee call; it
    logged repr(e), which holds the reason phrase and the URL with its query."""
    from app.services import swarm_api
    from tests.test_paid_purchase_lost_response import BATCH
    app, fac, buy, find = env
    late = {"now": False}
    chainstate = {"currentPrice": "24000", "minimumValidityBlocks": 17280, "block": 180000}

    async def get_chainstate(*args, **kwargs):
        if late["now"]:
            raise _status_error(503)
        return chainstate

    async def slow_buy(**kwargs):
        await asyncio.sleep(0.2)
        late["now"] = True
        return BATCH

    buy.side_effect = slow_buy
    with patch.object(settings, "SWARM_STAMP_PURCHASE_TIMEOUT_SECONDS", 0.05), \
            patch("app.services.swarm_api.get_chainstate", new=get_chainstate), \
            caplog.at_level(logging.WARNING):
        first, retry = _run_pending(app, None, key="k-late-expiry-logged")
    assert first.status_code == 202 and retry.status_code == 201
    assert retry.json()["expires_at"] is None
    lines = [m for m in _messages(caplog) if "No expiry for late-delivered batch" in m]
    assert len(lines) == 1 and lines[0].endswith(": HTTPStatusError: HTTP 503"), lines
    _assert_clean(lines)
    assert swarm_api.get_chainstate is not get_chainstate  # the patch is undone


def test_pool_allowance_refusal_price_failure_logs_no_bee_text(monkeypatch, tmp_path, caplog):
    """Pricing the 402 for a spent pool allowance reads Bee's chainstate; when
    that fails the 429 is answered and the failure logged without Bee's text."""
    from tests.test_pool_allowance_refusal_metric import APP, _acquire
    from app.api.models.stamp import SIZE_PRESETS
    from app.services import pool_allowance
    from app.services.pool_allowance import PoolAllowanceTracker
    import app.api.endpoints.pool as pool_ep
    monkeypatch.setattr(settings, "STAMP_POOL_ENABLED", True)
    monkeypatch.setattr(settings, "X402_ENABLED", True)
    monkeypatch.setattr(settings, "X402_NETWORK", "base-sepolia")
    monkeypatch.setattr(settings, "X402_ALLOW_TESTNET_PAID_BYPASS", True)  # so the 402 is offered
    monkeypatch.setattr(settings, "X402_PAY_TO_ADDRESS", "0x" + "ab" * 20)
    monkeypatch.setattr(settings, "POOL_DAILY_ALLOWANCES", f"{APP}=0")
    monkeypatch.setattr(settings, "POOL_DEFAULT_DAILY_ALLOWANCE", 0)
    tracker = PoolAllowanceTracker(state_file=str(tmp_path / "a.json"))
    monkeypatch.setattr(pool_allowance, "pool_allowance_tracker", tracker)
    monkeypatch.setattr(pool_ep, "pool_allowance_tracker", tracker)
    monkeypatch.setattr(pool_ep.stamp_pool_manager, "get_available_stamp",
                        lambda d: type("S", (), {"depth": SIZE_PRESETS["small"]})())
    with patch("app.services.swarm_api.get_chainstate", new=AsyncMock(side_effect=_status_error(503))), \
            caplog.at_level(logging.WARNING):
        resp = _acquire({"Origin": APP})
    assert resp.status_code == 429, resp.text
    lines = [m for m in _messages(caplog) if "Could not price the allowance refusal" in m]
    assert lines == ["Could not price the allowance refusal, answering 429: HTTPStatusError: HTTP 503"], \
        _messages(caplog)
    _assert_clean(lines)
