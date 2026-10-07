"""Which clients the gateway serves, and whether it serves them (#347).

Production traffic turned out to be almost entirely one client — the MCP plugin,
run by AI agents in cloud sandboxes — and 37 of its 84 production requests were
refused with 429, because the free tier allows three requests a minute. That was
invisible until someone read reverse-proxy access logs by hand on the host.

The constraint these tests exist to protect: a metric label must never take a
value the caller chooses, or anyone can mint unlimited time series.
"""
from prometheus_client import REGISTRY

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.middleware.client_metrics import EXCLUDED_PATHS, distinct_caller_counter
from app.services.client_type import CLIENT_TYPES, NONE, OTHER, classify, outcome_for

client = TestClient(app)


def _count(client_type, outcome):
    v = REGISTRY.get_sample_value(
        "gateway_requests_by_client_total",
        {"client_type": client_type, "outcome": outcome},
    )
    return 0.0 if v is None else v


class TestClassification:
    @pytest.mark.parametrize("ua,expected", [
        ("swarm-provenance-mcp/0.1.0", "mcp"),
        ("swarm_provenance_mcp/1.2.3", "mcp"),
        ("swarm-prov-upload/0.4.0", "cli"),
        ("@datafund/swarm-provenance/2.0.0", "sdk-js"),
        ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36", "browser"),
        ("curl/8.7.1", "curl"),
        ("Wget/1.21", "curl"),
        ("python-httpx/0.28.1", "http-lib"),
        ("python-requests/2.32.4", "http-lib"),
        ("Go-http-client/2.0", "http-lib"),
        ("facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)", "bot"),
        ("Googlebot/2.1", "bot"),
    ])
    def test_real_user_agents_seen_in_production(self, ua, expected):
        """Every one of these was observed in the gateway's own access logs."""
        assert classify(ua) == expected

    def test_an_absent_header_is_distinct_from_an_unrecognised_one(self):
        """A caller sending no User-Agent is a different population from one
        sending something we do not know, and both are worth seeing."""
        assert classify(None) == NONE
        assert classify("") == NONE
        assert classify("   ") == NONE
        assert classify("SomeThingNobodyHasEverSeen/9") == OTHER

    def test_a_browser_is_not_classified_by_its_http_library(self):
        """Browser UAs contain library-ish tokens; the client matters, not the
        transport. Ordering in the pattern table is what guarantees this."""
        assert classify("Mozilla/5.0 AppleWebKit/537.36 Chrome/120 Safari/537.36") == "browser"

    def test_the_mcp_is_matched_on_its_own_name_not_its_library(self):
        """A future MCP release built on requests must still read as mcp."""
        assert classify("swarm-provenance-mcp/0.2.0 python-requests/2.32.4") == "mcp"


class TestTheLabelSpaceIsBounded:
    """The reason the classification is a fixed table rather than the header.

    User-Agent is caller-supplied and unbounded. Using it as a label directly
    would let anyone create unlimited series — a cost attack on the metrics bill
    and a Prometheus instance that slows down for everyone.
    """

    @pytest.mark.parametrize("hostile", [
        "a" * 5000,
        "x/1.0\nx/2.0",
        "../../etc/passwd",
        '{"json":"injection"}',
        "unique-id-" + "9" * 40,
        "mcp\x00null",
    ])
    def test_no_input_can_invent_a_label_value(self, hostile):
        assert classify(hostile) in CLIENT_TYPES

    def test_the_client_type_space_stays_small(self):
        """Eight types by five outcomes is 40 series per environment. If this
        grows, the cost of the counter grows with it — worth noticing."""
        assert len(CLIENT_TYPES) <= 10, CLIENT_TYPES

    def test_outcomes_are_a_closed_set(self):
        seen = {outcome_for(code) for code in
                (200, 201, 204, 301, 400, 401, 402, 403, 404, 413, 422, 429, 500, 502, 503)}
        assert seen == {"ok", "client_error", "payment_required", "rate_limited", "server_error"}


class TestOutcomeBuckets:
    def test_refusals_by_policy_are_separated_from_mistakes(self):
        """402 and 429 mean "you are being refused by policy", which is the
        actionable case. Folding them into client_error would have hidden the
        429 problem that prompted this work."""
        assert outcome_for(429) == "rate_limited"
        assert outcome_for(402) == "payment_required"
        assert outcome_for(422) == "client_error"
        assert outcome_for(413) == "client_error"

    def test_success_and_redirects_are_ok(self):
        assert outcome_for(200) == "ok"
        assert outcome_for(201) == "ok"
        assert outcome_for(304) == "ok"


