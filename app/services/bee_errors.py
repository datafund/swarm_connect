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

What it replaces, in this order:

1. Percent-encoding is decoded first (twice at most), so "bee%2Einternal"
   and "http%253A%252F%252F..." are scrubbed like their plain form.
2. RFC 3339 timestamps are set aside and kept as they are.
3. URLs with a scheme -> <url>.
4. libp2p multiaddrs: the address after /ip4/ or /ip6/ -> <address>, the
   name after /dns/, /dns4/, /dns6/ or /dnsaddr/ -> <host>, and a /tcp/ or
   /udp/ port right after it -> <port>. The peer id is kept.
5. user:password@ before a host -> removed (up to the last "@").
6. The names in Go's x509 "certificate is valid for A, B, not C" and
   "wanted to match C" -> <host>.
7. IPv6 addresses -> <address>, bracketed or bare, with any port and path.
   A bare one must parse, and either have 3 or more groups (fd00::1:2,
   ::ffff:10.0.0.1, which takes a :port as well) or have groups on both
   sides of "::" with at least 4 hex digits in all (fd00::1). So "::",
   "a::b" and "cafe::" stay.
8. IPv4 addresses -> <address>, with any port (even an invalid one) and
   path: four dot-separated groups of 1-3 digits, each at most 255, leading
   zeros allowed. Three groups (2.8.1) or a group over 255 (1.2.300.4) stay.
9. The name after "lookup" -> <host> when it is dotted, is a configured
   host, or is followed by " on " (Go's resolver error). "lookup failed"
   stays.
10. Hosts this gateway is configured with -> <host>, or <address> with a
    port or path: the host names in SWARM_BEE_API_URL, GNOSIS_RPC_URL,
    BASE_RPC_URL, X402_FACILITATOR_URL and X402_BZZ_PRICE_FEED_URL, read at
    call time, matched as whole tokens in any case. This is the only rule
    that replaces a bare word or a hyphenated word without a dot
    ("bee:1633", "dial bee-dev"): a field such as "timeout:30",
    "gasLimit:21000" or "chain-id:100" cannot be told apart from a host by
    its shape, so only names known to be hosts are taken. The cost: with
    the Bee host named "bee", the word "bee" in Bee's text is replaced too.
11. A dotted name with a port -> <address> (bee.internal:1633), with any
    path after it; "main.go:123" is a Go source position and stays.
12. A dotted name ending in a letters-only label and followed by a path ->
    <address> (rpc.example.com/v2/KEY).
13. Internal-only names -> <host>: any name ending in .internal or .svc
    (bee.internal, bee.ns.svc), and names with at least two labels before
    .local, .lan or .home (bee.cluster.local, gw.site.lan). A single label
    before those suffixes stays, because "file.local" and "my.home" are
    ordinary words; with a port or path they are caught by 11 and 12.

Other bare host names without a port or path are not replaced: a dotted word
cannot be told apart from one in ordinary text.

Every pattern is anchored by a lookbehind or a literal and has bounded
repetition, so scrubbing takes linear time; callers still cap Bee's text at
200 characters.
"""
import functools
import ipaddress
import re
from urllib.parse import unquote, urlsplit

from app.core.config import settings


def error_cause(exc) -> str:
    """'HTTP <status>' when there is a response, otherwise the exception class."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is not None:
        return f"HTTP {status}"
    return type(exc).__name__


# Settings whose URL host is an address of this gateway's own infrastructure.
_HOST_SETTINGS = ("SWARM_BEE_API_URL", "GNOSIS_RPC_URL", "BASE_RPC_URL",
                  "X402_FACILITATOR_URL", "X402_BZZ_PRICE_FEED_URL")

# An address may end a sentence: a following "." is allowed when no word
# character comes after it ("refused by 10.0.0.1." -> "refused by <address>.").
_END = r"(?!\w|\.\w)"
# A path can carry an API key; it ends before trailing ).,;:
_PATH = r"(?:/(?:[^\s\"'<>]*[^\s\"'<>).,;:])?)?"
_LABEL = r"[A-Za-z0-9-]{1,63}"
_DOTTED = rf"(?:{_LABEL}\.){{1,16}}{_LABEL}"

_PCT_RE = re.compile(r"%[0-9A-Fa-f]{2}")
_RFC3339_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?(?:[Zz]|[+-]\d{2}:?\d{2})?)")
_URL_RE = re.compile(r"(?<![a-z0-9+.-])[a-z][a-z0-9+.-]{0,31}://[^\s\"'<>]*[^\s\"'<>).,;:]",
                     re.IGNORECASE)
_MULTIADDR_RE = re.compile(
    r"(?<![\w/])/(ip4|ip6|dns|dns4|dns6|dnsaddr)/([^/\s]{1,253})(?:/(tcp|udp)/\d{1,5})?",
    re.IGNORECASE)
_USERINFO_RE = re.compile(
    r"(?<![\w.:@-])[^\s:@/\"'<>()]{1,64}:[^\s/\"'<>()]{0,128}@(?=[\w\[])")
_X509_RE = re.compile(
    r"\b(certificate is valid for )((?:[^\s,]{1,253}, ){0,32}?[^\s,]{1,253})(, not )"
    r"([^\s,;]{1,253}?)" + _END
    + r"|\b(wanted to match )([^\s,;]{1,253}?)" + _END,
    re.IGNORECASE)
_BRACKETED_V6_RE = re.compile(
    r"\[([0-9A-Fa-f:.]{2,45})(?:%[\w.-]{1,32})?\](?::\d{1,5})?" + _PATH)
_BARE_V6_RE = re.compile(
    r"(?<![\w:.])((?:[0-9A-Fa-f]{0,4}:){2,7}(?:(\d{1,3}(?:\.\d{1,3}){3})|[0-9A-Fa-f]{0,4}))"
    r"(%[\w.-]{1,32})?((?<=\d):\d{1,5}(?!\d))?")
