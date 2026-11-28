"""Forged caller identities do not get past the per-caller limits (#697).

With no proxy in front, every forwarding header is whatever the caller sent.
Two forms used to pass every limit: X-Real-IP, read whenever X-Forwarded-For
was absent, so a new value was a new budget; and the literal "unknown", which
the x402 free-tier limiter did not limit at all. Measured before the fix on a
local gateway with a free-tier limit of 3: 6 of 6 requests passed for
`X-Real-IP: unknown`, `X-Forwarded-For: unknown` and a rotating X-Real-IP.

These tests drive the real limiters through HTTP, with a real TCP peer address
set on the test client, so the forged header is the only thing that varies.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.core.client_ip import UNKNOWN, get_client_ip, get_client_key
from app.core.config import settings

PEER = ("192.0.2.10", 50000)       # the attacker's real connection
PROXY = ("192.0.2.1", 50000)       # a reverse proxy in front of the gateway
LIMIT = 3
SENT = 6

# One header set per request. Each list is one spoofing form from the issue.
SPOOFS = {
    "x-real-ip-unknown": [{"X-Real-IP": "unknown"}] * SENT,
    "x-forwarded-for-unknown": [{"X-Forwarded-For": "unknown"}] * SENT,
    "x-forwarded-for-empty": [{"X-Forwarded-For": " , "}] * SENT,
    "rotating-x-real-ip": [{"X-Real-IP": f"192.0.2.{100 + i}"} for i in range(SENT)],
    "rotating-x-real-ipv6": [{"X-Real-IP": f"2001:db8:{i}::1"} for i in range(SENT)],
    "rotating-garbage-x-forwarded-for": [{"X-Forwarded-For": f"garbage-{i}"} for i in range(SENT)],
    "x-forwarded-for-with-port": [{"X-Forwarded-For": f"192.0.2.50:{1000 + i}"} for i in range(SENT)],
    "x-forwarded-for-bad-ipv6": [{"X-Forwarded-For": f"2001:db8::zz{i}"} for i in range(SENT)],
    "x-forwarded-for-bracketed-ipv6": [{"X-Forwarded-For": f"[2001:db8:{i}::1]"} for i in range(SENT)],
    "x-forwarded-for-out-of-range": [{"X-Forwarded-For": f"999.0.2.{i}"} for i in range(SENT)],
    "x-forwarded-for-zone-id": [{"X-Forwarded-For": f"2001:db8::1%zone{i}"} for i in range(SENT)],
    "x-forwarded-for-long-zone-id": [{"X-Forwarded-For": "2001:db8::1%" + str(i) * 4000} for i in range(SENT)],
    "x-forwarded-for-long-garbage": [{"X-Forwarded-For": str(i) * 6000} for i in range(SENT)],
}


def _request(headers=None, peer=None):
    r = MagicMock(spec=Request)
    r.headers = headers or {}
    if peer is None:
        r.client = None
    else:
        r.client = MagicMock()
        r.client.host = peer
    return r


# --- the global limiter (RateLimitMiddleware) --------------------------------

def _global_app():
    from app.middleware.rate_limit import RateLimitMiddleware, SlidingWindowCounter
    app = FastAPI()

    @app.get("/x")
    async def x():
        return {"ok": True}

    app.add_middleware(RateLimitMiddleware, counter=SlidingWindowCounter())
    return app


@pytest.fixture
def global_limit(monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(settings, "RATE_LIMIT_PER_MINUTE", LIMIT)
    monkeypatch.setattr(settings, "RATE_LIMIT_BURST", 0)
    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)


@pytest.mark.parametrize("form", sorted(SPOOFS))
def test_global_limit_holds_against_spoofed_headers(global_limit, form):
    client = TestClient(_global_app(), client=PEER)
    codes = [client.get("/x", headers=h).status_code for h in SPOOFS[form]]
    assert codes == [200] * LIMIT + [429] * (SENT - LIMIT), (form, codes)


def test_global_limit_keeps_callers_behind_one_proxy_apart(global_limit):
    """Two clients behind one trusted proxy hop each keep their own budget."""
    client = TestClient(_global_app(), client=PROXY)
    for ip in ("192.0.2.20", "192.0.2.21"):
        codes = [client.get("/x", headers={"X-Forwarded-For": ip}).status_code for _ in range(LIMIT + 1)]
        assert codes == [200] * LIMIT + [429], (ip, codes)


# --- the x402 free-tier limiter, through the stamps router -------------------

@pytest.fixture
def free_tier(monkeypatch, tmp_path):
    """x402 on with the free tier, through POST /api/v1/stamps/, Bee mocked.

    app.main attaches the x402 dependency only if x402 was on when it was first
    imported, so the router is mounted here with it, as app.main does.
    """
    from app.api.endpoints import stamps as stamps_ep
    from app.services.spend_budget import SpendBudgetTracker
    from app.x402 import dependency
    from app.x402.ratelimit import reset_rate_limiter

    monkeypatch.setattr(settings, "X402_ENABLED", True)
    monkeypatch.setattr(settings, "X402_FREE_TIER_ENABLED", True)
    monkeypatch.setattr(settings, "X402_FREE_TIER_RATE_LIMIT", LIMIT)
    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)
    # The daily spend budget is keyed on the same caller; keep it out of the
    # way so a 429 here can only be the free-tier rate limit.
    monkeypatch.setattr(stamps_ep, "spend_budget_tracker",
                        SpendBudgetTracker(state_file=str(tmp_path / "spend.json")))
    monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", 1000.0)
    reset_rate_limiter()
    with patch.object(dependency, "_calculate_price_for_request",
                      new=AsyncMock(return_value={"price_usd": 0.05, "description": "x"})), \
         patch("app.services.swarm_api.get_chainstate",
               new=AsyncMock(return_value={"currentPrice": "24000", "minimumValidityBlocks": 17280})), \
         patch("app.services.swarm_api.check_sufficient_funds",
               new=AsyncMock(return_value={"sufficient": True, "wallet_balance_bzz": 1.0,
                                           "required_bzz": 0.01, "shortfall_bzz": 0.0})), \
         patch("app.services.swarm_api.purchase_postage_stamp", new=AsyncMock(return_value="b" * 64)):
        yield
    reset_rate_limiter()


def _stamps_app():
    from fastapi import Depends
    from app.api.endpoints import stamps
    from app.x402.dependency import require_x402_payment
    app = FastAPI()
    app.include_router(stamps.router, prefix=f"{settings.API_V1_STR}/stamps",
                       dependencies=[Depends(require_x402_payment)])
    return app


def _free_posts(peer, header_sets):
    client = TestClient(_stamps_app(), client=peer)
    out = []
    for h in header_sets:
        r = client.post("/api/v1/stamps/", json={"depth": 17, "duration_hours": 24},
                        headers={"X-Payment-Mode": "free", **h})
        if r.status_code == 429:
            # Only the free-tier limiter's 429 counts; anything else is a
            # different control and would make this test prove nothing.
            assert "Free tier rate limit exceeded" in r.text, r.text[:300]
        out.append(r.status_code)
    return out


@pytest.mark.parametrize("form", sorted(SPOOFS))
def test_free_tier_limit_holds_against_spoofed_headers(free_tier, form):
    codes = _free_posts(PEER, SPOOFS[form])
    assert codes == [201] * LIMIT + [429] * (SENT - LIMIT), (form, codes)


def test_free_tier_keeps_callers_behind_one_proxy_apart(free_tier):
    for ip in ("192.0.2.20", "192.0.2.21"):
        codes = _free_posts(PROXY, [{"X-Forwarded-For": ip}] * (LIMIT + 1))
        assert codes == [201] * LIMIT + [429], (ip, codes)


def test_free_tier_groups_an_ipv6_64_behind_the_proxy(free_tier):
    """IPv6 grouping by /64 is unchanged: rotating within one /64 shares it,
    another /64 has its own."""
    rotating = [{"X-Forwarded-For": f"2001:db8:9:9::{i}"} for i in range(1, SENT + 1)]
    codes = _free_posts(PROXY, rotating)
    assert codes == [201] * LIMIT + [429] * (SENT - LIMIT), codes
    assert _free_posts(PROXY, [{"X-Forwarded-For": "2001:db8:9:a::1"}]) == [201]


@pytest.mark.parametrize("headers", [
    {},
    {"X-Forwarded-For": "unknown"},
    {"X-Real-IP": "unknown"},
    {"X-Forwarded-For": "garbage"},
    {"X-Real-IP": "192.0.2.99"},
], ids=["none", "xff-unknown", "xri-unknown", "xff-garbage", "xri-address"])
def test_free_tier_limits_peerless_callers_as_one_shared_caller(free_tier, headers):
    """No peer and no usable header: one shared bucket, limited, end to end.
    The x402 limiter used to skip this key entirely."""
    codes = _free_posts(None, [headers] * SENT)
    assert codes == [201] * LIMIT + [429] * (SENT - LIMIT), codes


def test_peerless_callers_share_the_bucket_whatever_they_send(free_tier):
    """Different forged values with no peer are the same caller."""
    forms = [{}, {"X-Forwarded-For": "unknown"}, {"X-Real-IP": "192.0.2.98"},
             {"X-Forwarded-For": "garbage"}, {"X-Real-IP": "unknown"}, {}]
    assert _free_posts(None, forms) == [201] * LIMIT + [429] * (SENT - LIMIT)


def test_a_peerless_caller_is_limited_not_exempt():
    """No peer and no usable header is one shared caller, limited like any
    other (the x402 limiter used to skip it)."""
    from app.x402.ratelimit import RateLimiter
    limiter = RateLimiter(requests_per_minute=LIMIT)
    keys = [get_client_key(_request({"X-Forwarded-For": "unknown"})) for _ in range(SENT)]
    assert set(keys) == {UNKNOWN}
    results = [limiter.is_rate_limited(k)[0] for k in keys]
    assert results == [False] * LIMIT + [True] * (SENT - LIMIT)


# --- the parsing rules behind the above --------------------------------------

class TestOnlyAddressesBecomeKeys:
    @pytest.fixture(autouse=True)
    def _one_hop(self, monkeypatch):
        monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)

    @pytest.mark.parametrize("value", ["unknown", "garbage", "", " , ", "999.1.1.1",
                                       "2001:db8::zz", "192.0.2.5:8080", "[2001:db8::5]",
                                       "_hidden", "192.0.2.5 192.0.2.6"])
    def test_an_invalid_entry_falls_back_to_the_peer(self, value):
        assert get_client_ip(_request({"X-Forwarded-For": value}, peer="192.0.2.10")) == "192.0.2.10"

    @pytest.mark.parametrize("value", ["fe80::1%eth0", "2001:db8::1%1", "2001:db8::1%zone",
                                       "::ffff:192.0.2.1%x",
                                       pytest.param("2001:db8::1%" + "z" * 5000, id="long-zone-id")])
    def test_an_ipv6_zone_id_falls_back_to_the_peer(self, value):
        assert get_client_ip(_request({"X-Forwarded-For": value}, peer="192.0.2.10")) == "192.0.2.10"

    def test_a_long_zone_id_is_not_logged_in_full(self, caplog):
        value = "2001:db8::1%" + "z" * 5000
        with caplog.at_level("WARNING", logger="app.core.client_ip"):
            get_client_ip(_request({"X-Forwarded-For": value}, peer="192.0.2.10"))
        assert caplog.records and all(len(r.getMessage()) < 300 for r in caplog.records)

    @pytest.mark.parametrize("value", ["192.0.2.1", "::ffff:192.0.2.1", "0:0:0:0:0:ffff:c000:201",
                                       "::FFFF:192.0.2.1"])
    def test_ipv4_mapped_forms_share_the_ipv4_key(self, value):
        r = _request({"X-Forwarded-For": value}, peer="192.0.2.250")
        assert get_client_key(r) == "192.0.2.1"

    def test_a_very_long_header_still_finds_the_proxy_entry(self):
        prefix = ", ".join(f"garbage-{i}" for i in range(2000))
        r = _request({"X-Forwarded-For": f"{prefix}, 192.0.2.20"}, peer="192.0.2.1")
        assert get_client_ip(r) == "192.0.2.20"

    def test_a_very_long_invalid_entry_falls_back_to_the_peer(self):
        r = _request({"X-Forwarded-For": "x" * 20000}, peer="192.0.2.10")
        assert get_client_ip(r) == "192.0.2.10"

    def test_an_invalid_chosen_entry_with_a_valid_one_right_of_it_falls_back(self, monkeypatch):
        """hops=2: the chosen entry is invalid and the proxy's entry is valid.
        The valid one is the proxy's address, not the caller's, so neither may
        be used."""
        monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 2)
        r = _request({"X-Forwarded-For": "192.0.2.30, unknown, 192.0.2.2"}, peer="192.0.2.1")
        assert get_client_ip(r) == "192.0.2.1"

    def test_x_real_ip_is_never_read(self):
        r = _request({"X-Real-IP": "192.0.2.99"}, peer="192.0.2.10")
        assert get_client_ip(r) == "192.0.2.10"

    def test_a_valid_ipv6_entry_is_kept_and_normalised(self):
        r = _request({"X-Forwarded-For": "2001:DB8:1:2:0:0:0:5"}, peer="192.0.2.1")
        assert get_client_ip(r) == "2001:db8:1:2::5"
        assert get_client_key(r) == "2001:db8:1:2::/64"

    def test_a_garbage_prefix_left_of_the_proxy_entry_is_irrelevant(self):
        """An appending proxy leaves the caller's own text on the left; only
        the entries our proxies wrote are checked."""
        r = _request({"X-Forwarded-For": "unknown, garbage, 192.0.2.20"}, peer="192.0.2.1")
        assert get_client_ip(r) == "192.0.2.20"

    def test_an_invalid_entry_inside_the_trusted_hops_falls_back(self, monkeypatch):
        monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 2)
        r = _request({"X-Forwarded-For": "192.0.2.20, unknown"}, peer="192.0.2.1")
        assert get_client_ip(r) == "192.0.2.1"

    def test_separate_header_lines_are_one_list(self):
        """A caller's own X-Forwarded-For line must not win over the one a
        proxy adds as a second field: the rightmost entry overall counts."""
        from starlette.datastructures import Headers
        headers = Headers(raw=[(b"x-forwarded-for", b"192.0.2.99"),
                               (b"x-forwarded-for", b"192.0.2.20")])
        assert get_client_ip(_request(headers, peer="192.0.2.1")) == "192.0.2.20"

    def test_no_peer_and_no_usable_header_is_unknown(self):
        assert get_client_ip(_request({"X-Forwarded-For": "unknown", "X-Real-IP": "192.0.2.9"})) == UNKNOWN

    def test_the_peer_is_used_as_it_is(self):
        """The peer cannot be forged, so it is not required to be an address."""
        assert get_client_ip(_request({}, peer="testclient")) == "testclient"
