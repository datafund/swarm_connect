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
A secret in the path of a configured URL (an RPC provider's /v2/<KEY>) is
replaced with <secret> by bee_errors.without_secrets() (#659), which matches
the secrets bee_errors collects from SWARM_BEE_API_URL, GNOSIS_RPC_URL,
BASE_RPC_URL, X402_FACILITATOR_URL and X402_BZZ_PRICE_FEED_URL, in any letter
case and percent-encoded the way httpx2 writes a path. Two kinds of key are
not collected there, so they are not replaced either: a segment without a
run of 8 or more letters and digits ("/v2/aB3d5f7"), and a key of one letter
case only ("abcdefghijklmnop"). If the redaction fails, the path is logged
as /<redacted>; the filter never raises.

httpcore2 traces at DEBUG only, which the gateway never enables. Should
someone turn DEBUG on, two kinds of its lines are rewritten: the
receive_response_headers.complete line (headers and reason phrase) loses its
arguments, and every "<step>.failed exception=<repr>" line keeps only the
exception type, because the repr quotes server bytes ("illegal status line:
..."). Other trace lines pass unchanged.

Logger filters apply to records logged on that exact logger, under any
handler configuration (uvicorn's included), so install() is called once
where logging is configured: app/main.py and app/x402/check_config.py. It is
idempotent.
"""
import logging
import re

MAX_PATH_CHARS = 200

_HTTPX2_REQUEST_MSG = 'HTTP Request: %s %s "%s %d %s"'
# The loggers httpcore2 traces on (httpcore2/_async/*.py, _sync/*.py).
_HTTPCORE2_LOGGERS = ("httpcore2.http11", "httpcore2.http2", "httpcore2.connection",
                      "httpcore2.proxy", "httpcore2.socks")


# Logged instead of the path when the secret redaction fails.
REDACTED_PATH = "/<redacted>"


def _safe_path(url) -> str:
    try:
        path = url.raw_path.split(b"?", 1)[0].decode("ascii", errors="replace")
    except Exception:
        path = str(getattr(url, "path", "")) or "/"
    try:
        # Imported on use: importing this module must not load the settings.
        from app.services import bee_errors
        # Before the cap, so a secret that straddles it is still found whole.
        path = bee_errors.without_secrets(path)
    except Exception:
        return REDACTED_PATH
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


# httpcore2's _trace.py: f"{name}.failed exception={exc!r}". Only the step
# name and the exception's class name are kept.
_FAILED_RE = re.compile(r"([a-z0-9_]{1,64})\.failed(?: exception=([A-Za-z_][A-Za-z0-9_]{0,64})\b)?")


class Httpcore2HeadersLogFilter(logging.Filter):
    """Redacts httpcore2's DEBUG traces of response headers and of failures."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.msg
        if not isinstance(msg, str):
            return True
        if msg.startswith("receive_response_headers.complete"):
            record.msg = "receive_response_headers.complete (headers redacted)"
            record.args = ()
            return True
        failed = _FAILED_RE.match(msg)
        if failed:
            step, exc_type = failed.groups()
            record.msg = "%s.failed exception=%s" if exc_type else "%s.failed"
            record.args = (step, exc_type) if exc_type else (step,)
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
