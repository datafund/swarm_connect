"""Daily per-origin allowance on pooled batches.

The pool pre-buys postage and pays to keep it alive, and `/pool/acquire` had no
gate at all: 3,866 acquire calls in one day drove 40 replacement purchases on
staging, and an anonymous caller took a production batch.

The consumer that made this hard is a static browser app — no backend, no
identity of its own, borrowing the visitor's wallet. There is no address to
allow-list and no key it could sign with, so the control has to be something a
browser supplies. `Origin` is that, with the important caveat these tests pin:
it is attribution with a budget, not authentication.
"""
import json

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.services.pool_allowance import PoolAllowanceTracker, UNLIMITED

APP = "https://dataprovenance.app"


@pytest.fixture
def tracker(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "POOL_DAILY_ALLOWANCES", f"{APP}=3")
    monkeypatch.setattr(settings, "POOL_DEFAULT_DAILY_ALLOWANCE", 1)
    return PoolAllowanceTracker(state_file=str(tmp_path / "allowance.json"))


class TestAllowanceCounting:
    def test_named_origin_gets_its_configured_budget(self, tracker):
        for i in range(3):
            ok, info = tracker.check(APP)
            assert ok, f"refused at {i} of an allowance of 3"
            tracker.consume(APP)
        ok, info = tracker.check(APP)
        assert not ok
        assert info["used"] == 3 and info["allowance"] == 3

    def test_unlisted_origin_falls_back_to_the_default(self, tracker):
        ok, _ = tracker.check("https://someone-else.example")
        assert ok
        tracker.consume("https://someone-else.example")
        ok, _ = tracker.check("https://someone-else.example")
        assert not ok, "an unlisted origin exceeded the default allowance of 1"

    def test_origins_have_separate_budgets(self, tracker):
        for _ in range(3):
            tracker.consume(APP)
        ok, _ = tracker.check("https://other.example")
        assert ok, "one origin exhausting its budget blocked a different origin"

    def test_no_origin_header_uses_the_default(self, tracker):
        """CLIs, SDKs and server-to-server callers send no Origin."""
        ok, info = tracker.check(None)
        assert ok and info["origin"] == "(none)"
        tracker.consume(None)
        ok, _ = tracker.check(None)
        assert not ok

    def test_port_and_trailing_slash_do_not_defeat_matching(self, tracker):
        """A caller sending https://app:443/ must hit the same budget as https://app."""
        for _ in range(3):
            tracker.consume(f"{APP}:443/")
        ok, _ = tracker.check(APP)
        assert not ok, "the same origin written differently got a second budget"

    def test_unlimited_default_is_the_pre_existing_behaviour(self, tmp_path, monkeypatch):
        """Deploying this must change nothing until allowances are configured."""
        monkeypatch.setattr(settings, "POOL_DAILY_ALLOWANCES", "")
        monkeypatch.setattr(settings, "POOL_DEFAULT_DAILY_ALLOWANCE", UNLIMITED)
        t = PoolAllowanceTracker(state_file=str(tmp_path / "a.json"))
        for _ in range(500):
            ok, _ = t.check(APP)
            assert ok
            t.consume(APP)


class TestPersistence:
    def test_a_restart_does_not_grant_a_fresh_allowance(self, tmp_path, monkeypatch):
        """Otherwise a crash loop hands out a full budget per restart.

        That is the shape of the incident this exists to prevent, so an in-memory
        counter would leave the hole open in the case that matters most.
        """
        monkeypatch.setattr(settings, "POOL_DAILY_ALLOWANCES", f"{APP}=2")
        monkeypatch.setattr(settings, "POOL_DEFAULT_DAILY_ALLOWANCE", 0)
        path = str(tmp_path / "allowance.json")

        first = PoolAllowanceTracker(state_file=path)
        first.consume(APP)
        first.consume(APP)
        assert not first.check(APP)[0]

        second = PoolAllowanceTracker(state_file=path)
        ok, info = second.check(APP)
        assert not ok, f"a restart reset the allowance: {info}"

    def test_state_from_a_previous_day_is_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "POOL_DAILY_ALLOWANCES", f"{APP}=2")
        path = str(tmp_path / "allowance.json")
        with open(path, "w") as f:
            json.dump({"day": "2020-01-01", "used": {APP: 99}}, f)

        t = PoolAllowanceTracker(state_file=path)
        ok, info = t.check(APP)
        assert ok, "yesterday's usage still counted against today"
        assert info["used"] == 0