class TestTheMiddlewareRecords:
    def test_a_served_request_is_counted_under_its_client_type(self):
        before = _count("mcp", "client_error")
        client.get("/api/v1/data/not-a-valid-reference",
                   headers={"User-Agent": "swarm-provenance-mcp/0.1.0"})
        assert _count("mcp", "client_error") > before

    @pytest.mark.parametrize("status,outcome", [
        (402, "payment_required"),
        (429, "rate_limited"),
        (500, "server_error"),
        (422, "client_error"),
        (200, "ok"),
    ])
    def test_every_outcome_is_recorded_through_the_real_middleware(self, status, outcome):
        """A client being turned away must be visible as such — the whole point.

        Driven through a minimal app rather than the real one, so each outcome is
        produced deterministically instead of depending on whether the x402 stack
        or the rate limiter happens to fire. The middleware under test is the
        real one.
        """
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse
        from app.middleware.client_metrics import ClientMetricsMiddleware

        probe = FastAPI()

        @probe.get("/api/v1/probe")
        async def _probe():
            return JSONResponse({"x": 1}, status_code=status)

        probe.add_middleware(ClientMetricsMiddleware)

        before = _count("mcp", outcome)
        TestClient(probe).get("/api/v1/probe",
                              headers={"User-Agent": "swarm-provenance-mcp/0.1.0"})
        assert _count("mcp", outcome) == before + 1

    def test_an_unhandled_exception_is_still_counted(self):
        """A request that blew up is exactly the one worth seeing. Recording in a
        finally block is what makes that true."""
        from fastapi import FastAPI
        from app.middleware.client_metrics import ClientMetricsMiddleware

        probe = FastAPI()

        @probe.get("/api/v1/boom")
        async def _boom():
            raise RuntimeError("deliberate")

        probe.add_middleware(ClientMetricsMiddleware)

        before = _count("curl", "server_error")
        with pytest.raises(RuntimeError):
            TestClient(probe).get("/api/v1/boom", headers={"User-Agent": "curl/8.7.1"})
        assert _count("curl", "server_error") == before + 1

    def test_scrapes_and_healthchecks_are_not_counted(self):
        """Alloy scrapes /metrics every 15s and Docker checks / every 30s. Those
        are the two most frequent requests the gateway ever sees, and neither is
        a client being served — counting them would bury everything else."""
        totals_before = sum(
            _count(ct, o) for ct in CLIENT_TYPES
            for o in ("ok", "client_error", "payment_required", "rate_limited", "server_error")
        )
        for path in ("/metrics", "/health", "/"):
            client.get(path, headers={"User-Agent": "curl/8.7.1"})
        totals_after = sum(
            _count(ct, o) for ct in CLIENT_TYPES
            for o in ("ok", "client_error", "payment_required", "rate_limited", "server_error")
        )
        assert totals_after == totals_before

    def test_the_excluded_paths_are_the_ones_we_think(self):
        assert {"/metrics", "/health", "/"} <= EXCLUDED_PATHS


class TestDistinctCallersExportsOnlyACount:
    """Counting distinctness needs to recognise a repeat caller, which needs some
    per-caller value in memory. These pin that only the count ever leaves."""

    def test_repeat_callers_count_once(self):
        c = distinct_caller_counter
        c._seen.clear()
        for _ in range(5):
            c.observe("1.2.3.4")
        c.observe("5.6.7.8")
        assert len(c._seen) == 2

    def test_no_address_is_retained(self):
        """A salted hash, not the address. The salt is random per process, so the
        stored values cannot be reversed, cannot be checked against a
        precomputed table, and cannot be correlated across restarts."""
        c = distinct_caller_counter
        c._seen.clear()
        c.observe("203.0.113.50")
        stored = b"".join(c._seen)
        assert b"203.0.113.50" not in stored
        assert b"203.0.113" not in stored

    def test_two_processes_produce_different_values_for_one_caller(self):
        """Without a per-process salt, the same address would hash identically
        everywhere, which is a stable pseudonymous identifier — the thing this is
        specifically avoiding."""
        from app.middleware.client_metrics import _DistinctCallerCounter
        a, b = _DistinctCallerCounter(), _DistinctCallerCounter()
        a.observe("203.0.113.50")
        b.observe("203.0.113.50")
        assert a._seen != b._seen

    def test_the_count_resets_on_a_new_day(self, monkeypatch):
        from app.middleware import client_metrics
        c = client_metrics._DistinctCallerCounter()
        c.observe("1.2.3.4")
        c.observe("5.6.7.8")
        assert len(c._seen) == 2

        monkeypatch.setattr(c, "_today", staticmethod(lambda: "2099-01-01"))
        c.observe("9.9.9.9")
        assert len(c._seen) == 1, "yesterday's callers still counted toward today"

    def test_no_caller_identity_reaches_the_metrics_endpoint(self):
        """The end-to-end guarantee, asserted against the real exposition."""
        client.get("/api/v1/data/abc",
                   headers={"User-Agent": "swarm-provenance-mcp/0.1.0",
                            "X-Forwarded-For": "203.0.113.50"})
        body = TestClient(app).get("/metrics").text
        assert "203.0.113.50" not in body
        for line in body.splitlines():
            if line.startswith(("gateway_requests_by_client_total", "gateway_distinct_callers")):
                assert "203.0" not in line, line
