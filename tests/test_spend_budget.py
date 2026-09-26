"""Bounds on what a caller can spend of the gateway's BZZ.

`POST /api/v1/stamps/` and `PATCH /api/v1/stamps/{id}/extend` both spend the
operator's money for whoever asks. Neither had a spending bound: measured on the
staging gateway, an anonymous free-tier request reached the point of the gateway
costing a 243,074 BZZ batch and was refused only because the wallet could not
cover it. The wallet balance was the limit.

Extend is the softer of the two — `is_protected_endpoint` matches on method, and
only POST routes are listed, so a PATCH is not payment-gated at all and does not
even meet the free-tier rate limit. It also tops up any batch on the node,
including ones the caller does not own.

Two limits, answering different questions: a per-request ceiling so no single
call takes a large share of the wallet, and a per-caller daily budget so the
first cannot simply be applied repeatedly.
"""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.services.spend_budget import SpendBudgetTracker, UNLIMITED
from prometheus_client import REGISTRY

STAMP_ID = "a" * 64


def _bee_refusal():
    import httpx
    return httpx.HTTPStatusError(
        "400", request=httpx.Request("POST", "http://bee/stamps"),
        response=httpx.Response(400, json={"message": "insufficient amount"}))

# depth 17 at this price is a fraction of a BZZ; the tests set costs explicitly
# via the chainstate price where the exact figure matters.
CHAINSTATE = {"currentPrice": "24000", "block": 1, "chainTip": 1, "totalAmount": "1"}
FUNDS_OK = {"sufficient": True, "wallet_balance_bzz": 100.0,
            "required_bzz": 0.01, "shortfall_bzz": 0.0}


@pytest.fixture
def tracker(tmp_path, monkeypatch):
    t = SpendBudgetTracker(state_file=str(tmp_path / "spend.json"))
    from app.services import spend_budget
    monkeypatch.setattr(spend_budget, "spend_budget_tracker", t)
    import app.api.endpoints.stamps as stamps_ep
    monkeypatch.setattr(stamps_ep, "spend_budget_tracker", t)
    return t


class TestBudgetArithmetic:
    def test_a_request_that_would_overrun_is_refused_before_it_starts(self, tracker, monkeypatch):
        """Not "is any budget left" but "does THIS request fit".

        A caller with 0.01 BZZ remaining must not be allowed to begin a 5 BZZ
        purchase; checking only for a non-zero remainder would let them.
        """
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", 1.0)
        tracker.consume("1.2.3.4", 0.99)
        assert not tracker.check("1.2.3.4", 0.5)[0]
        assert tracker.check("1.2.3.4", 0.005)[0]

    def test_spending_exactly_the_budget_is_allowed(self, tracker, monkeypatch):
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", 1.0)
        assert tracker.check("1.2.3.4", 1.0)[0]
        tracker.consume("1.2.3.4", 1.0)
        assert not tracker.check("1.2.3.4", 0.000001)[0]

    def test_callers_have_separate_budgets(self, tracker, monkeypatch):
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", 1.0)
        tracker.consume("1.2.3.4", 1.0)
        assert not tracker.check("1.2.3.4", 0.1)[0]
        assert tracker.check("5.6.7.8", 0.1)[0], "one caller must not spend another's budget"

    def test_unlimited_is_the_pre_existing_behaviour(self, tracker, monkeypatch):
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", UNLIMITED)
        tracker.consume("1.2.3.4", 10_000.0)
        assert tracker.check("1.2.3.4", 10_000.0)[0]

    def test_a_restart_does_not_grant_a_fresh_budget(self, tmp_path, monkeypatch):
        """A crash loop must not hand out a full budget per restart — that is the
        shape of the incident this exists to prevent."""
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", 1.0)
        path = str(tmp_path / "spend.json")
        first = SpendBudgetTracker(state_file=path)
        first.consume("1.2.3.4", 0.9)

        second = SpendBudgetTracker(state_file=path)
        assert not second.check("1.2.3.4", 0.5)[0]
        assert second.check("1.2.3.4", 0.05)[0]

    def test_state_from_a_previous_day_is_ignored(self, tmp_path, monkeypatch):
        import json
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", 1.0)
        path = tmp_path / "spend.json"
        path.write_text(json.dumps({"day": "1999-01-01", "spent": {"1.2.3.4": 999.0}}))
        assert SpendBudgetTracker(state_file=str(path)).check("1.2.3.4", 0.9)[0]

    def test_an_unreadable_state_file_does_not_break_startup(self, tmp_path, monkeypatch):
        """Never fail to start over a counter."""
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", 1.0)
        path = tmp_path / "spend.json"
        path.write_text("{not json")
        assert SpendBudgetTracker(state_file=str(path)).check("1.2.3.4", 0.5)[0]


