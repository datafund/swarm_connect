"""The outbound-HTTP log filter keeps URL-path secrets and server bytes out (#659).

#608 rewrote httpx2's INFO request line to method, path and status. This
covers what was left in app/core/http_log.py: a key in the path of a
configured URL, and httpcore2's DEBUG ".failed" trace lines, whose exception
repr quotes server bytes. The deploy job prints the container log into its
public log. Exception text at the gateway's own log sites is in
tests/test_exc_text_server_text.py.
"""
import asyncio
import logging

import httpx2
import pytest

# Imported for its side effect too: app.main installs the log filters.
from app import main  # noqa: F401
from app.core import http_log
from app.core.config import settings
from app.services import bee_errors
from tests.test_http_log_filter import _RawServer

MARKER = "LEAKMARK"
PLANTED = "0123abcdKEY4567"  # a made-up key: mixed case and digits, as _looks_secret collects
BEE_URL = "http://bee.internal:1633"


def _messages(caplog, name=None):
    return [r.getMessage() for r in caplog.records if name is None or r.name.startswith(name)]


def _request_record(url):
    return logging.LogRecord("httpx2", logging.INFO, __file__, 1, 'HTTP Request: %s %s "%s %d %s"',
                             ("POST", httpx2.URL(url), "HTTP/1.1", 200, "OK"), None)


# --------------------------------------------------------------------------- #
# The INFO request line: secret path segments of configured URLs
# --------------------------------------------------------------------------- #
def test_a_configured_rpc_url_key_in_the_path_is_redacted(monkeypatch, caplog):
    monkeypatch.setattr(settings, "BASE_RPC_URL", f"https://rpc.example/v2/{PLANTED}")

    async def go():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(
                lambda r: httpx2.Response(200, json={"result": "0x0"}))) as c:
            await c.post(settings.BASE_RPC_URL, json={"method": "eth_getBalance"})

    with caplog.at_level(logging.INFO, logger="httpx2"):
        asyncio.run(go())
    lines = _messages(caplog, "httpx2")
    assert lines == ["HTTP Request: POST /v2/<secret> 200"]


def test_a_key_in_any_letter_case_is_redacted(monkeypatch):
    monkeypatch.setattr(settings, "GNOSIS_RPC_URL", f"https://gnosis.example/{PLANTED}")
    rec = _request_record(f"https://gnosis.example/{PLANTED.lower()}/x")
    http_log.Httpx2RequestLogFilter().filter(rec)
    assert rec.getMessage() == "HTTP Request: POST /<secret>/x 200"


def test_a_percent_encoded_configured_secret_is_redacted(monkeypatch):
    secret = "Key 123abcXYZ"
    monkeypatch.setattr(settings, "BASE_RPC_URL", "https://rpc.example/v2/Key%20123abcXYZ")
    rec = _request_record("https://rpc.example/v2/Key%20123abcXYZ")
    http_log.Httpx2RequestLogFilter().filter(rec)
    msg = rec.getMessage()
    assert "123abcXYZ" not in msg and secret not in msg
    assert msg == "HTTP Request: POST /v2/<secret> 200"


@pytest.mark.parametrize("secret", [
    'Key"1234abcXYZ{}|^`',  # httpx2 keeps "|" and "^" in a path but encodes the rest
    "Key1234abcXYZ" + "".join(c for c in "!\"$&'()*+,;:<=>@[\\]^`{|}~"),
    "Key1234abcXYZ\u00e9\u65e5",  # non-ASCII, UTF-8 percent-encoded
])
def test_a_configured_secret_is_redacted_as_httpx2_encodes_it(monkeypatch, secret):
    url = "https://rpc.example/v2/" + secret
    monkeypatch.setattr(settings, "BASE_RPC_URL", url)
    rec = _request_record(url)
    raw = httpx2.URL(url).raw_path.decode("ascii")
    assert raw != "/v2/" + secret  # the premise: httpx2 percent-encodes part of it
    http_log.Httpx2RequestLogFilter().filter(rec)
    msg = rec.getMessage()
    assert "1234abcXYZ" not in msg, (raw, msg)
    assert msg == "HTTP Request: POST /v2/<secret> 200", (raw, msg)


def test_unconfigured_paths_pass_unchanged(monkeypatch):
    monkeypatch.setattr(settings, "BASE_RPC_URL", f"https://rpc.example/v2/{PLANTED}")
    ref = "ab12" * 16
    rec = _request_record(f"{BEE_URL}/bytes/{ref}")
    http_log.Httpx2RequestLogFilter().filter(rec)
    assert rec.getMessage() == f"HTTP Request: POST /bytes/{ref} 200"


def test_path_redaction_failure_never_raises(monkeypatch):
    monkeypatch.setattr(settings, "BASE_RPC_URL", f"https://rpc.example/v2/{PLANTED}")

    def boom(text):
        raise RuntimeError("scrubber broke")

    monkeypatch.setattr(bee_errors, "without_secrets", boom)
    rec = _request_record(f"https://rpc.example/v2/{PLANTED}")
    assert http_log.Httpx2RequestLogFilter().filter(rec) is True
    msg = rec.getMessage()
    assert PLANTED not in msg
    assert msg == "HTTP Request: POST /<redacted> 200"


# --------------------------------------------------------------------------- #
# httpcore2's DEBUG trace
# --------------------------------------------------------------------------- #
def test_receive_response_headers_failed_is_redacted_at_debug(caplog):
    server = _RawServer(f"HTTP/1.1 {MARKER} \xff\r\n\r\n".encode("latin-1"))

    async def go():
        async with httpx2.AsyncClient() as c:
            await c.get(f"http://127.0.0.1:{server.port}/x")

    try:
        with caplog.at_level(logging.DEBUG), pytest.raises(httpx2.RemoteProtocolError):
            asyncio.run(go())
    finally:
        server.close()
    lines = _messages(caplog, "httpcore2")
    assert "receive_response_headers.failed exception=RemoteProtocolError" in lines
    assert not any(MARKER in line for line in lines), lines


def test_other_failed_traces_keep_only_the_exception_type():
    f = http_log.Httpcore2HeadersLogFilter()
    rec = logging.LogRecord("httpcore2.http11", logging.DEBUG, __file__, 1,
                            f"receive_response_body.failed exception=ReadError('{MARKER}')", (), None)
    assert f.filter(rec) and rec.getMessage() == "receive_response_body.failed exception=ReadError"
    rec = logging.LogRecord("httpcore2.connection", logging.DEBUG, __file__, 1,
                            "connect_tcp.started host='h' port=1", (), None)
    assert f.filter(rec) and rec.getMessage() == "connect_tcp.started host='h' port=1"
