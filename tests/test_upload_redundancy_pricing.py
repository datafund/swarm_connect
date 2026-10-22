# tests/test_upload_redundancy_pricing.py
"""Paid uploads are priced for the erasure-coding parity Bee adds (#429).

The gateway always asks Bee for a redundancy level (the `redundancy` query
parameter, or DEFAULT_REDUNDANCY_LEVEL), and Bee pushes parity chunks on top of
the data chunks. The upload price is bandwidth, so it covers those bytes too.
"""
import pytest
from starlette.requests import Request

from app.core.config import settings
from app.services import swarm_api
from app.x402.dependency import _calculate_price_for_request
from app.x402.pricing import calculate_bandwidth_price_usd

# Copied from ethersphere/bee v2.8.1, pkg/file/redundancy/level.go
# (mediumEt, strongEt, insaneEt, paranoidEt): shard thresholds and the parity
# count Bee uses for a group of at least that many data chunks.
BEE_V2_8_1_ERASURE_TABLES = {
    1: ([95, 69, 47, 29, 15, 6, 2, 1], [9, 8, 7, 6, 5, 4, 3, 2]),
    2: ([105, 96, 87, 78, 70, 62, 54, 47, 40, 33, 27, 21, 16, 11, 7, 4, 2, 1],
        [21, 20, 19, 18, 17, 16, 15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4]),
    3: ([93, 88, 83, 78, 74, 69, 64, 60, 55, 51, 46, 42, 38, 34, 30, 27, 23, 20, 17, 14, 11, 9, 6, 4, 3, 2, 1],
        [31, 30, 29, 28, 27, 26, 25, 24, 23, 22, 21, 20, 19, 18, 17, 16, 15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5]),
    4: (list(range(37, 0, -1)),
        [89, 87, 86, 84, 83, 81, 80, 78, 76, 75, 73, 71, 70, 68, 66, 65, 63, 61, 59, 58,
         56, 54, 52, 50, 48, 47, 45, 43, 40, 38, 36, 34, 31, 29, 26, 23, 19]),
}
BRANCHES = 128  # swarm.Branches


def _bee_get_parities(level, shards):
    """erasureTable.getParities, as in Bee."""
    thresholds, parities = BEE_V2_8_1_ERASURE_TABLES[level]
    for s, p in zip(thresholds, parities):
        if shards >= s:
            return p
    return 0


