# app/core/http_log.py
"""Keep server-controlled text out of the HTTP client's log lines (#608).

httpx2 logs every request at INFO as
    HTTP Request: GET <url> "HTTP/1.1 <status> <reason phrase>"
The reason phrase, and on a followed redirect the URL, come from the server
answering: any visible character, up to about 100 KiB. The gateway logs at
INFO, and the deploy job prints the container's last log lines into its
public log, so a facilitator or Bee answering `HTTP/1.1 999 <anything>`
could write into that log.

install() puts a filter on the "httpx2" logger that rewrites that line to
    HTTP Request: GET /path 999
keeping the method, the URL path without its query (capped at
MAX_PATH_CHARS, already percent-encoded by httpx2) and the status code, and
dropping the reason phrase, the host, the query and the HTTP version.
httpcore2 logs response headers (reason phrase included) only at DEBUG,
which the gateway never enables; its receive_response_headers lines are
redacted the same way should someone turn DEBUG on.

Logger filters apply to records logged on that exact logger, under any
handler configuration (uvicorn's included), so install() is called once
where logging is configured: app/main.py and app/x402/check_config.py. It is
idempotent.
"""
import logging

MAX_PATH_CHARS = 200

_HTTPX2_REQUEST_MSG = 'HTTP Request: %s %s "%s %d %s"'
# The loggers httpcore2 traces on (httpcore2/_async/*.py, _sync/*.py).
_HTTPCORE2_LOGGERS = ("httpcore2.http11", "httpcore2.http2", "httpcore2.connection",
                      "httpcore2.proxy", "httpcore2.socks")


def _safe_path(url) -> str:
    try:
        path = url.raw_path.split(b"?", 1)[0].decode("ascii", errors="replace")
    except Exception:
        path = str(getattr(url, "path", "")) or "/"
    path = "".join(c if " " < c <= "~" else "?" for c in path) or "/"
    return path if len(path) <= MAX_PATH_CHARS else path[:MAX_PATH_CHARS] + "..."


class Httpx2RequestLogFilter(logging.Filter):
    """Rewrites httpx2's request line; passes every other record unchanged."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.msg == _HTTPX2_REQUEST_MSG and isinstance(record.args, tuple) and len(record.args) == 5:
            method, url, _version, status, _reason = record.args
            method = method if isinstance(method, str) and method.isalpha() and len(method) <= 16 else "?"
            record.msg = "HTTP Request: %s %s %d"
            record.args = (method, _safe_path(url), int(status))
        return True


class Httpcore2HeadersLogFilter(logging.Filter):
    """Redacts httpcore2's DEBUG trace of received response headers."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str) and record.msg.startswith("receive_response_headers.complete"):
            record.msg = "receive_response_headers.complete (headers redacted)"
            record.args = ()
        return True


_httpx2_filter = Httpx2RequestLogFilter()
_httpcore2_filter = Httpcore2HeadersLogFilter()


def install() -> None:
    """Attach the filters (once)."""
    logger = logging.getLogger("httpx2")
    if _httpx2_filter not in logger.filters:
        logger.addFilter(_httpx2_filter)
    for name in _HTTPCORE2_LOGGERS:
        logger = logging.getLogger(name)
        if _httpcore2_filter not in logger.filters:
            logger.addFilter(_httpcore2_filter)
