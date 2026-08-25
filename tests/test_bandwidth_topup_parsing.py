"""The `mb` top-up parameter must be parsed identically for pricing and crediting.

Regression for #260: the x402 pricing dependency used int(), which raises on a
float-formatted string, while the endpoint used Pydantic's lax int coercion,
which accepts one. Pricing fell back to the 100 MB minimum while the handler
credited the full value, so `?mb=1000000.0` was charged as 100 MB and credited
as 1,000,000 MB — a 10,000x discrepancy in the caller's favour.
"""
import pytest
from unittest.mock import AsyncMock, patch

from starlette.requests import Request

from app.core.config import settings
from app.services.bandwidth_credit import (
    BYTES_PER_MB, effective_topup_mb, parse_topup_mb,
)


def _request(query: str) -> Request:
    return Request({
        "type": "http", "path": "/api/v1/chunks/credit",
        "query_string": query.encode(), "headers": [],
    })


async def _priced_mb(query: str) -> int:
    """The MB figure the x402 quote is actually built from."""
    from app.x402 import dependency
    seen = {}

    async def fake_quote(operation, size_bytes):
        seen["size_bytes"] = size_bytes
        return {"price_usd": 0.0}

    with patch.object(dependency, "get_price_quote", new=AsyncMock(side_effect=fake_quote)):
        await dependency._calculate_price_for_request(_request(query))
    return seen["size_bytes"] // BYTES_PER_MB


class TestParseTopUpMb:
    @pytest.mark.parametrize("raw,expected", [
        ("100", 100), ("1000000", 1000000), (" 250 ", 250), (7, 7),
    ])
    def test_accepts_whole_numbers(self, raw, expected):
        assert parse_topup_mb(raw) == expected

    @pytest.mark.parametrize("raw", [
        "1000000.0",   # the exploit: accepted by Pydantic, rejected by int()
        "1e6", "abc", "", "  ", None, "0x10", True, False,
    ])
    def test_rejects_anything_not_a_plain_integer(self, raw):
        assert parse_topup_mb(raw) is None

    def test_booleans_are_not_integers_here(self):
        """bool is an int subclass; accepting it would credit 1 MB for `?mb=true`."""
        assert parse_topup_mb(True) is None


class TestPricingAndCreditingAgree:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("query", ["mb=100", "mb=1000000", "mb=250"])
    async def test_priced_amount_equals_credited_amount(self, query):
        raw = query.split("=", 1)[1]
        credited = effective_topup_mb(parse_topup_mb(raw))
        assert await _priced_mb(query) == credited

    @pytest.mark.asyncio
    async def test_float_formatted_value_is_not_credited_more_than_priced(self):
        """The exploit case. Pricing may floor it; the handler must refuse it."""
        assert parse_topup_mb("1000000.0") is None, "must not be creditable"
        assert await _priced_mb("mb=1000000.0") == settings.BANDWIDTH_CREDIT_MIN_TOPUP_MB

    @pytest.mark.asyncio
    async def test_below_minimum_is_priced_at_the_floor_and_refused_by_the_handler(self):
        """Pricing floors a small value; the handler refuses it as TOPUP_TOO_SMALL.

        These are consistent: the floored price is never charged because the
        request does not complete.
        """
        assert await _priced_mb("mb=1") == settings.BANDWIDTH_CREDIT_MIN_TOPUP_MB
        assert parse_topup_mb("1") == 1  # parsed as given; refused downstream

    def test_effective_mb_never_lowers_a_large_request(self):
        assert effective_topup_mb(5000) == 5000


class TestSingleSourceOfTruth:
    """The MB-to-bytes constant must exist once.

    Two copies is precisely how pricing and crediting drifted apart in the first
    place. If one were changed to 1024*1024 and the other left at 10^6, the same
    class of divergence returns under a different name.
    """

    def test_endpoint_uses_the_same_constant_as_the_credit_module(self):
        from app.api.endpoints import chunks
        from app.services import bandwidth_credit

        assert chunks.BYTES_PER_MB is bandwidth_credit.BYTES_PER_MB

    def test_pricing_uses_it_too(self):
        """The x402 dependency converts with the same constant."""
        import inspect
        from app.x402 import dependency

        source = inspect.getsource(dependency._calculate_price_for_request)
        assert "BYTES_PER_MB" in source, "pricing must not hardcode the conversion"
        assert "1_000_000" not in source and "1000000" not in source


class TestTopUpCeiling:
    """A single top-up must be bounded (#283).

    The minimum was enforced; the maximum was not, so one request could credit
    an unbounded amount. Pricing happens first, so economics discourage it — but
    that guarantee depends on pricing and crediting agreeing about the number,
    which is exactly what #260 showed cannot be assumed.
    """

    @pytest.mark.asyncio
    async def test_above_maximum_is_refused(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from fastapi import HTTPException
        from app.api.endpoints import chunks

        from tests.test_chunks_billing import _enabled_settings

        req = SimpleNamespace(state=SimpleNamespace(x402_mode="paid", x402_payer="0xP"))
        over = 1_000_001
        with patch("app.api.endpoints.chunks.settings", _enabled_settings()), \
             patch("app.api.endpoints.chunks.bandwidth_credit_manager", MagicMock()):
            with pytest.raises(HTTPException) as exc:
                await chunks.top_up_credit(req, mb=str(over))

        assert exc.value.status_code == 400
        assert exc.value.detail["code"] == "TOPUP_TOO_LARGE"
        assert exc.value.detail["max_topup_mb"] == 1_000_000

    def test_maximum_is_above_the_minimum(self):
        """A misconfiguration inverting them would refuse every request."""
        assert settings.BANDWIDTH_CREDIT_MAX_TOPUP_MB > settings.BANDWIDTH_CREDIT_MIN_TOPUP_MB
