"""The v1 goldens, sent through the gateway's own facilitator client (#604).

tests/test_x402_v1_golden.py drives the gateway through x402 1.0.0's
FacilitatorClient, so it pins what the gateway hands the client but not the
bytes the gateway's own client puts on the wire. This runs the same capture
with app.x402.facilitator_client.FacilitatorClient (the one production uses)
and compares it with the same golden files, read-only. Never regenerate them.
"""
import httpx2
import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient

from app.x402.facilitator_client import FacilitatorClient
from app.x402.ratelimit import reset_rate_limiter
from tests import test_x402_v1_golden as golden


def _httpx2(response):
    """The golden stub answers with an httpx.Response; the client is on httpx2."""
    return httpx2.Response(response.status_code, headers=list(response.headers.raw), content=response.content)


@pytest.fixture
def own_gateway(monkeypatch):
    stack = []

    def build(network, free_tier=False):
        golden.configure(monkeypatch, network, free_tier=free_tier)
        reset_rate_limiter()
        fac = golden.Facilitator()
        client = FacilitatorClient(config={"url": golden.FACILITATOR_URL},
                                   transport=httpx2.MockTransport(lambda request: _httpx2(fac.handler(request))))
        p1 = patch("app.x402.dependency._calculate_price_for_request",
                   new=AsyncMock(return_value={"price_usd": 0.02, "description": "Golden operation"}))
        p2 = patch("app.x402.dependency._get_facilitator_client", return_value=client)
        p1.start()
        p2.start()
        stack.extend([p1, p2])
        return TestClient(golden._app(client), raise_server_exceptions=False), fac

    yield build
    for p in reversed(stack):
        p.stop()
    reset_rate_limiter()


@pytest.mark.parametrize("network", golden.NETWORKS)
def test_own_client_sends_the_v1_golden_bytes(own_gateway, monkeypatch, network):
    assert not golden.REGENERATE, "the goldens are regenerated only by test_x402_v1_golden.py"
    got = golden.run_capture(own_gateway, monkeypatch, network, network)
    assert [c["path"] for c in got["paid_settle_before_delivery_requests"]] == ["verify", "settle"]
    assert [c["path"] for c in got["paid_settle_after_response_requests"]] == ["verify", "settle"]
