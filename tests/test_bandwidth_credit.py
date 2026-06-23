# tests/test_bandwidth_credit.py
"""
Tests for the prepaid bandwidth credit ledger (issue #220).
"""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.services.bandwidth_credit import BandwidthCreditManager


@pytest.fixture
def state_file(tmp_path):
    return str(tmp_path / "bandwidth_credit.json")


@pytest.fixture
def manager(state_file):
    return BandwidthCreditManager(state_file=state_file)


class TestCredit:
    def test_credit_increases_balance(self, manager):
        assert manager.credit("0xABC", 1000) == 1000
        assert manager.credit("0xABC", 500) == 1500
        assert manager.balance("0xABC") == 1500

    def test_credit_tracks_total_topped_up(self, manager):
        manager.credit("0xABC", 1000)
        manager.debit("0xABC", 400)
        manager.credit("0xABC", 200)
        info = manager.get_info("0xABC")
        assert info["balance_bytes"] == 800
        assert info["total_topped_up_bytes"] == 1200

    def test_credit_non_positive_raises(self, manager):
        with pytest.raises(ValueError):
            manager.credit("0xABC", 0)
        with pytest.raises(ValueError):
            manager.credit("0xABC", -5)

    def test_credit_empty_address_raises(self, manager):
        with pytest.raises(ValueError):
            manager.credit("", 100)


class TestDebit:
    def test_debit_success(self, manager):
        manager.credit("0xABC", 1000)
        ok, remaining = manager.debit("0xABC", 300)
        assert ok is True
        assert remaining == 700
        assert manager.balance("0xABC") == 700

    def test_debit_exact_balance(self, manager):
        manager.credit("0xABC", 500)
        ok, remaining = manager.debit("0xABC", 500)
        assert ok is True
        assert remaining == 0

    def test_debit_insufficient_leaves_balance_unchanged(self, manager):
        manager.credit("0xABC", 100)
        ok, remaining = manager.debit("0xABC", 101)
        assert ok is False
        assert remaining == 100
        assert manager.balance("0xABC") == 100

    def test_debit_unknown_address(self, manager):
        ok, remaining = manager.debit("0xNOPE", 1)
        assert ok is False
        assert remaining == 0

    def test_debit_non_positive_raises(self, manager):
        manager.credit("0xABC", 100)
        with pytest.raises(ValueError):
            manager.debit("0xABC", 0)
        with pytest.raises(ValueError):
            manager.debit("0xABC", -1)


class TestBalanceAndInfo:
    def test_balance_unknown_is_zero(self, manager):
        assert manager.balance("0xUNKNOWN") == 0

    def test_get_info_unknown_is_none(self, manager):
        assert manager.get_info("0xUNKNOWN") is None

    def test_address_normalization_case_insensitive(self, manager):
        manager.credit("0xAbCdEf", 1000)
        assert manager.balance("0xabcdef") == 1000
        ok, remaining = manager.debit("0xABCDEF", 250)
        assert ok is True
        assert remaining == 750

    def test_account_count_only_nonzero(self, manager):
        manager.credit("0xA", 100)
        manager.credit("0xB", 100)
        manager.debit("0xB", 100)  # drains B to zero
        assert manager.account_count() == 1


class TestPersistence:
    def test_save_load_round_trip(self, state_file):
        mgr1 = BandwidthCreditManager(state_file=state_file)
        mgr1.credit("0xA", 1000)
        mgr1.credit("0xB", 2000)
        mgr1.debit("0xA", 250)

        mgr2 = BandwidthCreditManager(state_file=state_file)
        mgr2.load_on_startup()
        assert mgr2.balance("0xA") == 750
        assert mgr2.balance("0xB") == 2000

    def test_corrupt_file_starts_fresh(self, state_file):
        with open(state_file, "w") as f:
            f.write("{{not valid json")
        mgr = BandwidthCreditManager(state_file=state_file)
        mgr.load_on_startup()
        assert mgr.balance("0xanything") == 0

    def test_missing_file_starts_fresh(self, state_file):
        mgr = BandwidthCreditManager(state_file=state_file)
        mgr.load_on_startup()
        assert mgr.balance("0xanything") == 0

    def test_state_file_contents_after_credit(self, state_file, manager):
        manager.credit("0xA", 1234)
        with open(state_file) as f:
            saved = json.load(f)
        assert saved["0xa"]["balance_bytes"] == 1234


class TestConcurrency:
    def test_concurrent_debits_never_overspend(self, manager):
        """1000 concurrent 1-byte debits against a 600-byte balance: exactly 600 succeed,
        balance ends at 0, never negative."""
        manager.credit("0xRACE", 600)

        def attempt(_):
            ok, _rem = manager.debit("0xRACE", 1)
            return ok

        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(attempt, range(1000)))

        assert sum(1 for r in results if r) == 600
        assert manager.balance("0xRACE") == 0

    def test_concurrent_credits_sum_correctly(self, manager):
        def attempt(_):
            manager.credit("0xSUM", 10)

        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(attempt, range(100)))

        assert manager.balance("0xSUM") == 1000
