"""The for-owner quote must describe the batch the endpoint actually builds.

Regression for #261: pricing inspected the raw JSON with isinstance checks,
which disagree with Pydantic's lax coercion. isinstance("22", int) is False, so
a string-formatted depth priced at the depth-17 default while the endpoint
built the depth-22 batch requested. Cost scales as amount(duration) * 2^depth,
so the quote could be a small fraction of the batch created — and the shortfall
comes out of the gateway's own Gnosis wallet.
"""
import json

import pytest
from unittest.mock import AsyncMock, patch

from starlette.requests import Request

from app.api.models.stamp import StampForOwnerRequest
from app.x402 import dependency

OWNER = "0x571dEAC541E65312Bdb027E1C570e2751f8A6795"


def _request(body: dict) -> Request:
    raw = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request({
        "type": "http", "path": "/api/v1/stamps/for-owner", "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
    }, receive)


async def _priced(body: dict) -> dict:
    """Capture the depth and duration the quote is actually built from."""
    seen = {}

    async def fake_quote(operation, duration_hours=None, depth=None, **kw):
        seen.update(depth=depth, duration_hours=duration_hours)
        return {"price_usd": 0.0}

    with patch.object(dependency, "get_price_quote", new=AsyncMock(side_effect=fake_quote)):
        await dependency._calculate_price_for_request(_request(body))
    return seen


BODIES = [
    pytest.param({"owner": OWNER, "depth": 22, "duration_hours": 168}, id="native-ints"),
    pytest.param({"owner": OWNER, "depth": "22", "duration_hours": "168"}, id="strings"),
    pytest.param({"owner": OWNER, "depth": 20.0, "duration_hours": 168.0}, id="floats"),
    pytest.param({"owner": OWNER, "size": "large", "duration_hours": 48}, id="size-preset"),
    pytest.param({"owner": OWNER, "size": "small"}, id="defaults"),
    pytest.param({"owner": OWNER, "depth": 16, "duration_hours": 24}, id="minimums"),
]


class TestPricingMatchesWhatIsBuilt:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("body", BODIES)
    async def test_priced_batch_equals_built_batch(self, body):
        priced = await _priced(body)
        built = StampForOwnerRequest.model_validate(body)

        assert priced["depth"] == built.get_effective_depth()
        assert priced["duration_hours"] == built.duration_hours

    @pytest.mark.asyncio
    async def test_string_depth_is_not_priced_as_the_default(self):
        """The specific exploit: "22" previously priced as depth 17."""
        priced = await _priced({"owner": OWNER, "depth": "22", "duration_hours": "168"})
        assert priced["depth"] == 22, "must not fall back to the depth-17 default"
        assert priced["duration_hours"] == 168

    @pytest.mark.asyncio
    async def test_size_preset_beats_depth_exactly_as_the_model_does(self):
        """get_effective_depth gives size precedence; pricing must not differ."""
        body = {"owner": OWNER, "size": "small", "depth": 22}
        priced = await _priced(body)
        assert priced["depth"] == StampForOwnerRequest.model_validate(body).get_effective_depth()

    @pytest.mark.asyncio
    async def test_unacceptable_body_prices_at_the_smallest_defaults(self):
        """A body the endpoint will reject must not be quoted as something large.

        The quote is never charged, since the request fails validation — but it
        should not guess upward.
        """
        priced = await _priced({"owner": "not-an-address", "depth": 32})
        assert priced["depth"] == 17
        assert priced["duration_hours"] == 24

    @pytest.mark.asyncio
    async def test_malformed_json_does_not_raise(self):
        async def receive():
            return {"type": "http.request", "body": b"{not json", "more_body": False}

        request = Request({
            "type": "http", "path": "/api/v1/stamps/for-owner", "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
        }, receive)

        with patch.object(dependency, "get_price_quote",
                          new=AsyncMock(return_value={"price_usd": 0.0})):
            result = await dependency._calculate_price_for_request(request)
        assert "price_usd" in result


class TestCapsRejectRatherThanClamp:
    """The endpoint's caps must refuse an over-cap request, not shrink it.

    Pricing quotes the batch the model describes. If a cap silently clamped
    depth or duration down instead of rejecting, the endpoint would build
    something smaller than was quoted and the caller would overpay — the same
    divergence this PR removes, reintroduced one layer further down.
    """

    def test_depth_cap_raises(self):
        import inspect
        from app.api.endpoints import stamps_for_owner

        src = inspect.getsource(stamps_for_owner.create_batch_for_owner)
        assert "DEPTH_TOO_HIGH" in src
        # A clamp would look like min(...) or an assignment back onto depth.
        assert "depth = min(" not in src, "cap must reject, not clamp"

    def test_duration_cap_raises(self):
        import inspect
        from app.api.endpoints import stamps_for_owner

        src = inspect.getsource(stamps_for_owner.create_batch_for_owner)
        assert "duration" in src and "exceeds max" in src
        assert "duration_hours = min(" not in src, "cap must reject, not clamp"

    @pytest.mark.asyncio
    async def test_over_cap_body_is_still_priced_as_requested(self):
        """Pricing does not apply the caps, and should not.

        The request is refused at the endpoint, so the quote is never charged.
        Quoting the requested batch keeps the two paths describing the same
        thing; applying caps only in pricing would recreate the mismatch.
        """
        body = {"owner": OWNER, "depth": 32, "duration_hours": 8760}
        priced = await _priced(body)
        built = StampForOwnerRequest.model_validate(body)
        assert priced["depth"] == built.get_effective_depth() == 32
        assert priced["duration_hours"] == built.duration_hours == 8760
