# app/services/bee_errors.py
"""Keep internal addresses out of error text shown to callers (#534, #549).

httpx's HTTPStatusError text names the request URL, which is the internal Bee
API address (or an RPC endpoint, which can carry an API key). Responses that
anyone can read -- /health, /pool/status, stamp purchase and extend errors --
name a failure by its HTTP status or exception class instead, and the full
text goes to the log.

Bee's own message is still worth showing ("out of funds"), but it is response
text: a proxy's error page or a Go network error can quote a URL, an IP, a
host:port or the host it failed to resolve. without_addresses() replaces
those and leaves the rest readable.
"""
import ipaddress
import re


def error_cause(exc) -> str:
    """'HTTP <status>' when there is a response, otherwise the exception class."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is not None:
        return f"HTTP {status}"
    return type(exc).__name__


# A URL ends before trailing punctuation, so "Get http://bee:1633/x: EOF"
# keeps its colon and "(see http://x/y)." keeps ")." .
_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>]*[^\s\"'<>).,;:]")
_BRACKETED_V6_RE = re.compile(r"\[([0-9A-Fa-f:.]+)(?:%[\w.-]+)?\](?::\d{1,5})?")
_IPV4_RE = re.compile(r"(?<![\w.])((?:\d{1,3}\.){3}\d{1,3})(?::\d{1,5})?(?![\w.])")
_BARE_V6_RE = re.compile(
    r"(?<![\w:.])([0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7})(?:%[\w.-]+)?(?![\w:])")
# Go's resolver error: "lookup bee.internal on 127.0.0.11:53: no such host".
_LOOKUP_RE = re.compile(r"\b(lookup\s+)(?!<)[^\s:]+")
_HOST_PORT_RE = re.compile(r"(?<![\w.<-])([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*):(\d{1,5})(?![\w.])")

# "key:number" forms that Go and JSON-ish error text uses and that are not
# addresses. A host named exactly one of these is the only thing this keeps.
_NOT_HOSTS = frozenset({
    "status", "statuscode", "code", "error", "err", "errno", "line", "depth",
    "attempt", "retry", "retries", "id",
})


def _ipv4(match):
    try:
        ipaddress.IPv4Address(match.group(1))
    except ValueError:
        return match.group(0)  # a version string such as 1.2.300.4
    return "<address>"


def _bracketed_v6(match):
    try:
        ipaddress.IPv6Address(match.group(1))
    except ValueError:
        return match.group(0)  # e.g. a slice such as [0:10]
    return "<address>"


def _bare_v6(match):
    try:
        ipaddress.IPv6Address(match.group(1))
    except ValueError:
        return match.group(0)  # e.g. a time such as 12:34:56
    return "<address>"


def _host_port(match):
    host = match.group(1)
    if not any(c.isalpha() for c in host) or host.lower() in _NOT_HOSTS:
        return match.group(0)
    return "<address>"


def without_addresses(text: str) -> str:
    """Replace URLs, IP addresses, host:port and resolved host names in text."""
    text = _URL_RE.sub("<url>", text)
    text = _BRACKETED_V6_RE.sub(_bracketed_v6, text)
    text = _IPV4_RE.sub(_ipv4, text)
    text = _BARE_V6_RE.sub(_bare_v6, text)
    text = _LOOKUP_RE.sub(r"\1<host>", text)
    return _HOST_PORT_RE.sub(_host_port, text)
