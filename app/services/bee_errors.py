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
those and leaves the rest readable. What it replaces:

- URLs with a scheme, plain or percent-encoded (http%3A%2F%2F...) -> <url>
- user:password@ in front of a host -> removed
- IPv4 and IPv6 addresses (bracketed, bare, IPv4-mapped), with an optional
  port and path -> <address>
- host:port with an optional path -> <address>, where the host has a letter
  and either a dot or hyphen in it (bee.internal:1633, bee-dev:1633), or is a
  single word with a service port -- 1024 or higher, or one of 21, 22, 25,
  53, 80, 443 -- that is not a common field name (bee:1633, rpc:8545).
  "min:17", "timeout:30", "amount:100", "status:500" and "code:400" are
  "key:number" fields, not addresses, so they stay, as do Go source
  positions such as main.go:123. A hyphenated "key:number" such as
  max-age:3600 is replaced: it cannot be told apart from bee-dev:1633.
- the name after Go's "lookup" when followed by " on " or ":", or dotted
  -> <host>
- the names in Go's x509 "certificate is valid for A, B, not C" and "but
  wanted to match C" -> <host>
- any dotted name ending in .internal, .local (including .cluster.local),
  .lan, .svc or .home -> <host>

Other bare host names without a port are not replaced: a dotted word cannot
be told apart from one in ordinary text. RFC 3339 timestamps are left intact.
"""
import ipaddress
import re


def error_cause(exc) -> str:
    """'HTTP <status>' when there is a response, otherwise the exception class."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is not None:
        return f"HTTP {status}"
    return type(exc).__name__


# An address may end a sentence: a following "." is allowed when no word
# character comes after it ("refused by 10.0.0.1." -> "refused by <address>.").
_END = r"(?!\w|\.\w)"
# A path after a port can carry an API key; it ends before trailing ).,;:
_PATH = r"(?:/(?:[^\s\"'<>]*[^\s\"'<>).,;:])?)?"
_PORT = r"(?::(\d{1,5}))?"

_RFC3339_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:[Zz]|[+-]\d{2}:?\d{2})?)")
_URL_RE = re.compile(r"[a-z][a-z0-9+.-]*://[^\s\"'<>]*[^\s\"'<>).,;:]", re.IGNORECASE)
_ENCODED_URL_RE = re.compile(r"[a-z][a-z0-9+.-]*%3A%2F%2F[^\s\"'<>]*[^\s\"'<>).,;:]",
                             re.IGNORECASE)
_USERINFO_RE = re.compile(r"(?<![\w.-])[^\s:@/\"'<>()]+:[^\s@/\"'<>()]*@(?=[\w\[])")
_X509_RE = re.compile(
    r"\b(certificate is valid for )((?:[^\s,]+, )*?[^\s,]+)(, not )([^\s,;]+?)" + _END
    + r"|\b(wanted to match )([^\s,;]+?)" + _END,
    re.IGNORECASE)
_BRACKETED_V6_RE = re.compile(r"\[([0-9A-Fa-f:.]+)(?:%[\w.-]+)?\]" + _PORT + _PATH)
_BARE_V6_RE = re.compile(
    r"(?<![\w:.])((?:[0-9A-Fa-f]{0,4}:){2,7}(?:\d{1,3}(?:\.\d{1,3}){3}|[0-9A-Fa-f]{0,4}))"
    r"(%[\w.-]+)?")
_IPV4_RE = re.compile(r"(?<![\w.])((?:\d{1,3}\.){3}\d{1,3})" + _PORT + _PATH + _END)
# Go: "lookup bee.internal on 127.0.0.11:53: ..." or "lookup bee: no such
# host". The name is replaced when it is followed by " on " or ":", or has a
# dot, so ordinary text ("lookup failed") stays.
_LOOKUP_RE = re.compile(
    r"\b(lookup\s+)(?!<)(?:[^\s:]+(?=\s+on\s|:)|[^\s:]*\.[^\s:]*[^\s:.,;)])",
    re.IGNORECASE)