def _expected_bytes(size, level):
    """Bytes pushed for `size` data bytes in full 128-branch groups."""
    if level == 0:
        return size
    data = BRANCHES - _bee_get_parities(level, BRANCHES)  # Level.GetMaxShards
    return -(-size * BRANCHES // data)


def _request(path="/api/v1/data/", query="", headers=None):
    headers = headers if headers is not None else {}
    return Request({
        "type": "http", "method": "POST", "path": path,
        "query_string": query.encode(),
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    })


@pytest.fixture
def above_floor(monkeypatch):
    """A bandwidth rate high enough that the size, not the floor, sets the price."""
    monkeypatch.setattr(settings, "X402_MIN_PRICE_USD", 0.0)
    monkeypatch.setattr(settings, "X402_BANDWIDTH_USD_PER_GB", 1000.0)


class TestOverheadTable:
    def test_parities_match_bee_v2_8_1(self):
        for level in (1, 2, 3, 4):
            data = BRANCHES - _bee_get_parities(level, BRANCHES)
            # Bee encodes a full group of GetMaxShards data chunks with
            # getParities(GetMaxShards) parities.
            assert swarm_api.REDUNDANCY_ERASURE_GROUP[level] == (data, _bee_get_parities(level, data))

    def test_every_accepted_level_has_an_overhead(self):
        assert set(swarm_api.REDUNDANCY_ERASURE_GROUP) | {0} == set(swarm_api.REDUNDANCY_LEVELS)

    @pytest.mark.parametrize("level,factor", [(0, 1.0), (1, 128 / 119), (2, 128 / 107),
                                              (3, 128 / 97), (4, 128 / 39)])
    def test_factors(self, level, factor):
        assert swarm_api.redundancy_overhead_factor(level) == pytest.approx(factor)

    @pytest.mark.parametrize("level", [0, 1, 2, 3, 4])
    @pytest.mark.parametrize("size", [0, 1, 4096, 4_000_000, 10 * 1024 * 1024])
    def test_priced_bytes_round_up(self, level, size):
        assert swarm_api.bytes_with_redundancy(size, level) == _expected_bytes(size, level)


class TestUploadPricedWithRedundancy:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("level", [0, 1, 2, 3, 4])
    async def test_requested_level_is_priced(self, above_floor, level):
        q = await _calculate_price_for_request(
            _request(query=f"stamp_id=ab&redundancy={level}", headers={"Content-Length": "4000000"}))
        assert q["price_usd"] == calculate_bandwidth_price_usd(_expected_bytes(4_000_000, level))["price_usd"]

    @pytest.mark.asyncio
    async def test_paranoid_costs_more_than_three_times_none(self, above_floor):
        none = await _calculate_price_for_request(
            _request(query="redundancy=0", headers={"Content-Length": "4000000"}))
        paranoid = await _calculate_price_for_request(
            _request(query="redundancy=4", headers={"Content-Length": "4000000"}))
        assert paranoid["price_usd"] > 3 * none["price_usd"]

    @pytest.mark.asyncio
    async def test_no_parameter_prices_the_default_level_bee_is_sent(self, above_floor):
        q = await _calculate_price_for_request(_request(headers={"Content-Length": "4000000"}))
        expected = _expected_bytes(4_000_000, swarm_api.DEFAULT_REDUNDANCY_LEVEL)
        assert q["price_usd"] == calculate_bandwidth_price_usd(expected)["price_usd"]

    @pytest.mark.asyncio
    async def test_manifest_upload_is_priced_the_same_way(self, above_floor):
        q = await _calculate_price_for_request(_request(
            path="/api/v1/data/manifest", query="redundancy=3", headers={"Content-Length": "4000000"}))
        assert q["price_usd"] == calculate_bandwidth_price_usd(_expected_bytes(4_000_000, 3))["price_usd"]

    @pytest.mark.asyncio
    async def test_without_content_length_the_cap_is_multiplied(self, above_floor):
        q = await _calculate_price_for_request(_request(query="redundancy=4"))
        cap = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
        assert q["price_usd"] == calculate_bandwidth_price_usd(_expected_bytes(cap, 4))["price_usd"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value,level", [("+4", 4), (" 4 ", 4), ("04", 4), ("4.0", 4)])
    async def test_parsed_like_the_endpoint_parses_it(self, above_floor, value, level):
        """Anything the endpoint accepts as level N is priced as level N."""
        q = await _calculate_price_for_request(
            _request(query=f"redundancy={value}", headers={"Content-Length": "4000000"}))
        assert q["price_usd"] == calculate_bandwidth_price_usd(_expected_bytes(4_000_000, level))["price_usd"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["5", "-1", "abc", "4.5", ""])
    async def test_a_value_the_endpoint_refuses_is_priced_at_the_default(self, above_floor, value):
        q = await _calculate_price_for_request(
            _request(query=f"redundancy={value}", headers={"Content-Length": "4000000"}))
        expected = _expected_bytes(4_000_000, swarm_api.DEFAULT_REDUNDANCY_LEVEL)
        assert q["price_usd"] == calculate_bandwidth_price_usd(expected)["price_usd"]

    @pytest.mark.asyncio
    async def test_description_names_the_level_and_the_priced_bytes(self, above_floor):
        q = await _calculate_price_for_request(
            _request(query="redundancy=4", headers={"Content-Length": "4000000"}))
        assert "paranoid" in q["description"]
        assert str(_expected_bytes(4_000_000, 4)) in q["description"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("level", [0, 4])
    async def test_defaults_keep_the_largest_upload_at_the_floor(self, monkeypatch, level):
        """At the default settings the factor changes no price (the issue's premise)."""
        for name in ("X402_MIN_PRICE_USD", "X402_BANDWIDTH_USD_PER_GB", "X402_MARKUP_PERCENT", "MAX_UPLOAD_SIZE_MB"):
            monkeypatch.setattr(settings, name, type(settings).model_fields[name].default)
        cap = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
        q = await _calculate_price_for_request(
            _request(query=f"redundancy={level}", headers={"Content-Length": str(cap)}))
        assert q["price_usd"] == settings.X402_MIN_PRICE_USD
