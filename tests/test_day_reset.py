"""resets_at must be a timestamp ordinary parsers accept (#512).

It used to be built as "<day>T24:00:00Z". Hour 24 is legal in some readings
of ISO 8601 but Python's datetime.fromisoformat, among others, rejects it, so
a client parsing resets_at to schedule a retry failed.
"""
from datetime import datetime

import pytest

from app.core.day_reset import next_reset_at


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@pytest.mark.parametrize("day, expected", [
    ("2026-10-22", "2026-10-23T00:00:00Z"),   # an ordinary day
    ("2026-10-31", "2026-11-01T00:00:00Z"),   # month end
    ("2026-12-31", "2027-01-01T00:00:00Z"),   # year end
    ("2028-02-28", "2028-02-29T00:00:00Z"),   # into a leap day
    ("2028-02-29", "2028-03-01T00:00:00Z"),   # out of a leap day
])
def test_the_reset_is_the_start_of_the_next_day(day, expected):
    value = next_reset_at(day)
    assert value == expected
    parsed = _parse(value)
    assert parsed.hour == 0 and parsed.minute == 0 and parsed.second == 0
    assert parsed.utcoffset().total_seconds() == 0
