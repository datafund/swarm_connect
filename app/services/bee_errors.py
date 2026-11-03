# app/services/bee_errors.py
"""Keep internal addresses out of error text shown to callers (#534, #549).

httpx's HTTPStatusError text names the request URL, which is the internal Bee
API address (or an RPC endpoint, which can carry an API key). Responses that
anyone can read -- /health, /pool/status, stamp purchase and extend errors --
name a failure by its HTTP status or exception class instead, and the full
text goes to the log.

Bee's own message is still worth showing ("out of funds"), but it is response
text: a proxy's error page or a Go network error can quote a URL, an IP, a
host:port or the host it failed to resolve. public_bee_text() scrubs it with
without_addresses() and caps it for a response.

without_addresses() replaces, in this order:

1. Secrets of the configured URLs -> <secret>: every path segment and query
   value of 8 or more characters that is not a plain lowercase word (so the
   route "facilitator" stays), and every userinfo password of 4 or more,
   in SWARM_BEE_API_URL, GNOSIS_RPC_URL, BASE_RPC_URL, X402_FACILITATOR_URL
   and X402_BZZ_PRICE_FEED_URL ("invalid project id SECRETKEY"). Matched
   case-sensitively as whole tokens.
2. URLs with a scheme -> <url>.
3. libp2p multiaddrs: the address after /ip4/ or /ip6/ -> <address>, the
   name after /dns/, /dns4/, /dns6/ or /dnsaddr/ -> <host>, and a /tcp/ or
   /udp/ port right after it -> <port>. The peer id is kept.
4. user:password@ before a host -> removed (up to the last "@"; the password
   may hold "/" and "@"); a configured host right after it -> <host>, or
   <address> with a port.
5. The names in Go's x509 "certificate is valid for A, B, not C" and
   "wanted to match C" -> <host>.
6. IPv6 addresses -> <address>, bracketed or bare, with any port and path.
   A bare one must parse, and either have 3 or more groups (fd00::1:2,
   ::ffff:10.0.0.1, which takes a :port as well) or have groups on both
   sides of "::" with at least 4 hex digits in all (fd00::1).
7. IPv4 addresses -> <address>, with any port (even an invalid one) and
   path: four dot-separated groups of 1-3 digits, each at most 255, leading
   zeros allowed. Three groups (2.8.1) or a group over 255 (1.2.300.4) stay.
8. The name after "lookup" -> <host> when it is dotted, is a configured
   host, or is followed by " on " (Go's resolver error).
9. Hosts this gateway is configured with (the URLs in step 1; a value
   without a scheme such as "bee:1633" counts too), matched as whole tokens
   in any case. A dotted one is replaced anywhere -> <host>, or <address>
   with a port or path. A dotless one ("bee", "bee-dev") is a compose
   service name, already public in docker-compose.yml, and "bee" is also a
   word in Bee's own text, so it is replaced only with a :port, a /path
   that does not start with a digit, or a ?query/#fragment after it
   ("bee:1633", "BEE/stamps" -> <address>), or in steps 3, 4 and 8.
   "bee/2.8.1" (a user agent) and "Bee is syncing" stay.
10. A dotted name with a port -> <address> (bee.internal:1633), with any
    path, query or fragment after it; "main.go:123" is a Go source position
    and stays.
11. A dotted name ending in a letters-only label and followed by a path,
    query or fragment -> <address> (rpc.example.com/v2/KEY,
    gnosis.example.com?apikey=KEY). Not after "/", so a file path such as
    app/services/x.py/ stays.
12. Internal-only names -> <host>: any name ending in .internal or .svc,
    and names with at least two labels before .local, .lan or .home
    (bee.cluster.local). "file.local" and "my.home" stay.

A host is found after "<", "@" or "/" as well ("<bee.internal:1633>",
"admin@gnosis.example.com/v2/KEY"): replacements are written as private-use
placeholder characters while scrubbing and turned into <url>, <address>,
<host>, <port> and <secret> at the end, so a "<" in the text is not taken for
an earlier replacement.

Percent-encoded text is decoded (printable ASCII only, twice at most) and the
decoded form is used when it has something to replace, and at least as much
as the encoded form; otherwise the text keeps its encoding ("100%ff",
"50%25"). When the decoded form is used, the whole text is shown decoded. RFC 3339 timestamps
are kept.

Kept by design, because they cannot be told apart from ordinary text or
belong to no gateway setting: a dotless host that is not configured
("nethermind:8545", "localhost:8545"; Bee's own RPC endpoint is a Bee
setting, not a gateway one), a bare dotted name with no port or path
("rpc.example.com"), short IPv6 forms such as "::1", "2001:db8::" and "a::b",
and "key:number" fields such as "timeout:30" or "chain-id:100".

Every pattern is anchored by a lookbehind or a literal and has bounded
repetition, so scrubbing takes linear time.
"""
import functools
import ipaddress
import re
from urllib.parse import parse_qsl, unquote, urlsplit

