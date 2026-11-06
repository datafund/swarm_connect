# app/core/day_reset.py
"""When a daily (UTC) limit resets, as a timestamp clients can parse.

The daily counters (pool allowance, spend budget) are keyed by the UTC day
"YYYY-MM-DD" and reset at the next midnight. That moment used to be written
as "<day>T24:00:00Z"; hour 24 is rejected by common parsers, Python's
datetime.fromisoformat among them (#512). It is the start of the next day.
"""
from datetime import date, timedelta


def next_reset_at(day: str) -> str:
    """The start of the UTC day after `day` ("YYYY-MM-DD"), e.g. "2027-01-01T00:00:00Z"."""
    return (date.fromisoformat(day) + timedelta(days=1)).isoformat() + "T00:00:00Z"
