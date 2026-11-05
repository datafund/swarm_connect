"""/health reports the accepted x402 versions; /pricing adds `amount` (#602).

Clients and operators can see which x402 versions the gateway accepts without
sending a payment. /pricing gains `amount`, the x402 v2 name of the price, next
to `max_amount_required`, which stays (deprecated) until v1 is removed (D8 in
#596). Every field the two endpoints returned before is unchanged.
"""
import json

import pytest

from app.core.config import settings
from app.x402.payment_headers import accepted_x402_versions
from tests.test_pricing_endpoint import client, x402_on  # noqa: F401  (fixtures)

QUOTE_KEYS_BEFORE = ["method", "path", "price_usd", "max_amount_required", "description"]
PRICING_KEYS_BEFORE = {"x402_enabled", "currency", "network", "asset", "pay_to",
                       "min_price_usd", "free_tier", "quotes"}
HEALTH_X402_KEYS_BEFORE = {"enabled", "free_tier", "base_wallet", "bee_gnosis_wallet",
                           "warnings", "errors"}


def test_only_v1_is_accepted_by_default():
    # X402_PROTOCOL_VERSIONS replaced the ACCEPTED_X402_VERSIONS constant (#607).
    assert settings.X402_PROTOCOL_VERSIONS == "1"
    assert accepted_x402_versions() == (1,)


def test_pricing_quotes_add_amount_next_to_max_amount_required(client):  # noqa: F811
    body = client.get("/api/v1/pricing?size=medium").json()
    assert set(body) == PRICING_KEYS_BEFORE
    assert body["quotes"]
    for quote in body["quotes"].values():
        keys = list(quote)
        assert keys == QUOTE_KEYS_BEFORE[:4] + ["amount"] + QUOTE_KEYS_BEFORE[4:]
        assert quote["amount"] == quote["max_amount_required"]
        assert quote["amount"] == str(int(quote["price_usd"] * 1_000_000))


def test_pricing_amount_equals_what_the_402_asks(client):  # noqa: F811
    quote = client.get("/api/v1/pricing").json()["quotes"]["stamp_purchase"]
    r = client.post("/api/v1/stamps/", json={})
    assert r.status_code == 402
    assert quote["amount"] == r.json()["detail"]["accepts"][0]["maxAmountRequired"]


@pytest.fixture
def health(monkeypatch):
    from app.main import build_health_response

    def build(enabled):
        monkeypatch.setattr(settings, "X402_ENABLED", enabled)
        return build_health_response(None)
    return build


def test_health_reports_the_accepted_versions(health):
    x402 = health(True)["x402"]
    assert x402["versions"] == [1]
    assert set(x402) == HEALTH_X402_KEYS_BEFORE | {"versions"}


@pytest.mark.parametrize("value,expected", [("1,2", [1, 2]), ("2", [2]), ("1", [1])])
def test_health_versions_come_from_the_setting(health, monkeypatch, value, expected):
    monkeypatch.setattr(settings, "X402_PROTOCOL_VERSIONS", value)
    assert health(True)["x402"]["versions"] == expected


def test_health_without_x402_is_unchanged(health):
    assert "x402" not in health(False)


def test_openapi_shows_the_new_fields():
    from app.main import app
    paths = app.openapi()["paths"]
    pricing = json.dumps(paths["/api/v1/pricing"]["get"])
    assert '"amount"' in pricing
    assert "deprecated" in pricing and "max_amount_required" in pricing
    example = paths["/api/v1/pricing"]["get"]["responses"]["200"]["content"]["application/json"]["example"]
    quote = example["quotes"]["stamp_purchase"]
    assert quote["amount"] == quote["max_amount_required"]
    root = paths["/"]["get"]
    assert "x402.versions" in root["description"]
    assert root["responses"]["200"]["content"]["application/json"]["example"]["x402"]["versions"] == [1]
