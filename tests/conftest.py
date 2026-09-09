"""
Shared test configuration.

Sets environment variables before any app modules are imported,
ensuring test-friendly defaults (e.g., rate limiting disabled).
"""
import os

# Disable global rate limiting during tests to prevent 429 responses
# from interfering with test assertions. Rate limiter unit tests
# test the component directly without relying on middleware.
os.environ["RATE_LIMIT_ENABLED"] = "false"

# The daily spend budget lives in a module-level singleton with persisted state,
# so without this every purchase and extend in the suite charges the same caller
# ("testclient") and the budget is exhausted partway through — turning unrelated
# tests into 429s depending on how many ran before them.
#
# Reset per test, and pointed at a temporary file so a test run never writes to
# the real state path. Tests that exercise the budget itself construct their own
# tracker and are unaffected.
import pytest


@pytest.fixture(autouse=True)
def _isolate_spend_budget(tmp_path, monkeypatch):
    from app.services import spend_budget

    from app.core.config import settings

    # Unlimited, so the limit never decides the outcome of a test about
    # something else — several suites purchase at the top of the valid amount
    # range, which costs far more than any plausible budget. check() and
    # consume() still run on every purchase and extend, so the plumbing stays
    # covered; only the refusal is off. Tests about the budget set their own.
    monkeypatch.setattr(settings, "STAMP_DAILY_BZZ_PER_CALLER", -1.0)

    # Same reasoning for the per-request ceiling. Several suites deliberately
    # purchase at the top of the valid depth and amount ranges to test that
    # validation accepts them; at the production default of 5 BZZ those requests
    # legitimately cost more than the cap allows, and the test would then be
    # asserting the cap rather than the validation it was written for.
    monkeypatch.setattr(settings, "X402_MAX_STAMP_BZZ", 0.0)  # 0 disables the cap

    tracker = spend_budget.SpendBudgetTracker(state_file=str(tmp_path / "spend.json"))
    monkeypatch.setattr(spend_budget, "spend_budget_tracker", tracker)
    import app.api.endpoints.stamps as stamps_ep
    monkeypatch.setattr(stamps_ep, "spend_budget_tracker", tracker)
    yield tracker
