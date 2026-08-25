"""A stamp's status must say what is actually wrong with it.

Regression for #252: calculate_usable_status() collapsed six distinct causes
into one boolean, and consumers rendered that boolean as "Expired". The result
was a list in which a batch expiring at 12-49 was shown as Expired beside
batches expiring at 12-48 shown as Usable — the honest signal and the displayed
signal pointing in opposite directions, so an agent sorting by expiry picks the
unusable one.
"""
import pytest

from app.services.swarm_api import (
    UNUSABLE_EXPIRED, UNUSABLE_EXPIRING_SOON, UNUSABLE_FULL,
    UNUSABLE_INVALID_DEPTH, UNUSABLE_NOT_FOUND, UNUSABLE_UNREADABLE,
    calculate_usable_status, get_unusable_reason,
)


def _stamp(**over):
    d = {"exists": True, "batchTTL": 86400, "depth": 18, "immutableFlag": False}
    d.update(over)
    return d


class TestReasonIsSpecific:
    def test_usable_stamp_has_no_reason(self):
        assert get_unusable_reason(_stamp()) is None

    def test_expired(self):
        assert get_unusable_reason(_stamp(batchTTL=0))["code"] == UNUSABLE_EXPIRED

    def test_expiring_soon_is_not_reported_as_expired(self):
        """It still has time left. Calling it expired sends the caller to
        re-purchase when a top-up would do."""
        r = get_unusable_reason(_stamp(batchTTL=30))
        assert r["code"] == UNUSABLE_EXPIRING_SOON
        assert r["code"] != UNUSABLE_EXPIRED

    def test_immutable_needs_more_headroom(self):
        """Immutable batches cannot be topped up, so they need more TTL."""
        ttl = 1800  # fine for mutable, not for immutable
        assert get_unusable_reason(_stamp(batchTTL=ttl)) is None
        assert get_unusable_reason(_stamp(batchTTL=ttl, immutableFlag=True))["code"] == \
            UNUSABLE_EXPIRING_SOON

    def test_full_batch_with_plenty_of_time_left(self):
        """The #252 case: long TTL, still unusable, and never described as expired."""
        r = get_unusable_reason(_stamp(batchTTL=86400), utilization_percent=100.0)
        assert r["code"] == UNUSABLE_FULL
        assert "expire" not in r["message"].lower()

    def test_invalid_depth(self):
        assert get_unusable_reason(_stamp(depth=10))["code"] == UNUSABLE_INVALID_DEPTH

    def test_missing_batch(self):
        assert get_unusable_reason(_stamp(exists=False))["code"] == UNUSABLE_NOT_FOUND

    def test_unreadable_data(self):
        assert get_unusable_reason(_stamp(batchTTL="not-a-number"))["code"] == UNUSABLE_UNREADABLE

    def test_every_reason_carries_a_message(self):
        for stamp, util in [(_stamp(batchTTL=0), None), (_stamp(batchTTL=30), None),
                            (_stamp(depth=10), None), (_stamp(exists=False), None),
                            (_stamp(batchTTL="x"), None), (_stamp(), 100.0)]:
            r = get_unusable_reason(stamp, util)
            assert r and r["message"] and r["code"]


class TestReproducesTheReportedContradiction:
    def test_longer_lived_full_batch_versus_shorter_lived_usable_ones(self):
        """Reconstructs the three rows from the report.

        The batch with the LATEST expiry is the unusable one — which is only
        coherent if the status names capacity rather than time.
        """
        later_but_full = _stamp(batchTTL=86460)          # expires last
        earlier_usable_a = _stamp(batchTTL=86400)
        earlier_usable_b = _stamp(batchTTL=86400)

        assert get_unusable_reason(later_but_full, utilization_percent=100.0)["code"] == UNUSABLE_FULL
        assert get_unusable_reason(earlier_usable_a, utilization_percent=10.0) is None
        assert get_unusable_reason(earlier_usable_b, utilization_percent=10.0) is None


class TestBooleanWrapperUnchanged:
    """Existing callers keep the boolean contract."""

    @pytest.mark.parametrize("stamp,util,expected", [
        (_stamp(), None, True),
        (_stamp(batchTTL=0), None, False),
        (_stamp(depth=10), None, False),
        (_stamp(exists=False), None, False),
        (_stamp(), 100.0, False),
    ])
    def test_matches_reason_presence(self, stamp, util, expected):
        assert calculate_usable_status(stamp, util) is expected
        assert (get_unusable_reason(stamp, util) is None) is expected


class TestReasonReachesTheApiResponse:
    """A field on the model that nothing populates is a silent no-op.

    The value of this change is entirely in the reason reaching the caller, so
    assert it survives serialisation rather than only that the model declares it.
    """

    def _base(self, **over):
        d = {"batchID": "a" * 64, "amount": "1", "depth": 17, "bucketDepth": 16,
             "batchTTL": 0, "expectedExpiration": "2026-08-26-00-00", "local": True}
        d.update(over)
        return d

    def test_reason_is_serialised(self):
        from app.api.models.stamp import StampDetails

        m = StampDetails(**self._base(usable=False, unusableReason="expired",
                                      unusableMessage="the batch has expired"))
        dump = m.model_dump()
        assert dump["unusableReason"] == "expired"
        assert dump["unusableMessage"] == "the batch has expired"

    def test_usable_stamp_carries_no_reason(self):
        from app.api.models.stamp import StampDetails

        m = StampDetails(**self._base(usable=True))
        assert m.model_dump()["unusableReason"] is None
        assert m.model_dump()["unusableMessage"] is None

    @pytest.mark.asyncio
    async def test_processing_populates_the_reason_for_a_full_batch(self):
        """End to end through get_all_stamps_processed, not just the model."""
        from unittest.mock import AsyncMock, patch
        from app.services import swarm_api as api

        batch = {"batchID": "b" * 64, "amount": "1", "depth": 20, "bucketDepth": 16,
                 "batchTTL": 86400, "immutable": False}
        local = {"batchID": "b" * 64, "utilization": 2 ** (20 - 16), "usable": True}

        with patch.object(api, "get_all_stamps", new=AsyncMock(return_value=[batch])), \
             patch.object(api, "get_local_stamps", new=AsyncMock(return_value=[local])):
            processed = await api.get_all_stamps_processed()

        row = processed[0]
        assert row["usable"] is False
        assert row["unusableReason"] == UNUSABLE_FULL
        assert "expire" not in row["unusableMessage"].lower()