class TestPerRequestCeiling:
    """X402_MAX_STAMP_BZZ was in configuration from the start and referenced
    nowhere — a cap that reads as protection during review and enforces
    nothing."""

    def _purchase(self, depth=20, duration=8760):
        with patch("app.services.swarm_api.get_chainstate",
                   new=AsyncMock(return_value=CHAINSTATE)), \
             patch("app.services.swarm_api.check_sufficient_funds",
                   new=AsyncMock(return_value=FUNDS_OK)), \
             patch("app.services.swarm_api.purchase_postage_stamp",
                   new=AsyncMock(return_value="b" * 64)):
            return TestClient(app).post(
                "/api/v1/stamps/",
                json={"depth": depth, "duration_hours": duration},
            )

    def test_an_expensive_request_is_refused_on_policy(self, tracker, monkeypatch):
        monkeypatch.setattr(settings, "X402_MAX_STAMP_BZZ", 0.001)
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", UNLIMITED)
        r = self._purchase()
        assert r.status_code == 400
        d = r.json()["detail"]
        assert d["code"] == "STAMP_COST_EXCEEDS_LIMIT"
        assert d["limit_bzz"] == 0.001
        assert d["cost_bzz"] > d["limit_bzz"]

    def test_the_refusal_does_not_depend_on_the_wallet_balance(self, tracker, monkeypatch):
        """The old answer was "insufficient funds", which told the caller the
        wallet was the only limit. It was, and that is the defect."""
        monkeypatch.setattr(settings, "X402_MAX_STAMP_BZZ", 0.001)
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", UNLIMITED)
        rich = {"sufficient": True, "wallet_balance_bzz": 10 ** 9,
                "required_bzz": 0.01, "shortfall_bzz": 0.0}
        with patch("app.services.swarm_api.get_chainstate",
                   new=AsyncMock(return_value=CHAINSTATE)), \
             patch("app.services.swarm_api.check_sufficient_funds",
                   new=AsyncMock(return_value=rich)), \
             patch("app.services.swarm_api.purchase_postage_stamp",
                   new=AsyncMock(return_value="b" * 64)):
            r = TestClient(app).post("/api/v1/stamps/",
                                     json={"depth": 20, "duration_hours": 8760})
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "STAMP_COST_EXCEEDS_LIMIT"

    def test_zero_disables_the_ceiling(self, tracker, monkeypatch):
        monkeypatch.setattr(settings, "X402_MAX_STAMP_BZZ", 0.0)
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", UNLIMITED)
        assert self._purchase().status_code == 201

    def test_a_cheap_request_is_unaffected(self, tracker, monkeypatch):
        monkeypatch.setattr(settings, "X402_MAX_STAMP_BZZ", 5.0)
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", UNLIMITED)
        assert self._purchase(depth=17, duration=24).status_code == 201


class TestPurchaseEndpoint:
    def _purchase(self, depth=17, duration=24):
        with patch("app.services.swarm_api.get_chainstate",
                   new=AsyncMock(return_value=CHAINSTATE)), \
             patch("app.services.swarm_api.check_sufficient_funds",
                   new=AsyncMock(return_value=FUNDS_OK)), \
             patch("app.services.swarm_api.purchase_postage_stamp",
                   new=AsyncMock(return_value="b" * 64)):
            return TestClient(app).post(
                "/api/v1/stamps/",
                json={"depth": depth, "duration_hours": duration},
            )

    def test_the_budget_stops_repeated_purchases(self, tracker, monkeypatch):
        monkeypatch.setattr(settings, "X402_MAX_STAMP_BZZ", 0.0)
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", 0.05)

        allowed = 0
        for _ in range(40):
            if self._purchase().status_code != 201:
                break
            allowed += 1
        else:
            pytest.fail("the budget never refused a purchase")

        assert allowed > 0, "the budget refused the very first purchase"
        assert tracker.snapshot()["spent"]

    def test_the_refusal_says_what_a_caller_can_do(self, tracker, monkeypatch):
        monkeypatch.setattr(settings, "X402_MAX_STAMP_BZZ", 0.0)
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", 0.001)
        tracker.consume("testclient", 0.001)

        r = self._purchase()
        assert r.status_code == 429
        d = r.json()["detail"]
        assert d["code"] == "DAILY_SPEND_BUDGET_EXHAUSTED"
        assert d["resets_at"]
        assert d["remaining_bzz"] == 0
        assert "resets" in d["message"]
        assert str(d["daily_budget_bzz"]) in d["message"], "the number is not taken from config"

    def test_a_failed_purchase_does_not_spend_the_budget(self, tracker, monkeypatch):
        """Bee refusing must not cost the caller their allowance."""
        monkeypatch.setattr(settings, "X402_MAX_STAMP_BZZ", 0.0)
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", 10.0)
        with patch("app.services.swarm_api.get_chainstate",
                   new=AsyncMock(return_value=CHAINSTATE)), \
             patch("app.services.swarm_api.check_sufficient_funds",
                   new=AsyncMock(return_value=FUNDS_OK)), \
             patch("app.services.swarm_api.purchase_postage_stamp",
                   new=AsyncMock(side_effect=_bee_refusal())):
            TestClient(app).post("/api/v1/stamps/",
                                 json={"depth": 17, "duration_hours": 24})
        assert tracker.snapshot()["spent"] == {}, "a refused purchase charged the caller"

    def test_an_uncertain_failure_stays_charged(self, tracker, monkeypatch):
        """A timeout after the request was sent may have bought the batch:
        the limits fail closed rather than hand the budget back."""
        import httpx
        monkeypatch.setattr(settings, "X402_MAX_STAMP_BZZ", 0.0)
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", 10.0)
        with patch("app.services.swarm_api.get_chainstate",
                   new=AsyncMock(return_value=CHAINSTATE)), \
             patch("app.services.swarm_api.check_sufficient_funds",
                   new=AsyncMock(return_value=FUNDS_OK)), \
             patch("app.services.swarm_api.purchase_postage_stamp",
                   new=AsyncMock(side_effect=httpx.ReadTimeout("mining"))):
            TestClient(app).post("/api/v1/stamps/", json={"depth": 17, "duration_hours": 24})
        snap = tracker.snapshot()
        assert snap["spent"] != {}
        assert snap["gateway_spent"] > 0 and snap["giveaway_spent"] > 0


