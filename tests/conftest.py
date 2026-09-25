"""
Shared test configuration.

Sets environment variables before any app modules are imported,
ensuring test-friendly defaults (e.g., rate limiting disabled).
"""
import atexit
import os
import shutil
import tempfile

# Disable global rate limiting during tests to prevent 429 responses
# from interfering with test assertions. Rate limiter unit tests
# test the component directly without relying on middleware.
os.environ["RATE_LIMIT_ENABLED"] = "false"

# Send every persisted state file to a temporary directory for the run.
#
# Five services keep state in module-level singletons that write to paths under
# data/ by default: the stamp pool inventory, the pool daily allowance, the stamp
# ownership registry, the bandwidth credit ledger and the daily spend budget. A
# test run wrote to all of them (#335).
#
# On a machine also running a local gateway, that means a test run overwrites the
# state the running instance is using. The ownership registry is the damaging
# one: check_access denies batches it has no record of, so a clobbered registry
# makes uploads that worked a minute ago start failing with no visible cause.
# It also let state leak between runs, so a suite that passed on a clean checkout
# could behave differently the second time.
#
# Set here rather than in a fixture because the singletons are constructed at
# import time, and pydantic-settings reads the environment when Settings is first
# built. A fixture would run too late. Every one of these services resolves its
# path lazily from settings when no explicit file is given, so redirecting the
# settings redirects all of them.
_STATE_DIR = tempfile.mkdtemp(prefix="swarm_connect_test_state_")
atexit.register(shutil.rmtree, _STATE_DIR, True)

for _var, _name in (
    ("STAMP_POOL_STATE_FILE", "pool_state.json"),
    ("POOL_ALLOWANCE_STATE_FILE", "pool_allowance.json"),
    ("STAMP_OWNERSHIP_FILE", "stamp_owners.json"),
    ("BANDWIDTH_CREDIT_STATE_FILE", "bandwidth_credit.json"),
    ("STAMP_SPEND_BUDGET_STATE_FILE", "stamp_spend_budget.json"),
    ("X402_AUDIT_LOG_PATH", "x402_audit.jsonl"),
    ("X402_IDEMPOTENCY_STATE_FILE", "x402_idempotency.json"),
):
    os.environ[_var] = os.path.join(_STATE_DIR, _name)

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


@pytest.fixture(autouse=True)
def _reset_payment_replay_guard():
    """Each test starts with no payment authorizations reserved.

    The guard is process-wide by design (one authorization, one delivery), and
    tests reuse the same signed test authorization.
    """
    from app.x402.settlement import replay_guard
    from app.x402.idempotency import idempotency_store
    replay_guard.reset()
    idempotency_store.reset()
    yield
    replay_guard.reset()
    idempotency_store.reset()