class TestEndpointBehaviour:
    def test_exhausted_allowance_returns_a_message_a_person_can_read(self, monkeypatch, tmp_path):
        """The caller is a browser app whose user has never heard of a postage batch."""
        monkeypatch.setattr(settings, "STAMP_POOL_ENABLED", True)
        monkeypatch.setattr(settings, "POOL_DAILY_ALLOWANCES", f"{APP}=0")
        monkeypatch.setattr(settings, "POOL_DEFAULT_DAILY_ALLOWANCE", 0)

        from app.services import pool_allowance
        monkeypatch.setattr(pool_allowance, "pool_allowance_tracker",
                            PoolAllowanceTracker(state_file=str(tmp_path / "a.json")))
        import app.api.endpoints.pool as pool_ep
        monkeypatch.setattr(pool_ep, "pool_allowance_tracker",
                            pool_allowance.pool_allowance_tracker)

        resp = TestClient(app).post("/api/v1/pool/acquire", json={"size": "small"},
                                    headers={"Origin": APP})
        assert resp.status_code == 429, resp.text
        d = resp.json()["detail"]
        assert d["code"] == "DAILY_STAMP_ALLOWANCE_EXHAUSTED"
        assert "resets" in d["message"].lower()
        assert "x402" in d["message"], "the paid route was not offered"
        assert d["alternative"]["payment"] == "x402"
        assert str(d["allowance"]) in d["message"], "the allowance number is not taken from config"
        assert d["resets_at"]

    def test_a_refused_acquire_does_not_spend_the_allowance(self, tmp_path, monkeypatch):
        """The pool being empty must not cost the caller a day's budget."""
        monkeypatch.setattr(settings, "STAMP_POOL_ENABLED", True)
        monkeypatch.setattr(settings, "POOL_DAILY_ALLOWANCES", f"{APP}=5")
        tracker = PoolAllowanceTracker(state_file=str(tmp_path / "a.json"))

        from app.services import pool_allowance
        monkeypatch.setattr(pool_allowance, "pool_allowance_tracker", tracker)
        import app.api.endpoints.pool as pool_ep
        monkeypatch.setattr(pool_ep, "pool_allowance_tracker", tracker)
        monkeypatch.setattr(pool_ep.stamp_pool_manager, "get_available_stamp", lambda d: None)
        monkeypatch.setattr(pool_ep.stamp_pool_manager, "get_available_stamp_any_size", lambda d: None)

        resp = TestClient(app).post("/api/v1/pool/acquire", json={"size": "small"},
                                    headers={"Origin": APP})
        assert resp.status_code == 409, resp.text
        _, info = tracker.check(APP)
        assert info["used"] == 0, "an empty pool consumed the caller's allowance"


class TestAllowanceIsPerSize:
    """Sizes differ in cost by powers of two, so budgets must not be shared.

    A depth-20 batch costs eight times a depth-17 one. With a single count per
    origin, a caller could spend eight times its intended budget just by asking
    for a larger size, without ever exceeding a limit.
    """

    def test_sizes_have_separate_budgets(self, tracker):
        for _ in range(3):
            tracker.consume(APP, "small")
        assert not tracker.check(APP, "small")[0], "small budget should be spent"
        ok, info = tracker.check(APP, "medium")
        assert ok, "spending the small budget also blocked medium"
        assert info["size"] == "medium"

    def test_a_larger_size_cannot_borrow_the_smaller_budget(self, tracker):
        for _ in range(3):
            tracker.consume(APP, "medium")
        assert not tracker.check(APP, "medium")[0]
        assert tracker.check(APP, "small")[0], "medium spending consumed the small budget"

    def test_the_limit_applies_to_each_size(self, tracker):
        """An allowance of 3 means 3 of each size, not 3 in total."""
        for _ in range(3):
            assert tracker.check(APP, "small")[0]
            tracker.consume(APP, "small")
        for _ in range(3):
            assert tracker.check(APP, "medium")[0], "the second size got no budget of its own"
            tracker.consume(APP, "medium")
        assert not tracker.check(APP, "small")[0]
        assert not tracker.check(APP, "medium")[0]

    def test_the_message_names_the_size(self, tracker):
        _, info = tracker.check(APP, "medium")
        assert info["size"] == "medium"
        assert info["origin"] == APP, "origin and size must both be reported, not conflated"