class TestExtendEndpoint:
    """Extend is not in PROTECTED_ENDPOINTS — is_protected_endpoint matches on
    method and only POST paths are listed — so it has no payment gate and no
    free-tier rate limit. The budget is the only thing bounding it."""

    def _extend(self, duration=24):
        existing = [{"batchID": STAMP_ID, "depth": 17, "batchTTL": 86400}]
        with patch("app.services.swarm_api.get_all_stamps_processed",
                   new=AsyncMock(return_value=existing)), \
             patch("app.services.swarm_api.get_chainstate",
                   new=AsyncMock(return_value=CHAINSTATE)), \
             patch("app.services.swarm_api.check_sufficient_funds",
                   new=AsyncMock(return_value=FUNDS_OK)), \
             patch("app.services.swarm_api.extend_postage_stamp",
                   new=AsyncMock(return_value=STAMP_ID)):
            return TestClient(app).patch(
                f"/api/v1/stamps/{STAMP_ID}/extend",
                json={"duration_hours": duration},
            )

    def test_extend_is_bounded_too(self, tracker, monkeypatch):
        """Bounding purchase alone would leave the cheaper path open."""
        monkeypatch.setattr(settings, "X402_MAX_STAMP_BZZ", 0.0)
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", 0.001)
        tracker.consume("testclient", 0.001)

        r = self._extend()
        assert r.status_code == 429
        assert r.json()["detail"]["code"] == "DAILY_SPEND_BUDGET_EXHAUSTED"

    def test_extend_draws_on_the_same_budget_as_purchase(self, tracker, monkeypatch):
        """Separate budgets would double what a caller can spend."""
        monkeypatch.setattr(settings, "X402_MAX_STAMP_BZZ", 0.0)
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", UNLIMITED)
        assert self._extend().status_code == 200
        spent = tracker.snapshot()["spent"]
        assert list(spent) == ["testclient"], spent
        assert spent["testclient"] > 0


class TestPaidCallersBypass:
    def test_a_settled_payment_is_not_drawn_from_the_giveaway_budget(self, tracker, monkeypatch):
        monkeypatch.setattr(settings, "X402_MAX_STAMP_BZZ", 0.0)
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", 0.001)
        monkeypatch.setattr(settings, "X402_NETWORK", "base")
        tracker.consume("testclient", 0.001)

        # Drive the real helper with the payment state the dependency would set.
        import app.api.endpoints.stamps as stamps_ep
        from types import SimpleNamespace

        class _Req:
            def __init__(self):
                self.state = SimpleNamespace(x402_mode="paid")
                self.headers = {}
                self.client = None

        # caller None means "charge nobody's budget" — the caller funded it.
        # The gateway-wide ceiling still counts it (#363).
        reservation = stamps_ep._enforce_spend_limits(_Req(), 5.0, "stamp purchase")
        assert reservation.caller is None
        assert tracker.snapshot()["spent"] == {"testclient": 0.001}, "the payer was charged"
        assert tracker.snapshot()["gateway_spent"] == 5.0

    def test_a_testnet_payment_does_not_bypass(self, tracker, monkeypatch):
        """Testnet currency is free from a faucet, so a payment settled there is
        not evidence anyone paid — the same reasoning as the pool bypass."""
        monkeypatch.setattr(settings, "X402_MAX_STAMP_BZZ", 0.0)
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", 0.001)
        monkeypatch.setattr(settings, "X402_NETWORK", "base-sepolia")
        monkeypatch.setattr(settings, "X402_ALLOW_TESTNET_PAID_BYPASS", False)
        tracker.consume("1.2.3.4", 0.001)

        import app.api.endpoints.stamps as stamps_ep
        from fastapi import HTTPException
        from types import SimpleNamespace

        class _Req:
            def __init__(self):
                self.state = SimpleNamespace(x402_mode="paid")
                self.headers = {"X-Forwarded-For": "1.2.3.4"}
                self.client = None

        with pytest.raises(HTTPException) as e:
            stamps_ep._enforce_spend_limits(_Req(), 0.5, "stamp purchase")
        assert e.value.status_code == 429

    def test_the_per_request_ceiling_applies_to_paid_callers_too(self, tracker, monkeypatch):
        """The gateway fronts the BZZ either way, so one transaction's exposure
        is bounded regardless of who is paying."""
        monkeypatch.setattr(settings, "X402_MAX_STAMP_BZZ", 1.0)
        monkeypatch.setattr(settings, "X402_NETWORK", "base")

        import app.api.endpoints.stamps as stamps_ep
        from fastapi import HTTPException
        from types import SimpleNamespace

        class _Req:
            def __init__(self):
                self.state = SimpleNamespace(x402_mode="paid")
                self.headers = {}
                self.client = None

        with pytest.raises(HTTPException) as e:
            stamps_ep._enforce_spend_limits(_Req(), 5.0, "stamp purchase")
        assert e.value.status_code == 400
        assert e.value.detail["code"] == "STAMP_COST_EXCEEDS_LIMIT"


