"""Identifying the caller, for limits that are keyed on who is calling.

Three controls depend on this: the global per-IP rate limit, the x402 free-tier
rate limit, and the daily spend budget (#102). A caller who can choose this value
is not limited by any of them.

The previous implementation took the FIRST entry of `X-Forwarded-For`, which is
the one furthest from us and is caller-supplied if any proxy in the chain appends
rather than replaces. It was not exploitable in the deployed configuration —
verified against staging on 2026-09-09 by exhausting the free-tier limit and
retrying with forged values, all still refused — but the safety lived entirely in
the proxy's configuration, nothing recorded that dependency, and no test pinned
it. These tests pin it.
"""
from unittest.mock import MagicMock

import pytest
from fastapi import Request

from app.core.client_ip import UNKNOWN, get_client_ip
from app.core.config import settings

CLIENT = "203.0.113.50"
FORGED = "9.9.9.9"


def _request(headers=None, peer=None):
    r = MagicMock(spec=Request)
    r.headers = headers or {}
    if peer is None:
        r.client = None
    else:
        r.client = MagicMock()
        r.client.host = peer
    return r


class TestForgingTheHeader:
    """The property the old implementation did not have."""

    def test_a_forged_prefix_cannot_change_the_answer(self, monkeypatch):
        """A caller prepending entries must not be able to pick their identity.

        With one proxy in front, everything left of the last entry came from
        outside and carries no authority.
        """
        monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)
        for forged_chain in (
            f"{FORGED}, {CLIENT}",
            f"{FORGED}, 8.8.8.8, {CLIENT}",
            f"{FORGED},{FORGED},{FORGED}, {CLIENT}",
        ):
            got = get_client_ip(_request({"X-Forwarded-For": forged_chain}))
            assert got == CLIENT, f"{forged_chain!r} yielded {got}"

    def test_rotating_the_header_does_not_produce_a_new_identity(self, monkeypatch):
        """This is the failure mode in full: a caller changing the header on
        every request would get a fresh rate-limit window and a fresh spend
        budget each time."""
        monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)
        seen = {
            get_client_ip(_request({"X-Forwarded-For": f"10.0.0.{i}, {CLIENT}"}))
            for i in range(20)
        }
        assert seen == {CLIENT}, f"rotation produced {len(seen)} identities"

    def test_the_old_behaviour_is_gone(self, monkeypatch):
        """Explicit, because this is what the previous code and its test did."""
        monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)
        assert get_client_ip(
            _request({"X-Forwarded-For": f"{FORGED}, {CLIENT}"})
        ) != FORGED


class TestProxyBehaviourIndependence:
    """The point of counting hops is not depending on something we do not control.

    Caddy currently replaces the header. If it were configured with
    trusted_proxies, or replaced by a load balancer that appends, the answer must
    not change.
    """

    def test_a_replacing_proxy_and_an_appending_proxy_agree(self, monkeypatch):
        monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)
        replaced = get_client_ip(_request({"X-Forwarded-For": CLIENT}))
        appended = get_client_ip(_request({"X-Forwarded-For": f"{FORGED}, {CLIENT}"}))
        assert replaced == appended == CLIENT

    def test_two_hops_looks_one_further_out(self, monkeypatch):
        """A CDN in front of Caddy: the last entry is Caddy's view of the CDN,
        and the caller is one step further left."""
        monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 2)
        header = f"{CLIENT}, 172.16.0.1"
        assert get_client_ip(_request({"X-Forwarded-For": header})) == CLIENT

    def test_a_forged_prefix_still_cannot_reach_past_two_hops(self, monkeypatch):
        monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 2)
        header = f"{FORGED}, {CLIENT}, 172.16.0.1"
        assert get_client_ip(_request({"X-Forwarded-For": header})) == CLIENT


class TestDirectExposure:
    def test_zero_hops_ignores_the_headers_entirely(self, monkeypatch):
        """Half-trusting a header when nothing is in front is worse than using
        the connection we actually have."""
        monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 0)
        r = _request({"X-Forwarded-For": FORGED, "X-Real-IP": FORGED}, peer="192.0.2.7")
        assert get_client_ip(r) == "192.0.2.7"

    def test_zero_hops_with_no_peer_is_unknown(self, monkeypatch):
        monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 0)
        assert get_client_ip(_request({"X-Forwarded-For": FORGED})) == UNKNOWN


