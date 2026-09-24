# app/services/client_type.py
"""Classify a caller into a small fixed set of client types.

Production traffic turned out to be almost entirely one client —
`swarm-provenance-mcp`, run by AI agents in cloud sandboxes — and 37 of its 84
production requests were refused with 429, because the free tier allows three
requests a minute and an agent making a few calls in sequence exceeds it. That
is a usability problem affecting the gateway's intended audience, and it was
invisible: finding it meant reading reverse-proxy access logs by hand on the
host.

## Why a fixed table rather than the User-Agent itself

A metric label must never take a value the caller chooses. `User-Agent` is
caller-supplied and unbounded, so using it directly would let anyone create
unlimited time series — a denial-of-wallet on the metrics bill, and a Prometheus
instance that slows down for everyone. Matching against a fixed table and
falling back to "other" keeps the label space at six values no matter what
arrives.

## Why no per-caller identity here

Deliberately none. A hashed or truncated IP is pseudonymised, not anonymised —
still personal data — and still high-cardinality, so it fails on privacy and on
cost simultaneously. There is no version of per-caller identity in a metric
label that is both useful and safe.

Counts and buckets, never per-user records. Per-caller detail belongs in the
logs: short retention, access-controlled, and read when someone is actually
investigating rather than exported continuously to a third party.
"""
import re
from typing import Optional

# Ordered, because a caller can match more than one pattern and the first match
# should be the most specific. `swarm-provenance-mcp/0.1.0` contains neither
# "python" nor "httpx", but a future version built on requests might, and the
# client it IS matters more than the library it happens to use.
_PATTERNS = [
    ("mcp", re.compile(r"swarm[-_]provenance[-_]mcp", re.I)),
    ("cli", re.compile(r"swarm[-_]prov[-_]upload|swarm[-_]provenance[-_]cli", re.I)),
    ("sdk-js", re.compile(r"@datafund/swarm-provenance|swarm-provenance-sdk", re.I)),
    # Browsers before generic HTTP libraries: a browser UA also contains tokens
    # like "Mozilla" that nothing else should be matched on.
    ("browser", re.compile(r"Mozilla/|Chrome/|Safari/|Firefox/|Edg/", re.I)),
    ("curl", re.compile(r"^curl/|^Wget/", re.I)),
    ("http-lib", re.compile(r"python-httpx|python-requests|aiohttp|Go-http-client|okhttp|axios|node-fetch", re.I)),
    ("bot", re.compile(r"bot\b|crawler|spider|facebookexternalhit|slackbot|bingpreview", re.I)),
]

OTHER = "other"
NONE = "none"

# Every value this function can return. Exported so a test can assert the label
# space stays bounded, and so a dashboard can enumerate it.
CLIENT_TYPES = tuple([name for name, _ in _PATTERNS] + [OTHER, NONE])


def classify(user_agent: Optional[str]) -> str:
    """Bucket a User-Agent into one of CLIENT_TYPES.

    An absent or empty header is "none" rather than "other": a caller sending no
    User-Agent at all is a meaningfully different population from one sending
    something unrecognised, and both are worth being able to see.
    """
    if not user_agent or not user_agent.strip():
        return NONE
    for name, pattern in _PATTERNS:
        if pattern.search(user_agent):
            return name
    return OTHER


def outcome_for(status_code: int) -> str:
    """Bucket a response status into why the caller did or did not get served.

    Coarser than the status code on purpose. The question this answers is "are
    we serving this client or turning it away, and why" — `http_requests_total`
    already carries the exact code for anyone who needs it.

    402 and 429 are separated from other client errors because they are the two
    that mean "you are being refused by policy rather than for a mistake", and
    they are the ones worth acting on.
    """
    if status_code >= 500:
        return "server_error"
    if status_code == 429:
        return "rate_limited"
    if status_code == 402:
        return "payment_required"
    if status_code >= 400:
        return "client_error"
    return "ok"