def _counter(name, **labels):
    """Current value of a labelled counter, or 0 before it is first touched."""
    v = REGISTRY.get_sample_value(name, labels)
    return 0.0 if v is None else v


class TestMetrics:
    """The budget is a number chosen without usage data.

    Refusals are how we tell "correctly bounding abuse" from "turning away a
    legitimate integration", so they have to be visible somewhere other than the
    logs. These pin that the counters actually move.
    """

    def _purchase(self):
        with patch("app.services.swarm_api.get_chainstate",
                   new=AsyncMock(return_value=CHAINSTATE)), \
             patch("app.services.swarm_api.check_sufficient_funds",
                   new=AsyncMock(return_value=FUNDS_OK)), \
             patch("app.services.swarm_api.purchase_postage_stamp",
                   new=AsyncMock(return_value="b" * 64)):
            return TestClient(app).post("/api/v1/stamps/",
                                        json={"depth": 17, "duration_hours": 24})

    def test_a_budget_refusal_is_counted(self, tracker, monkeypatch):
        monkeypatch.setattr(settings, "X402_MAX_STAMP_BZZ", 0.0)
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", 0.001)
        tracker.consume("testclient", 0.001)

        before = _counter("gateway_stamp_spend_refusals_total",
                          operation="stamp purchase", limit="daily_budget")
        assert self._purchase().status_code == 429
        after = _counter("gateway_stamp_spend_refusals_total",
                         operation="stamp purchase", limit="daily_budget")
        assert after == before + 1

    def test_a_ceiling_refusal_is_counted_separately(self, tracker, monkeypatch):
        """The two limits mean different things — one number for both would not
        say whether the cap is too low or the budget is."""
        monkeypatch.setattr(settings, "X402_MAX_STAMP_BZZ", 0.0000001)
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", UNLIMITED)

        before = _counter("gateway_stamp_spend_refusals_total",
                          operation="stamp purchase", limit="per_request")
        assert self._purchase().status_code == 400
        after = _counter("gateway_stamp_spend_refusals_total",
                         operation="stamp purchase", limit="per_request")
        assert after == before + 1

    def test_committed_bzz_is_counted(self, tracker, monkeypatch):
        monkeypatch.setattr(settings, "X402_MAX_STAMP_BZZ", 0.0)
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", UNLIMITED)

        before = _counter("gateway_stamp_spend_bzz_total",
                          operation="stamp purchase", charged="budget")
        assert self._purchase().status_code == 201
        after = _counter("gateway_stamp_spend_bzz_total",
                         operation="stamp purchase", charged="budget")
        assert after > before, "the BZZ committed was not recorded"

    def test_a_refused_purchase_records_no_spend(self, tracker, monkeypatch):
        """A refusal must not look like money going out the door."""
        monkeypatch.setattr(settings, "X402_MAX_STAMP_BZZ", 0.0000001)
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", UNLIMITED)

        before = _counter("gateway_stamp_spend_bzz_total",
                          operation="stamp purchase", charged="budget")
        assert self._purchase().status_code == 400
        after = _counter("gateway_stamp_spend_bzz_total",
                         operation="stamp purchase", charged="budget")
        assert after == before

    def test_no_caller_identity_leaks_into_a_label(self, tracker, monkeypatch):
        """An IP is high-cardinality and is personal data going to a
        third-party metrics store. The logs name the caller; the metrics
        must not."""
        monkeypatch.setattr(settings, "X402_MAX_STAMP_BZZ", 0.0)
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", 0.001)
        tracker.consume("testclient", 0.001)
        self._purchase()

        body = TestClient(app).get("/metrics").text
        for line in body.splitlines():
            if line.startswith("gateway_stamp_spend"):
                assert "testclient" not in line, line


