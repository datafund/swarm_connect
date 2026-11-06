"""httpx2's request log line carries no server-controlled text (#608).

httpx2 logs `HTTP Request: GET <url> "HTTP/1.1 <status> <reason phrase>"` at
INFO; the gateway logs at INFO and the deploy job prints the container log
into its public log. app.core.http_log keeps method, path and status only.
"""
import asyncio
import logging
import socket
import threading

import httpx2
import pytest

# Imported for its side effect too: app.main installs the filter, as under uvicorn.
from app import main
from app.core import http_log
from app.core.config import settings
from app.x402 import check_config, facilitator
from app.x402.facilitator_client import FacilitatorClient

MARKER = "LEAKMARK"
HOSTILE_REASON = (MARKER + " " + "R" * (90 * 1024)).encode() + b"\r\xe9\xff obs" + MARKER.encode()


def _log_text(caplog):
    return "\n".join(r.getMessage() for r in caplog.records)


@pytest.fixture
def x402(monkeypatch):
    monkeypatch.setattr(settings, "X402_ENABLED", True)
    monkeypatch.setattr(settings, "X402_NETWORK", "base-sepolia")
    monkeypatch.setattr(settings, "X402_PAY_TO_ADDRESS", "0x" + "ab" * 20)
    monkeypatch.setattr(settings, "X402_FACILITATOR_URL", "https://facilitator.example/x402")
    monkeypatch.setattr(settings, "X402_FACILITATOR_CDP_API_KEY_ID", None)
    monkeypatch.setattr(settings, "X402_FACILITATOR_BEARER_TOKEN", None)
    monkeypatch.setattr(settings, "X402_PROTOCOL_VERSIONS", "1,2")
    facilitator.reset_facilitator_client()
    yield monkeypatch
    facilitator.reset_facilitator_client()


def test_app_main_installed_the_filter():
    assert any(isinstance(f, http_log.Httpx2RequestLogFilter) for f in logging.getLogger("httpx2").filters)
    for name in ("httpcore2.http11", "httpcore2.http2"):
        assert any(isinstance(f, http_log.Httpcore2HeadersLogFilter)
                   for f in logging.getLogger(name).filters)


def test_install_is_idempotent():
    before = len(logging.getLogger("httpx2").filters)
    http_log.install()
    http_log.install()
    assert len(logging.getLogger("httpx2").filters) == before


def _mock_client(status, reason=None):
    def handler(request):
        ext = {"reason_phrase": reason} if reason is not None else {}
        return httpx2.Response(status, json={"kinds": []}, extensions=ext)
    return FacilitatorClient({"url": "https://facilitator.example/x402"},
                             transport=httpx2.MockTransport(handler))


def test_a_hostile_reason_phrase_never_reaches_the_log_through_the_lifespan(x402, caplog):
    x402.setattr(facilitator, "_client", _mock_client(999, HOSTILE_REASON))
    x402.setattr(facilitator, "validate_x402_config", lambda: None)

    async def run():
        async with main.lifespan(main.app):
            pass

    with caplog.at_level(logging.INFO), pytest.raises(RuntimeError, match="answered HTTP 999"):
        asyncio.run(run())
    text = _log_text(caplog)
    assert "HTTP Request: GET /x402/supported 999" in text
    assert MARKER not in text and "RRRR" not in text
    assert "\r" not in text and "\xe9" not in text


class _RawServer:
    """A TCP server answering every request with one fixed raw HTTP response."""

    def __init__(self, raw: bytes):
        self.raw = raw
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                conn.recv(65536)
                conn.sendall(self.raw)

    def close(self):
        self.sock.close()


def test_a_hostile_reason_phrase_on_the_wire_never_reaches_the_log(x402, caplog):
    """Through a real socket and httpcore2's HTTP/1.1 parser: obs-text and a
    90 KB reason phrase, as the reviewer's stub sent."""
    reason = (MARKER + " " + "R" * (90 * 1024)).encode() + b" \xe9\xff " + MARKER.encode()
    body = b'{"kinds": []}'
    server = _RawServer(b"HTTP/1.1 999 " + reason + b"\r\nContent-Length: " + str(len(body)).encode()
                        + b"\r\nConnection: close\r\n\r\n" + body)
    try:
        client = FacilitatorClient({"url": f"http://127.0.0.1:{server.port}/x402"})
        with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError, match="answered HTTP 999"):
            asyncio.run(facilitator.check_facilitator_v2_support(client))
    finally:
        server.close()
    text = _log_text(caplog)
    assert "HTTP Request: GET /x402/supported 999" in text
    assert MARKER not in text and "RRRR" not in text
    # httpcore2's DEBUG header trace is redacted too.
    assert "receive_response_headers.complete (headers redacted)" in text


def test_deploy_check_installs_the_filter_and_keeps_the_log_clean(x402, caplog, capsys):
    x402.setattr(facilitator, "_client", _mock_client(999, HOSTILE_REASON))
    with caplog.at_level(logging.INFO):
        assert check_config.main() == check_config.EXIT_REFUSED
    text = _log_text(caplog) + capsys.readouterr().err
    assert MARKER not in text and "HTTP Request: GET /x402/supported 999" in _log_text(caplog)


def test_a_normal_request_still_logs_method_path_and_status(caplog):
    async def go():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(
                lambda r: httpx2.Response(201, json={}))) as c:
            await c.post("http://bee.example:1633/stamps/1000/17?label=x&immutable=false", json={})
            await c.get("http://bee.example:1633/health")

    with caplog.at_level(logging.INFO, logger="httpx2"):
        asyncio.run(go())
    lines = [r.getMessage() for r in caplog.records if r.name == "httpx2"]
    assert lines == ["HTTP Request: POST /stamps/1000/17 201", "HTTP Request: GET /health 201"]


def test_other_httpx2_records_pass_unchanged():
    f = http_log.Httpx2RequestLogFilter()
    rec = logging.LogRecord("httpx2", logging.INFO, __file__, 1, "something else %s", ("x",), None)
    assert f.filter(rec) and rec.getMessage() == "something else x"


def test_a_long_path_is_capped():
    rec = logging.LogRecord("httpx2", logging.INFO, __file__, 1, 'HTTP Request: %s %s "%s %d %s"',
                            ("GET", httpx2.URL("http://h/" + "a" * 5000 + "?q=1"), "HTTP/1.1", 200, "OK"),
                            None)
    http_log.Httpx2RequestLogFilter().filter(rec)
    msg = rec.getMessage()
    assert msg.startswith("HTTP Request: GET /aaaa") and msg.endswith("... 200")
    assert len(msg) < http_log.MAX_PATH_CHARS + 40