class TestMisconfiguration:
    """Getting the setting wrong should fail in the safe direction.

    Too high groups callers together, which over-limits and shows up as
    complaints. Too low lets a caller choose their own identity, which
    under-limits and shows up as nothing.
    """

    def test_a_chain_shorter_than_the_hop_count_falls_back_to_the_peer(self, monkeypatch):
        monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 3)
        r = _request({"X-Forwarded-For": f"{FORGED}, {CLIENT}"}, peer="192.0.2.7")
        assert get_client_ip(r) == "192.0.2.7", \
            "a short chain must not fall through to a caller-supplied entry"

    def test_the_fallback_never_returns_a_header_value(self, monkeypatch):
        """The dangerous version of the above would clamp to index 0 and hand
        back the leftmost entry — exactly the value an attacker controls."""
        monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 5)
        r = _request({"X-Forwarded-For": f"{FORGED}, {CLIENT}"}, peer="192.0.2.7")
        assert get_client_ip(r) not in (FORGED, CLIENT)


class TestOrdinaryCases:
    def test_a_single_entry_is_the_caller(self, monkeypatch):
        monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)
        assert get_client_ip(_request({"X-Forwarded-For": CLIENT})) == CLIENT

    def test_whitespace_and_empty_entries_are_ignored(self, monkeypatch):
        monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)
        assert get_client_ip(_request({"X-Forwarded-For": f" {FORGED} , , {CLIENT} "})) == CLIENT

    def test_real_ip_is_used_when_forwarded_for_is_absent(self, monkeypatch):
        monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)
        assert get_client_ip(_request({"X-Real-IP": CLIENT})) == CLIENT

    def test_forwarded_for_wins_over_real_ip(self, monkeypatch):
        monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)
        r = _request({"X-Forwarded-For": CLIENT, "X-Real-IP": "10.0.0.1"}, peer="192.0.2.7")
        assert get_client_ip(r) == CLIENT

    def test_no_headers_falls_back_to_the_peer(self, monkeypatch):
        monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)
        assert get_client_ip(_request({}, peer="192.0.2.7")) == "192.0.2.7"

    def test_nothing_at_all_is_unknown(self, monkeypatch):
        monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)
        assert get_client_ip(_request({})) == UNKNOWN


class TestOneImplementation:
    """Two copies of this existed and both had the same flaw. If they diverge
    again, one control gets fixed and another silently does not."""

    def test_every_module_uses_the_same_function(self):
        from app.core.client_ip import get_client_ip as canonical
        from app.middleware.rate_limit import get_client_ip as rate_limit_copy
        from app.x402.middleware import get_client_ip as x402_copy
        from app.api.endpoints.stamps import get_client_ip as stamps_copy

        assert rate_limit_copy is canonical
        assert x402_copy is canonical
        assert stamps_copy is canonical


class TestTheLimitsUseIt:
    """A correct helper nothing calls would fix nothing."""

    @pytest.mark.parametrize("chain,expected", [
        (f"{FORGED}, {CLIENT}", CLIENT),
        (f"8.8.8.8, 1.1.1.1, {CLIENT}", CLIENT),
    ])
    def test_the_spend_budget_charges_the_real_caller(self, monkeypatch, chain, expected):
        from unittest.mock import AsyncMock, patch
        from fastapi.testclient import TestClient
        from app.main import app
        from app.services.spend_budget import SpendBudgetTracker

        monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)
        monkeypatch.setattr(settings, "X402_MAX_STAMP_BZZ", 0.0)
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", -1.0)

        import app.api.endpoints.stamps as stamps_ep
        tracker = SpendBudgetTracker(state_file=None)
        tracker._state_file = "/dev/null"
        monkeypatch.setattr(stamps_ep, "spend_budget_tracker", tracker)

        with patch("app.services.swarm_api.get_chainstate",
                   new=AsyncMock(return_value={"currentPrice": "24000"})), \
             patch("app.services.swarm_api.check_sufficient_funds",
                   new=AsyncMock(return_value={"sufficient": True, "required_bzz": 0.01,
                                               "wallet_balance_bzz": 100.0,
                                               "shortfall_bzz": 0.0})), \
             patch("app.services.swarm_api.purchase_postage_stamp",
                   new=AsyncMock(return_value="b" * 64)):
            r = TestClient(app).post("/api/v1/stamps/",
                                     json={"depth": 17, "duration_hours": 24},
                                     headers={"X-Forwarded-For": chain})

        assert r.status_code == 201, r.text
        assert list(tracker.snapshot()["spent"]) == [expected], \
            "the spend was charged to a caller-supplied address"