class TestDayRollover:
    """"Daily" is the whole contract.

    If the reset failed, a caller would be locked out permanently after their
    first day rather than for the rest of it — a limit that never releases is a
    different product from one that resets, and nothing else in the suite
    exercised a live rollover: the other test loads yesterday's file at startup,
    which is a different code path.
    """

    def test_a_running_tracker_resets_when_the_day_turns(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", 1.0)
        t = SpendBudgetTracker(state_file=str(tmp_path / "spend.json"))
        t.consume("1.2.3.4", 1.0)
        assert not t.check("1.2.3.4", 0.5)[0]

        from app.services import spend_budget
        monkeypatch.setattr(spend_budget, "_today", lambda: "2099-01-01")

        allowed, info = t.check("1.2.3.4", 1.0)
        assert allowed, "the budget did not reset when the day turned"
        assert info["spent_bzz"] == 0
        assert info["resets_at"].startswith("2099-01-01")

    def test_the_reset_is_persisted_not_just_in_memory(self, tmp_path, monkeypatch):
        """Otherwise a restart just after midnight would reload yesterday's
        spend and re-apply it to the new day."""
        import json
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", 1.0)
        path = tmp_path / "spend.json"
        t = SpendBudgetTracker(state_file=str(path))
        t.consume("1.2.3.4", 1.0)

        from app.services import spend_budget
        monkeypatch.setattr(spend_budget, "_today", lambda: "2099-01-01")
        t.check("1.2.3.4", 0.1)

        on_disk = json.loads(path.read_text())
        assert on_disk["day"] == "2099-01-01"
        assert on_disk["spent"] == {}

    def test_snapshot_also_rolls_the_day(self, tmp_path, monkeypatch):
        """The metrics gauges read through snapshot(); a stale day there would
        report yesterday's total as today's."""
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", 1.0)
        t = SpendBudgetTracker(state_file=str(tmp_path / "spend.json"))
        t.consume("1.2.3.4", 0.5)

        from app.services import spend_budget
        monkeypatch.setattr(spend_budget, "_today", lambda: "2099-01-01")
        assert t.snapshot() == {"day": "2099-01-01", "spent": {}, "gateway_spent": 0.0, "giveaway_spent": 0.0}


class TestDurability:
    def test_a_request_survives_an_unwritable_state_file(self, tmp_path, monkeypatch):
        """Losing the counter is recoverable; failing the request is not. A full
        or read-only disk must not stop the gateway selling stamps."""
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", 1.0)
        t = SpendBudgetTracker(state_file=str(tmp_path / "spend.json"))

        def boom(*a, **k):
            raise OSError("read-only file system")

        monkeypatch.setattr("builtins.open", boom)
        t.consume("1.2.3.4", 0.1)  # must not raise
        monkeypatch.undo()

        # The in-memory count still moved, so the budget holds for this process.
        assert t.check("1.2.3.4", 0.95)[0] is False

    def test_concurrent_requests_cannot_overspend(self, tmp_path, monkeypatch):
        """This is a money path and the tracker is shared across threads.

        Without the lock, interleaved read-modify-write on the same caller loses
        updates, and the recorded spend comes out lower than what was actually
        committed — which is the direction that costs money.
        """
        import threading
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", -1.0)
        t = SpendBudgetTracker(state_file=str(tmp_path / "spend.json"))

        def spend():
            for _ in range(50):
                t.consume("1.2.3.4", 0.01)

        threads = [threading.Thread(target=spend) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        assert t.snapshot()["spent"]["1.2.3.4"] == pytest.approx(8 * 50 * 0.01)


class TestBothLimitsApplyToBothEndpoints:
    """The two endpoints were bounded in one change, so it is easy for a later
    edit to fix or break one and not the other. These pin the symmetry."""

    def _extend(self):
        existing = [{"batchID": STAMP_ID, "depth": 17, "batchTTL": 86400}]
        with patch("app.services.swarm_api.get_all_stamps_processed",
                   new=AsyncMock(return_value=existing)), \
             patch("app.services.swarm_api.get_chainstate",
                   new=AsyncMock(return_value=CHAINSTATE)), \
             patch("app.services.swarm_api.check_sufficient_funds",
                   new=AsyncMock(return_value=FUNDS_OK)), \
             patch("app.services.swarm_api.extend_postage_stamp",
                   new=AsyncMock(return_value=STAMP_ID)):
            return TestClient(app).patch(f"/api/v1/stamps/{STAMP_ID}/extend",
                                         json={"duration_hours": 8760})

    def test_the_per_request_ceiling_applies_to_extend(self, tracker, monkeypatch):
        """Only the daily budget was covered on this endpoint before."""
        monkeypatch.setattr(settings, "X402_MAX_STAMP_BZZ", 0.0000001)
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", UNLIMITED)
        r = self._extend()
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "STAMP_COST_EXCEEDS_LIMIT"

    def test_the_refusal_names_the_operation(self, tracker, monkeypatch):
        """A caller seeing "stamp purchase" on an extend has been told the wrong
        thing about what they just did."""
        monkeypatch.setattr(settings, "X402_MAX_STAMP_BZZ", 0.0000001)
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", UNLIMITED)
        assert "extension" in self._extend().json()["detail"]["message"]

    def test_a_refused_extend_is_counted_under_its_own_operation(self, tracker, monkeypatch):
        monkeypatch.setattr(settings, "X402_MAX_STAMP_BZZ", 0.0000001)
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", UNLIMITED)
        before = _counter("gateway_stamp_spend_refusals_total",
                          operation="stamp extension", limit="per_request")
        self._extend()
        after = _counter("gateway_stamp_spend_refusals_total",
                         operation="stamp extension", limit="per_request")
        assert after == before + 1


class TestOnlyASettledPaymentBypasses:
    """`paid` is one of several x402 modes. Treating anything non-None as paid
    would hand the bypass to every free-tier caller, which is the population the
    budget exists for."""

    def _helper_with_mode(self, mode):
        import app.api.endpoints.stamps as stamps_ep
        from types import SimpleNamespace

        class _Req:
            def __init__(self):
                self.state = SimpleNamespace(x402_mode=mode)
                self.headers = {}
                self.client = None

        return stamps_ep, _Req()

    @pytest.mark.parametrize("mode", ["free", "free-tier", "rejected", None])
    def test_a_non_paid_mode_is_still_charged(self, tracker, monkeypatch, mode):
        monkeypatch.setattr(settings, "X402_MAX_STAMP_BZZ", 0.0)
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", UNLIMITED)
        monkeypatch.setattr(settings, "X402_NETWORK", "base")
        stamps_ep, req = self._helper_with_mode(mode)
        assert stamps_ep._enforce_spend_limits(req, 0.1, "stamp purchase") is not None, \
            f"mode {mode!r} was treated as a settled payment"

    def test_the_handler_passes_the_live_request_to_the_limits(self, tracker, monkeypatch):
        """The other paid tests drive the helper with a stand-in request, so
        they would still pass if the handler forgot to pass the real one and the
        payment state never reached the check.

        Mutating app middleware to simulate a settled payment was tried and
        leaked into later tests in the same file. This wraps the real helper
        instead: it sets the payment state on whatever request the handler
        actually passed, then calls through, so both the wiring and the bypass
        are exercised without touching global app state.
        """
        monkeypatch.setattr(settings, "X402_MAX_STAMP_BZZ", 0.0)
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", 0.001)
        monkeypatch.setattr(settings, "X402_NETWORK", "base")
        tracker.consume("testclient", 0.001)

        import app.api.endpoints.stamps as stamps_ep
        real = stamps_ep._enforce_spend_limits
        seen = {}

        def as_paid(request, cost_bzz, operation):
            seen["request"] = request
            request.state.x402_mode = "paid"
            # A real paid request is settled just before the purchase; mark it
            # settled so the handler does not try to reach a facilitator.
            from types import SimpleNamespace
            request.state.x402_settlement = SimpleNamespace(success=True, transaction="0xtx")
            return real(request, cost_bzz, operation)

        monkeypatch.setattr(stamps_ep, "_enforce_spend_limits", as_paid)

        with patch("app.services.swarm_api.get_chainstate",
                   new=AsyncMock(return_value=CHAINSTATE)), \
             patch("app.services.swarm_api.check_sufficient_funds",
                   new=AsyncMock(return_value=FUNDS_OK)), \
             patch("app.services.swarm_api.purchase_postage_stamp",
                   new=AsyncMock(return_value="b" * 64)):
            r = TestClient(app).post("/api/v1/stamps/",
                                     json={"depth": 17, "duration_hours": 24})

        from starlette.requests import Request as StarletteRequest
        assert isinstance(seen.get("request"), StarletteRequest), \
            "the handler did not pass the live request to the spending check"
        assert r.status_code == 201, r.text
        assert tracker.snapshot()["spent"] == {"testclient": 0.001}, \
            "a settled payment was charged to the giveaway budget"

    def test_an_exhausted_budget_still_refuses_without_a_payment(self, tracker, monkeypatch):
        """The control for the test above: same setup, no payment state set, so
        a pass there cannot be the budget quietly failing to apply."""
        monkeypatch.setattr(settings, "X402_MAX_STAMP_BZZ", 0.0)
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", 0.001)
        monkeypatch.setattr(settings, "X402_NETWORK", "base")
        tracker.consume("testclient", 0.001)

        with patch("app.services.swarm_api.get_chainstate",
                   new=AsyncMock(return_value=CHAINSTATE)), \
             patch("app.services.swarm_api.check_sufficient_funds",
                   new=AsyncMock(return_value=FUNDS_OK)), \
             patch("app.services.swarm_api.purchase_postage_stamp",
                   new=AsyncMock(return_value="b" * 64)):
            r = TestClient(app).post("/api/v1/stamps/",
                                     json={"depth": 17, "duration_hours": 24})
        assert r.status_code == 429



class TestReservation:
    """Limits are charged when a request is admitted, not after Bee returns (#363)."""

    def test_reserve_is_atomic_check_and_charge(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", 1.0)
        t = SpendBudgetTracker(state_file=str(tmp_path / "s.json"))
        hold, _, _ = t.reserve_all([("1.2.3.4", 0.6, 1.0)])
        assert hold is not None
        assert t.reserve_all([("1.2.3.4", 0.6, 1.0)])[0] is None, "a second concurrent request must see the first"
        t.release_hold(hold)
        t.release_hold(hold)   # idempotent
        assert t.reserve_all([("1.2.3.4", 0.6, 1.0)])[0] is not None

    def test_concurrent_extends_cannot_overrun_the_budget(self, tracker, monkeypatch):
        """The audit PoC, inverted: many parallel extends from one IP."""
        import asyncio
        import httpx
        monkeypatch.setattr(settings, "X402_MAX_STAMP_BZZ", 5.0)
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", 0.5)
        extended = []

        async def slow_extend(stamp_id, amount):
            await asyncio.sleep(0.05)
            extended.append(amount)
            return stamp_id

        async def run():
            transport = httpx.ASGITransport(app=app, client=("203.0.113.7", 1234))
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                return await asyncio.gather(*[
                    c.patch(f"/api/v1/stamps/{STAMP_ID}/extend", json={"duration_hours": 24})
                    for _ in range(20)])

        with patch("app.services.swarm_api.get_all_stamps_processed",
                   new=AsyncMock(return_value=[{"batchID": STAMP_ID, "depth": 20}])), \
             patch("app.services.swarm_api.get_chainstate",
                   new=AsyncMock(return_value={"currentPrice": "114458", "minimumValidityBlocks": 17280})), \
             patch("app.services.swarm_api.check_sufficient_funds", new=AsyncMock(return_value=FUNDS_OK)), \
             patch("app.services.swarm_api.extend_postage_stamp", new=slow_extend):
            asyncio.run(run())
        spent = sum(tracker.snapshot()["spent"].values())
        assert spent <= 0.5 + 1e-9
        assert len(extended) == 2   # ~0.218 BZZ each: two fit, the rest are refused

    def test_gateway_ceiling_bounds_all_callers_together(self, tracker, monkeypatch):
        monkeypatch.setattr(settings, "X402_MAX_STAMP_BZZ", 0.0)
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", UNLIMITED)
        monkeypatch.setattr(settings, "GATEWAY_DAILY_BZZ_CEILING", 0.01)  # ~0.0057 BZZ per purchase here
        codes = []
        for ip in ["1.1.1.1", "2.2.2.2", "3.3.3.3"]:
            with patch("app.services.swarm_api.get_chainstate", new=AsyncMock(return_value=CHAINSTATE)), \
                 patch("app.services.swarm_api.check_sufficient_funds", new=AsyncMock(return_value=FUNDS_OK)), \
                 patch("app.services.swarm_api.purchase_postage_stamp", new=AsyncMock(return_value=STAMP_ID)), \
                 patch("app.api.endpoints.stamps.get_client_ip", return_value=ip):
                codes.append(TestClient(app).post("/api/v1/stamps/", json={"depth": 17, "duration_hours": 24}).status_code)
        assert 503 in codes
        assert tracker.snapshot()["gateway_spent"] <= 0.01 + 1e-9

    def test_a_refused_request_gives_the_gateway_reservation_back(self, tracker, monkeypatch):
        monkeypatch.setattr(settings, "X402_MAX_STAMP_BZZ", 0.0)
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", 10.0)
        monkeypatch.setattr(settings, "GATEWAY_DAILY_BZZ_CEILING", 10.0)
        with patch("app.services.swarm_api.get_chainstate", new=AsyncMock(return_value=CHAINSTATE)), \
             patch("app.services.swarm_api.check_sufficient_funds",
                   new=AsyncMock(return_value={**FUNDS_OK, "sufficient": False, "shortfall_bzz": 1.0})):
            r = TestClient(app).post("/api/v1/stamps/", json={"depth": 17, "duration_hours": 24})
        assert r.status_code == 400
        assert tracker.snapshot()["gateway_spent"] == 0.0
        assert tracker.snapshot()["spent"] == {}



class TestHolds:
    def test_a_hold_from_yesterday_is_not_taken_off_today(self, tmp_path, monkeypatch):
        from app.services import spend_budget
        from app.services.spend_budget import GLOBAL_KEY
        t = SpendBudgetTracker(state_file=str(tmp_path / "s.json"))
        monkeypatch.setattr(spend_budget, "_today", lambda: "2099-01-01")
        t._day = "2099-01-01"
        hold, _, _ = t.reserve_all([(GLOBAL_KEY, 0.8, 1.0)])
        monkeypatch.setattr(spend_budget, "_today", lambda: "2099-01-02")
        t.reserve_all([(GLOBAL_KEY, 0.9, 1.0)])      # real spend of the new day
        t.release_hold(hold)                          # yesterday's failure
        assert t.snapshot()["gateway_spent"] == 0.9

    def test_reserve_all_is_all_or_nothing(self, tmp_path):
        t = SpendBudgetTracker(state_file=str(tmp_path / "s.json"))
        hold, refused, _ = t.reserve_all([("a", 0.5, 1.0), ("b", 0.5, 0.4)])
        assert hold is None and refused == "b"
        assert t.snapshot()["spent"] == {}

    def test_free_ceiling_keeps_headroom_for_paid_and_pool(self, tracker, monkeypatch):
        monkeypatch.setattr(settings, "X402_MAX_STAMP_BZZ", 0.0)
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", UNLIMITED)
        monkeypatch.setattr(settings, "GATEWAY_DAILY_BZZ_CEILING", 1.0)
        monkeypatch.setattr(settings, "GATEWAY_DAILY_BZZ_FREE_CEILING", 0.01)
        codes = []
        for ip in ["1.1.1.1", "2.2.2.2", "3.3.3.3"]:
            with patch("app.services.swarm_api.get_chainstate", new=AsyncMock(return_value=CHAINSTATE)), \
                 patch("app.services.swarm_api.check_sufficient_funds", new=AsyncMock(return_value=FUNDS_OK)), \
                 patch("app.services.swarm_api.purchase_postage_stamp", new=AsyncMock(return_value=STAMP_ID)), \
                 patch("app.api.endpoints.stamps.get_client_ip", return_value=ip):
                codes.append(TestClient(app).post("/api/v1/stamps/", json={"depth": 17, "duration_hours": 24}).status_code)
        assert 503 in codes
        # The pool can still reserve from the remaining gateway ceiling.
        hold, _ = tracker.reserve_gateway(0.5)
        assert hold is not None



class TestCeilingOnEveryPath:
    def test_extend_refused_by_bee_gives_everything_back(self, tracker, monkeypatch):
        monkeypatch.setattr(settings, "X402_MAX_STAMP_BZZ", 0.0)
        monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", 10.0)
        monkeypatch.setattr(settings, "GATEWAY_DAILY_BZZ_CEILING", 10.0)
        with patch("app.services.swarm_api.get_all_stamps_processed",
                   new=AsyncMock(return_value=[{"batchID": STAMP_ID, "depth": 17}])), \
             patch("app.services.swarm_api.get_chainstate", new=AsyncMock(return_value=CHAINSTATE)), \
             patch("app.services.swarm_api.check_sufficient_funds", new=AsyncMock(return_value=FUNDS_OK)), \
             patch("app.services.swarm_api.extend_postage_stamp", new=AsyncMock(side_effect=_bee_refusal())):
            TestClient(app).patch(f"/api/v1/stamps/{STAMP_ID}/extend", json={"duration_hours": 24})
        assert tracker.snapshot()["spent"] == {}
        assert tracker.snapshot()["gateway_spent"] == 0.0

    def test_for_owner_is_bounded_by_the_gateway_ceiling(self, tracker, monkeypatch):
        from app.api.endpoints import stamps_for_owner as ep
        monkeypatch.setattr(settings, "STAMP_PURCHASE_FOR_OTHERS_ENABLED", True)
        monkeypatch.setattr(settings, "STAMP_FOR_OTHERS_REQUIRE_WHITELIST", False)
        monkeypatch.setattr(settings, "STAMP_FOR_OTHERS_FREE_TIER_ENABLED", True)
        monkeypatch.setattr(settings, "GATEWAY_DAILY_BZZ_CEILING", 0.000001)
        chain = ep.gnosis_chain_client
        with patch("app.services.swarm_api.get_chainstate", new=AsyncMock(return_value=CHAINSTATE)), \
             patch.object(type(chain), "is_configured", new=property(lambda self: True)), \
             patch.object(chain, "preflight", new=AsyncMock(return_value={"is_critical": False, "warnings": []})), \
             patch.object(chain, "create_batch", new=AsyncMock()) as create:
            r = TestClient(app).post("/api/v1/stamps/for-owner", json={
                "owner": "0x" + "1" * 40, "depth": 17, "duration_hours": 24})
        assert r.status_code == 503
        assert r.json()["detail"]["code"] == "GATEWAY_DAILY_SPEND_CEILING"
        create.assert_not_called()



class TestClassification:
    @staticmethod
    def _status(code):
        import httpx
        return httpx.HTTPStatusError(str(code), request=httpx.Request("POST", "http://bee/x"),
                                     response=httpx.Response(code))

    def test_what_counts_as_certainly_unspent(self):
        import asyncio
        import httpx
        from fastapi import HTTPException
        from app.services.spend_budget import spend_certainly_did_not_happen as unspent
        req = httpx.Request("POST", "http://bee/x")
        released = [HTTPException(status_code=400), httpx.ConnectError("x", request=req),
                    httpx.ConnectTimeout("x", request=req), self._status(400), self._status(429)]
        kept = [httpx.ReadTimeout("x", request=req), httpx.WriteTimeout("x", request=req),
                httpx.RemoteProtocolError("x", request=req), self._status(500), self._status(503),
                asyncio.CancelledError(), ValueError("?"), RuntimeError("?")]
        assert all(unspent(e) for e in released)
        assert not any(unspent(e) for e in kept)



class TestForOwnerRelease:
    def _call(self, monkeypatch, error):
        from app.api.endpoints import stamps_for_owner as ep
        monkeypatch.setattr(settings, "STAMP_PURCHASE_FOR_OTHERS_ENABLED", True)
        monkeypatch.setattr(settings, "STAMP_FOR_OTHERS_REQUIRE_WHITELIST", False)
        monkeypatch.setattr(settings, "STAMP_FOR_OTHERS_FREE_TIER_ENABLED", True)
        monkeypatch.setattr(settings, "GATEWAY_DAILY_BZZ_CEILING", 10.0)
        chain = ep.gnosis_chain_client
        with patch("app.services.swarm_api.get_chainstate", new=AsyncMock(return_value=CHAINSTATE)), \
             patch.object(type(chain), "is_configured", new=property(lambda self: True)), \
             patch.object(chain, "preflight", new=AsyncMock(return_value={"is_critical": False, "warnings": []})), \
             patch.object(chain, "create_batch", new=AsyncMock(side_effect=error)):
            return TestClient(app, raise_server_exceptions=False).post("/api/v1/stamps/for-owner", json={
                "owner": "0x" + "1" * 40, "depth": 17, "duration_hours": 24})

    def test_a_gnosis_error_gives_the_hold_back(self, tracker, monkeypatch):
        from app.services.gnosis_chain import GnosisChainError
        assert self._call(monkeypatch, GnosisChainError("reverted")).status_code == 502
        assert tracker.snapshot()["gateway_spent"] == 0.0

    def test_an_uncertain_error_keeps_the_hold(self, tracker, monkeypatch):
        assert self._call(monkeypatch, TimeoutError("receipt")).status_code == 500
        assert tracker.snapshot()["gateway_spent"] > 0