_IPV4_RE = re.compile(
    r"(?<![\w.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![A-Za-z0-9]|\.\d)"
    r"(?::\d{1,10})?" + _PATH)
_LOOKUP_RE = re.compile(r"\b(lookup\s{1,8})(?!<)([^\s:]{1,253})", re.IGNORECASE)
_HOST_PORT_RE = re.compile(rf"(?<![\w.<@/-])({_DOTTED}):\d{{1,10}}" + _PATH)
_HOST_PATH_RE = re.compile(
    rf"(?<![\w.<@/-])(?:{_LABEL}\.){{1,16}}[A-Za-z]{{2,63}}/(?:[^\s\"'<>]*[^\s\"'<>).,;:])?")
_INTERNAL_NAME_RE = re.compile(
    rf"(?<![\w.<@/-])(?:(?:{_LABEL}\.){{1,16}}(?:internal|svc)"
    rf"|(?:{_LABEL}\.){{2,16}}(?:local|lan|home))" + _END,
    re.IGNORECASE)

# "main.go:123" is a source position in a Go error (Bee is written in Go).
# Only .go: other source suffixes such as .py and .rs are country domains.
_SOURCE_SUFFIXES = (".go",)


def _configured_hosts():
    hosts = set()
    for name in _HOST_SETTINGS:
        value = getattr(settings, name, None)
        if not value:
            continue
        try:
            host = urlsplit(str(value)).hostname
        except ValueError:
            continue
        if host:
            hosts.add(host.lower().strip("[]"))
    return tuple(sorted(hosts, key=lambda h: (-len(h), h)))


@functools.lru_cache(maxsize=8)
def _configured_host_re(hosts):
    names = "|".join(re.escape(h) for h in hosts)
    return re.compile(rf"(?<![\w.<@/-])(?:{names})(?::\d{{1,10}}|(?=/))?{_PATH}{_END}",
                      re.IGNORECASE)


def _multiaddr(match):
    proto = match.group(1)
    hidden = "<address>" if proto.lower() in ("ip4", "ip6") else "<host>"
    out = f"/{proto}/{hidden}"
    if match.group(3):
        out += f"/{match.group(3)}/<port>"
    return out


def _x509(match):
    if match.group(1):
        return f"{match.group(1)}<host>{match.group(3)}<host>"
    return f"{match.group(5)}<host>"


def _bracketed_v6(match):
    try:
        ipaddress.IPv6Address(match.group(1))
    except ValueError:
        return match.group(0)  # e.g. a slice such as [0:10]
    return "<address>"


def _v6_is_address(candidate):
    try:
        ipaddress.IPv6Address(candidate)
    except ValueError:
        return False
    groups = [g for g in candidate.split(":") if g]
    count = sum(2 if "." in g else 1 for g in groups)
    if count >= 3:
        return True
    if "::" in candidate:
        left, _, right = candidate.partition("::")
        digits = sum(len(g) for g in groups)
        return bool(left) and bool(right) and digits >= 4
    return False


def _bare_v6(match):
    # The pattern can take trailing ':' that belong to the sentence
    # ("dial fd00::1: unreachable"); give them back until the address parses.
    candidate, zone, port = match.group(1), match.group(3) or "", match.group(4) or ""
    rest = ""
    while not _v6_is_address(candidate):
        if zone or port or not candidate.endswith(":"):
            return match.group(0)  # e.g. a time such as 12:34:56
        candidate, rest = candidate[:-1], ":" + rest
    if not rest and not port and re.match(r"\w|\.\w", match.string[match.end():match.end() + 2]):
        return match.group(0)
    return "<address>" + rest


def _ipv4(match):
    if any(int(match.group(i)) > 255 for i in range(1, 5)):
        return match.group(0)  # a version string such as 1.2.300.4
    return "<address>"


def _host_port(match):
    host = match.group(1)
    if not any(c.isalpha() for c in host) or host.lower().endswith(_SOURCE_SUFFIXES):
        return match.group(0)
    return "<address>"


def _scrub(text, hosts):
    text = _URL_RE.sub("<url>", text)
    text = _MULTIADDR_RE.sub(_multiaddr, text)
    text = _USERINFO_RE.sub("", text)
    text = _X509_RE.sub(_x509, text)
    text = _BRACKETED_V6_RE.sub(_bracketed_v6, text)
    text = _BARE_V6_RE.sub(_bare_v6, text)
    text = _IPV4_RE.sub(_ipv4, text)

    def lookup(match):
        name = match.group(2).rstrip(".,;)")
        tail = match.group(2)[len(name):]
        after = match.string[match.end():match.end() + 4]
        if "." in name or name.lower() in hosts or after == " on ":
            return f"{match.group(1)}<host>{tail}"
        return match.group(0)

    text = _LOOKUP_RE.sub(lookup, text)
    if hosts:
        text = _configured_host_re(hosts).sub(
            lambda m: "<host>" if m.group(0).lower() in hosts else "<address>", text)
    text = _HOST_PORT_RE.sub(_host_port, text)
    text = _HOST_PATH_RE.sub("<address>", text)
    return _INTERNAL_NAME_RE.sub("<host>", text)


def without_addresses(text: str) -> str:
    """Replace URLs, IP addresses, configured hosts and internal names in text."""
    for _ in range(2):
        if not _PCT_RE.search(text):
            break
        text = unquote(text)
    hosts = _configured_hosts()
    parts = _RFC3339_RE.split(text)
    # re.split with one group: even indices are text, odd are timestamps.
    return "".join(p if i % 2 else _scrub(p, hosts) for i, p in enumerate(parts))