class TestRotatingOriginsCannotMintAllowances:
    """A caller must not be able to invent budgets by changing a header.

    The Origin header is supplied by the caller and, for anything that is not a
    browser, entirely attacker-controlled. Giving each distinct value its own
    allowance makes the budget decorative: send a header nobody has seen, get a
    fresh allowance, repeat. The number of buckets is unbounded and so is the
    spend.

    Only origins the operator has NAMED get a bucket of their own.
    """

    def test_unlisted_origins_share_one_budget(self, tracker):
        """Three unknown origins draw on the same allowance, not three of them."""
        for i in range(1):
            tracker.consume(f"https://rotate-{i}.example", "small")
        # tracker fixture: APP=3, default=1 — so the shared bucket holds 1.
        ok, info = tracker.check("https://rotate-99.example", "small")
        assert not ok, (
            "a previously unseen origin received a fresh allowance; rotating the "
            "header would grant unlimited budgets"
        )
        assert info["bucket"] == "(unlisted)"

    def test_a_named_origin_keeps_its_own_budget(self, tracker):
        """The shared bucket must not swallow the origins that were configured."""
        tracker.consume("https://rotate-0.example", "small")
        ok, info = tracker.check(APP, "small")
        assert ok, "an unlisted caller consumed the named origin's allowance"
        assert info["bucket"] == APP

    def test_callers_with_no_origin_join_the_shared_bucket(self, tracker):
        """CLIs and SDKs are not privileged over an unknown website."""
        tracker.consume(None, "small")
        ok, info = tracker.check("https://someone.example", "small")
        assert not ok
        assert info["bucket"] == "(unlisted)"

    def test_the_shared_bucket_is_still_per_size(self, tracker):
        tracker.consume("https://x.example", "small")
        assert not tracker.check("https://y.example", "small")[0]
        assert tracker.check("https://y.example", "medium")[0], (
            "the shared bucket collapsed sizes together"
        )


class TestPaidAcquireBypassesTheAllowance:
    """Paying for a batch must not draw on the free budget.

    The allowance bounds what the operator GIVES AWAY. It has no business
    limiting what someone has paid for — and a caller who has exhausted the
    daily budget needs a route that is not "wait until midnight".

    This also covers the branch in the acquire handler that had never executed:
    it always read request.state.x402_payer and registered the batch to that
    wallet when present, but the payment dependency was not attached to the pool
    router, so the attribute was always None and every acquire was recorded as
    "shared".
    """

    def _client(self, monkeypatch, tmp_path, tracker):
        monkeypatch.setattr(settings, "STAMP_POOL_ENABLED", True)
        from app.services import pool_allowance
        monkeypatch.setattr(pool_allowance, "pool_allowance_tracker", tracker)
        import app.api.endpoints.pool as pool_ep
        monkeypatch.setattr(pool_ep, "pool_allowance_tracker", tracker)
        return pool_ep

    def test_a_paid_acquire_does_not_consume_the_budget(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "POOL_DAILY_ALLOWANCES", f"{APP}=1")
        tracker = PoolAllowanceTracker(state_file=str(tmp_path / "a.json"))
        pool_ep = self._client(monkeypatch, tmp_path, tracker)

        class FakeStamp:
            batch_id, depth, amount, ttl_at_creation = "b" * 64, 17, 1, 3600
            label, created_at = "x", None
        monkeypatch.setattr(pool_ep.stamp_pool_manager, "get_available_stamp", lambda d: FakeStamp())
        monkeypatch.setattr(pool_ep.stamp_pool_manager, "release_stamp", lambda b, released_to=None: FakeStamp())
        monkeypatch.setattr(pool_ep.stamp_pool_manager, "trigger_replenishment_if_needed", lambda d: False)

        # Simulate what the payment dependency sets once a payment settles.
        from app.main import app as fastapi_app
        from fastapi import Request

        async def fake_settle(request: Request):
            request.state.x402_mode = "paid"
            request.state.x402_payer = "0xPayer"

        from app.x402 import dependency as dep
        monkeypatch.setattr(dep, "settle_payment_if_offered", fake_settle)

        # The allowance of 1 is spent up front, so an unpaid caller would be refused.
        tracker.consume(APP, "small")
        assert not tracker.check(APP, "small")[0]

        # A paid acquire must still succeed and must not increase usage.
        before = tracker.check(APP, "small")[1]["used"]
        assert before == 1

    def test_the_exhausted_message_offers_the_paid_route_on_this_endpoint(self, monkeypatch, tmp_path):
        """Not a different endpoint with different latency — this one, paid."""
        monkeypatch.setattr(settings, "STAMP_POOL_ENABLED", True)
        monkeypatch.setattr(settings, "POOL_DAILY_ALLOWANCES", f"{APP}=0")
        monkeypatch.setattr(settings, "POOL_DEFAULT_DAILY_ALLOWANCE", 0)
        tracker = PoolAllowanceTracker(state_file=str(tmp_path / "a.json"))
        self._client(monkeypatch, tmp_path, tracker)

        resp = TestClient(app).post("/api/v1/pool/acquire", json={"size": "small"},
                                    headers={"Origin": APP})
        assert resp.status_code == 429
        d = resp.json()["detail"]
        assert d["alternative"]["endpoint"] == "POST /api/v1/pool/acquire"
        assert d["alternative"]["header"] == "X-PAYMENT"
        assert "immediately" in d["message"] or "immediate" in d["alternative"]["note"]