from app.core.config import settings


def error_cause(exc) -> str:
    """'HTTP <status>' when there is a response, otherwise the exception class."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is not None:
        return f"HTTP {status}"
    return type(exc).__name__


# Settings whose URLs belong to this gateway's own infrastructure.
_HOST_SETTINGS = ("SWARM_BEE_API_URL", "GNOSIS_RPC_URL", "BASE_RPC_URL",
                  "X402_FACILITATOR_URL", "X402_BZZ_PRICE_FEED_URL")

# Placeholders while scrubbing (private-use code points, never in real text).
_URL, _ADDR, _HOST, _PORT, _SECRET = "", "", "", "", ""
_SENTINELS = _URL + _ADDR + _HOST + _PORT + _SECRET
_FINAL = str.maketrans({_URL: "<url>", _ADDR: "<address>", _HOST: "<host>",
                        _PORT: "<port>", _SECRET: "<secret>"})

# An address may end a sentence: a following "." is allowed when no word
# character comes after it ("refused by 10.0.0.1." -> "refused by <address>.").
_END = r"(?!\w|\.\w)"
# A path, query or fragment can carry an API key; it ends before ).,;:
_TAIL = r"(?:[^\s\"'<>]{0,2048}[^\s\"'<>).,;:])?"
_PATH = r"(?:[/?#]" + _TAIL + r")?"
_LABEL = r"[A-Za-z0-9-]{1,63}"
_DOTTED = rf"(?:{_LABEL}\.){{1,16}}{_LABEL}"
# A host may follow "<", "@", "/" or a placeholder, but not a word character,
# "." or "-", which would make it part of a longer name.
_HOST_START = r"(?<![\w.-])"

_PCT_RE = re.compile(r"%([2-7][0-9A-Fa-f])")
_RFC3339_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?(?:[Zz]|[+-]\d{2}:?\d{2})?)")
_URL_RE = re.compile(r"(?<![a-z0-9+.-])[a-z][a-z0-9+.-]{0,31}://" + _TAIL, re.IGNORECASE)
_MULTIADDR_RE = re.compile(
    r"(?<![\w/])/(ip4|ip6|dns|dns4|dns6|dnsaddr)/([^/\s]{1,253})(?:/(tcp|udp)/\d{1,5})?",
    re.IGNORECASE)
_USERINFO_RE = re.compile(
    r"(?<![\w.:@-])[^\s:@/\"'<>()]{1,64}:[^\s\"'<>()]{0,128}@(?=[\w\[])"
    r"([A-Za-z0-9-]{1,63}(?![\w.-]))?(:\d{1,10})?")
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
_LOOKUP_RE = re.compile(r"\b(lookup\s{1,8})(?![" + _SENTINELS + r"])([^\s:]{1,253})",
                        re.IGNORECASE)
_HOST_PORT_RE = re.compile(rf"{_HOST_START}({_DOTTED}):\d{{1,10}}" + _PATH)
_HOST_PATH_RE = re.compile(
    rf"(?<![\w./-])(?:{_LABEL}\.){{1,16}}[A-Za-z]{{2,63}}(?:/|[?#](?=\S))" + _TAIL)
_INTERNAL_NAME_RE = re.compile(
    rf"{_HOST_START}(?:(?:{_LABEL}\.){{1,16}}(?:internal|svc)"
    rf"|(?:{_LABEL}\.){{2,16}}(?:local|lan|home))" + _END,
    re.IGNORECASE)

# "main.go:123" is a source position in a Go error (Bee is written in Go).
# Only .go: other source suffixes such as .py and .rs are country domains.
_SOURCE_SUFFIXES = (".go",)


def _split_url(value):
    parts = urlsplit(value)
    if parts.hostname is None and "://" not in value:
        parts = urlsplit("//" + value)  # "bee:1633" has no scheme
    return parts


def _looks_secret(value):
    # 8 or more characters, and not a plain lowercase word: "facilitator" in
    # https://x402.org/facilitator is a route, not a key.
    return len(value) >= 8 and not re.fullmatch(r"[a-z]+", value)


def _configured():
    """(hosts, secrets) of the configured URLs, read now."""
    hosts, secrets = set(), set()
    for name in _HOST_SETTINGS:
        value = getattr(settings, name, None)
        if not value:
            continue
        try:
            parts = _split_url(str(value))
            host = parts.hostname
        except ValueError:
            continue
        if host:
            hosts.add(host.lower().strip("[]"))
        for segment in parts.path.split("/"):
            segment = unquote(segment)
            if _looks_secret(segment):
                secrets.add(segment)
        for _, query_value in parse_qsl(parts.query, keep_blank_values=False):
            if _looks_secret(query_value):
                secrets.add(query_value)
        if parts.password and len(parts.password) >= 4:
            secrets.add(unquote(parts.password))
    by_length = lambda s: (-len(s), s)  # noqa: E731
    return tuple(sorted(hosts, key=by_length)), tuple(sorted(secrets, key=by_length))


@functools.lru_cache(maxsize=8)
def _configured_res(hosts, secrets):
    secret_re = None
    if secrets:
        names = "|".join(re.escape(s) for s in secrets)
        secret_re = re.compile(rf"(?<![A-Za-z0-9_-])(?:{names})(?![A-Za-z0-9_-])")
    host_re = None
    dotted = "|".join(re.escape(h) for h in hosts if "." in h)
    dotless = "|".join(re.escape(h) for h in hosts if "." not in h)
    forms = []
    if dotted:
        forms.append(rf"(?:{dotted})(?::\d{{1,10}})?{_PATH}")
    if dotless:
        # A port, a path that is not a version ("bee/2.8.1"), or a query.
        forms.append(rf"(?:{dotless})(?::\d{{1,10}}{_PATH}|/(?!\d)" + _TAIL
                     + r"|[?#](?=\S)" + _TAIL + ")")
    if forms:
        host_re = re.compile(rf"{_HOST_START}(?:{'|'.join(forms)}){_END}", re.IGNORECASE)
    return secret_re, host_re


def _multiaddr(match):
    proto = match.group(1)
    hidden = _ADDR if proto.lower() in ("ip4", "ip6") else _HOST
    out = f"/{proto}/{hidden}"
    if match.group(3):
        out += f"/{match.group(3)}/{_PORT}"
    return out


def _x509(match):
    if match.group(1):
        return f"{match.group(1)}{_HOST}{match.group(3)}{_HOST}"
    return f"{match.group(5)}{_HOST}"


def _bracketed_v6(match):
    try:
        ipaddress.IPv6Address(match.group(1))
    except ValueError:
        return match.group(0)  # e.g. a slice such as [0:10]
    return _ADDR


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
    return _ADDR + rest


def _ipv4(match):
    if any(int(match.group(i)) > 255 for i in range(1, 5)):
        return match.group(0)  # a version string such as 1.2.300.4
    return _ADDR


def _host_port(match):
    host = match.group(1)
    if not any(c.isalpha() for c in host) or host.lower().endswith(_SOURCE_SUFFIXES):
        return match.group(0)
    return _ADDR


def _scrub(text, hosts, secrets):
    secret_re, host_re = _configured_res(hosts, secrets)
    if secret_re is not None:
        text = secret_re.sub(_SECRET, text)
    text = _URL_RE.sub(_URL, text)
    text = _MULTIADDR_RE.sub(_multiaddr, text)

    def userinfo(match):
        # Credentials go; a configured host right after them goes too, since
        # a dotless one would otherwise be left as a bare word.
        host, port = match.group(1) or "", match.group(2) or ""
        if host.lower() in hosts:
            return _ADDR if port else _HOST
        return host + port

    text = _USERINFO_RE.sub(userinfo, text)
    text = _X509_RE.sub(_x509, text)
    text = _BRACKETED_V6_RE.sub(_bracketed_v6, text)
    text = _BARE_V6_RE.sub(_bare_v6, text)
    text = _IPV4_RE.sub(_ipv4, text)

    def lookup(match):
        name = match.group(2).rstrip(".,;)")
        tail = match.group(2)[len(name):]
        after = match.string[match.end():match.end() + 4]
        if "." in name or name.lower() in hosts or after == " on ":
            return f"{match.group(1)}{_HOST}{tail}"
        return match.group(0)

    text = _LOOKUP_RE.sub(lookup, text)
    if host_re is not None:
        text = host_re.sub(lambda m: _HOST if m.group(0).lower() in hosts else _ADDR, text)
    text = _HOST_PORT_RE.sub(_host_port, text)
    text = _HOST_PATH_RE.sub(_ADDR, text)
    return _INTERNAL_NAME_RE.sub(_HOST, text)


def _scrub_all(text, hosts, secrets):
    # Placeholder characters in the input itself are not ours; drop them.
    text = text.translate({ord(c): None for c in _SENTINELS})
    parts = _RFC3339_RE.split(text)
    # re.split with one group: even indices are text, odd are timestamps.
    return "".join(p if i % 2 else _scrub(p, hosts, secrets) for i, p in enumerate(parts))


def _replacements(text):
    return sum(text.count(c) for c in _SENTINELS)


def _decode(text):
    # Printable ASCII only: "%ff" is not text and stays as it is.
    return _PCT_RE.sub(lambda m: chr(int(m.group(1), 16)) if m.group(1).lower() != "7f"
                       else m.group(0), text)


def without_addresses(text: str) -> str:
    """Replace URLs, IP addresses, configured hosts and secrets in text."""
    hosts, secrets = _configured()
    scrubbed = _scrub_all(text, hosts, secrets)
    decoded = text
    for _ in range(2):
        if not _PCT_RE.search(decoded):
            break
        decoded = _decode(decoded)
    if decoded != text:
        # Use the decoded form when it has something to replace, and at least
        # as much as the encoded one ("bee%2Einternal%3A1633"); otherwise keep
        # the text's own encoding ("100%ff", "50%25").
        decoded_scrubbed = _scrub_all(decoded, hosts, secrets)
        found = _replacements(decoded_scrubbed)
        if found and found >= _replacements(scrubbed):
            scrubbed = decoded_scrubbed
    return scrubbed.translate(_FINAL)


# Bee's text is scrubbed at this length, then cut for the response. The
# scrubber is linear, so the larger cap costs little, and an address that
# straddles the response cut is still recognised whole.
SCRUB_CAP = 2048


def public_bee_text(text: str, limit: int = 200) -> str:
    """Bee's own message, scrubbed and cut to `limit` for a public response.

    The text is scrubbed at SCRUB_CAP characters first. Wherever it is cut,
    at SCRUB_CAP or at `limit`, a word the cut lands inside is dropped and
    "…" added, so no partial address is shown.
    """
    text = str(text)
    if len(text) > SCRUB_CAP:
        text = _drop_partial(text[:SCRUB_CAP], text[SCRUB_CAP])
    scrubbed = without_addresses(text)
    if len(scrubbed) <= limit:
        return scrubbed
    return _drop_partial(scrubbed[:limit], scrubbed[limit])


def _drop_partial(cut: str, next_char: str) -> str:
    if not next_char.isspace() and cut and not cut[-1].isspace():
        head = cut.rsplit(None, 1)
        cut = head[0] if len(head) == 2 else ""
    return cut.rstrip() + "…"