_HOST_PORT_RE = re.compile(
    r"(?<![\w.<-])([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*):(\d{1,5})(?!\d)" + _PATH + _END)
_INTERNAL_NAME_RE = re.compile(
    r"(?<![\w.-])(?:[A-Za-z0-9-]+\.)+(?:internal|local|lan|svc|home)" + _END,
    re.IGNORECASE)

# A single plain word before ":number" is a field ("timeout:30") unless the
# number is a service port; these field names keep even a large number.
_FIELD_NAMES = frozenset({
    "status", "statuscode", "code", "error", "err", "errno", "line", "depth",
    "attempt", "retry", "retries", "id", "min", "max", "timeout", "amount",
    "batch", "size", "count", "limit", "offset", "len", "length", "index",
    "block", "height", "ttl", "port", "want", "got", "expected",
})
_WELL_KNOWN_PORTS = frozenset({21, 22, 25, 53, 80, 443})
# "main.go:123" is a source position in a Go error (Bee is written in Go),
# not a host and a port. Only .go: other source suffixes such as .py and .rs
# are also country domains.
_SOURCE_SUFFIXES = (".go",)


def _valid_port(port):
    return port is None or 0 < int(port) <= 65535


def _ipv4(match):
    try:
        ipaddress.IPv4Address(match.group(1))
    except ValueError:
        return match.group(0)  # a version string such as 1.2.300.4
    if not _valid_port(match.group(2)):
        return match.group(0)
    return "<address>"


def _bracketed_v6(match):
    try:
        ipaddress.IPv6Address(match.group(1))
    except ValueError:
        return match.group(0)  # e.g. a slice such as [0:10]
    return "<address>"


def _bare_v6(match):
    # The pattern can take trailing ':' that belong to the sentence
    # ("dial fd00::1: unreachable"); give them back until the address is valid.
    candidate, zone = match.group(1), match.group(2) or ""
    rest = ""
    while True:
        try:
            ipaddress.IPv6Address(candidate)
            break
        except ValueError:
            if zone or not candidate.endswith(":"):
                return match.group(0)  # e.g. a time such as 12:34:56
            candidate, rest = candidate[:-1], ":" + rest
    if not rest and re.match(r"\w|\.\w", match.string[match.end():match.end() + 2]):
        return match.group(0)
    return "<address>" + rest


def _host_port(match):
    host, port = match.group(1), int(match.group(2))
    if not any(c.isalpha() for c in host) or not _valid_port(port):
        return match.group(0)
    if host.lower().endswith(_SOURCE_SUFFIXES):
        return match.group(0)
    if "." in host or "-" in host:
        return "<address>"
    if host.lower() in _FIELD_NAMES:
        return match.group(0)
    if port >= 1024 or port in _WELL_KNOWN_PORTS:
        return "<address>"
    return match.group(0)


def _x509(match):
    if match.group(1):
        return f"{match.group(1)}<host>{match.group(3)}<host>"
    return f"{match.group(5)}<host>"


def _scrub(text: str) -> str:
    text = _URL_RE.sub("<url>", text)
    text = _ENCODED_URL_RE.sub("<url>", text)
    text = _USERINFO_RE.sub("", text)
    text = _X509_RE.sub(_x509, text)
    text = _BRACKETED_V6_RE.sub(_bracketed_v6, text)
    text = _BARE_V6_RE.sub(_bare_v6, text)
    text = _IPV4_RE.sub(_ipv4, text)
    text = _LOOKUP_RE.sub(r"\1<host>", text)
    text = _HOST_PORT_RE.sub(_host_port, text)
    return _INTERNAL_NAME_RE.sub("<host>", text)


def without_addresses(text: str) -> str:
    """Replace URLs, IP addresses, host:port and internal host names in text.

    RFC 3339 timestamps are split out first and kept as they are, so
    "2026-11-03T10:00:00Z" is not read as a host and a port.
    """
    parts = _RFC3339_RE.split(text)
    # re.split with one group: even indices are text, odd are timestamps.
    return "".join(p if i % 2 else _scrub(p) for i, p in enumerate(parts))
