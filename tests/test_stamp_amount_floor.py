"""Tests for the stamp amount floor and Bee error propagation (issue #258)."""
import httpx
import pytest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import app
from app.services import swarm_api


BEE_MIN_VALIDITY_BLOCKS = 17280  # what Bee enforces: 24h at 720 blocks/hour


class TestAmountClearsBeeFloor:
    """Bee rejects an amount that merely equals its minimum-validity floor.

    The floor is currentPrice * minimumValidityBlocks, and minimumValidityBlocks
    is exactly 24 * 720 — so an exact calculation for the documented 24h minimum
    lands on the floor and is always refused.
    """

    @pytest.mark.parametrize("price", [41516, 77610, 1])
    def test_documented_minimum_duration_clears_the_floor(self, price):
        amount = swarm_api.calculate_stamp_amount(24, price)
        assert amount > price * BEE_MIN_VALIDITY_BLOCKS

    @pytest.mark.parametrize("hours", [24, 25, 30, 48, 168])
    def test_all_durations_clear_the_floor(self, hours):
        price = 77610
        amount = swarm_api.calculate_stamp_amount(hours, price)
        assert amount > price * BEE_MIN_VALIDITY_BLOCKS

    def test_amount_still_scales_with_duration(self):
        price = 77610
        a24 = swarm_api.calculate_stamp_amount(24, price)
        a48 = swarm_api.calculate_stamp_amount(48, price)
        assert a48 > a24

    def test_margin_absorbs_realistic_price_drift(self):
        """Price was observed moving 41516 -> 77610 within an hour.

        A calculation made at one price is validated by Bee at another, so a
        modest rise must not push the amount back under the floor.
        """
        calculated = swarm_api.calculate_stamp_amount(24, 77610)
        risen = int(77610 * 1.04)
        assert calculated > risen * BEE_MIN_VALIDITY_BLOCKS


def _http_error(status_code, payload):
    request = httpx.Request("POST", "http://bee:1633/stamps/1/17")
    response = httpx.Response(status_code, json=payload, request=request)
    return httpx.HTTPStatusError("err", request=request, response=response)


class TestBeeErrorIsNotMasked:
    """A refusal by Bee is the caller's problem, not a gateway outage."""

    @pytest.fixture
    def client(self):
        return TestClient(app)

    def test_bee_400_surfaces_as_400_with_its_message(self, client):
        err = _http_error(400, {"code": 400,
                                "message": "insufficient amount for 24h minimum validity"})
        with patch("app.services.swarm_api.get_chainstate",
                   new=AsyncMock(return_value={"currentPrice": "77610"})), \
             patch("app.services.swarm_api.check_sufficient_funds",
                   new=AsyncMock(return_value={"sufficient": True})), \
             patch("app.services.swarm_api.purchase_postage_stamp",
                   new=AsyncMock(side_effect=err)):
            r = client.post("/api/v1/stamps/", json={"size": "small", "duration_hours": 24})

        assert r.status_code == 400
        assert "insufficient amount for 24h minimum validity" in r.json()["detail"]
        assert "may be unavailable" not in r.json()["detail"]

    def test_connection_failure_still_reports_502(self, client):
        """No response to read means the node really is unreachable."""
        request = httpx.Request("POST", "http://bee:1633/stamps/1/17")
        err = httpx.ConnectError("connection refused", request=request)
        with patch("app.services.swarm_api.get_chainstate",
                   new=AsyncMock(return_value={"currentPrice": "77610"})), \
             patch("app.services.swarm_api.check_sufficient_funds",
                   new=AsyncMock(return_value={"sufficient": True})), \
             patch("app.services.swarm_api.purchase_postage_stamp",
                   new=AsyncMock(side_effect=err)):
            r = client.post("/api/v1/stamps/", json={"size": "small"})

        assert r.status_code == 502
        assert "may be unavailable" in r.json()["detail"]

    def test_bee_500_still_reports_502(self, client):
        """A server-side failure in Bee is not the caller's fault."""
        err = _http_error(500, {"code": 500, "message": "internal error"})
        with patch("app.services.swarm_api.get_chainstate",
                   new=AsyncMock(return_value={"currentPrice": "77610"})), \
             patch("app.services.swarm_api.check_sufficient_funds",
                   new=AsyncMock(return_value={"sufficient": True})), \
             patch("app.services.swarm_api.purchase_postage_stamp",
                   new=AsyncMock(side_effect=err)):
            r = client.post("/api/v1/stamps/", json={"size": "small"})

        assert r.status_code == 502


class TestExtendPathBehavesLikePurchase:
    """The extend path shares the boundary condition and must share the fix.

    Both paths call calculate_stamp_amount() and both talk to Bee, so a caller
    extending a stamp can hit the same minimum-validity refusal — and would have
    been told the node was unavailable.
    """

    @pytest.fixture
    def client(self):
        return TestClient(app)

    def test_bee_400_on_extend_surfaces_as_400(self, client):
        err = _http_error(400, {"code": 400,
                                "message": "insufficient amount for 24h minimum validity"})
        existing = [{"batchID": "a" * 64, "depth": 17, "batchTTL": 86400}]
        with patch("app.services.swarm_api.get_all_stamps_processed",
                   new=AsyncMock(return_value=existing)), \
             patch("app.services.swarm_api.get_chainstate",
                   new=AsyncMock(return_value={"currentPrice": "77610",
                                               "minimumValidityBlocks": 17280})), \
             patch("app.services.swarm_api.check_sufficient_funds",
                   new=AsyncMock(return_value={"sufficient": True})), \
             patch("app.services.swarm_api.extend_postage_stamp",
                   new=AsyncMock(side_effect=err)):
            r = client.patch("/api/v1/stamps/" + "a" * 64 + "/extend",
                             json={"duration_hours": 24})

        assert r.status_code == 400
        assert "insufficient amount" in r.json()["detail"]
        assert "may be unavailable" not in r.json()["detail"]

    def test_connection_failure_on_extend_still_reports_502(self, client):
        request = httpx.Request("PATCH", "http://bee:1633/stamps/topup/x/1")
        err = httpx.ConnectError("connection refused", request=request)
        existing = [{"batchID": "a" * 64, "depth": 17, "batchTTL": 86400}]
        with patch("app.services.swarm_api.get_all_stamps_processed",
                   new=AsyncMock(return_value=existing)), \
             patch("app.services.swarm_api.get_chainstate",
                   new=AsyncMock(return_value={"currentPrice": "77610",
                                               "minimumValidityBlocks": 17280})), \
             patch("app.services.swarm_api.check_sufficient_funds",
                   new=AsyncMock(return_value={"sufficient": True})), \
             patch("app.services.swarm_api.extend_postage_stamp",
                   new=AsyncMock(side_effect=err)):
            r = client.patch("/api/v1/stamps/" + "a" * 64 + "/extend",
                             json={"duration_hours": 24})

        assert r.status_code == 502
        assert "may be unavailable" in r.json()["detail"]
