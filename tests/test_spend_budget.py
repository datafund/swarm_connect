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
                   new=AsyncMock(side_effect=RuntimeError("bee said no"))):
            TestClient(app).post("/api/v1/stamps/",
                                 json={"depth": 17, "duration_hours": 24})
        assert tracker.snapshot()["spent"] == {}, "a failed purchase charged the caller"


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

        # None means "charge nobody" — the caller funded it themselves.
        assert stamps_ep._enforce_spend_limits(_Req(), 5.0, "stamp purchase") is None
        assert tracker.snapshot()["spent"] == {"testclient": 0.001}, "the payer was charged"

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
