"""The API docs say the gateway is alpha software (#683), and on which network (#687)."""
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app


def test_openapi_title_says_alpha():
    assert app.title == f"{settings.PROJECT_NAME} (alpha)"


def test_docs_page_title_says_alpha():
    response = TestClient(app).get("/docs")
    assert response.status_code == 200
    assert f"<title>{settings.PROJECT_NAME} (alpha) - Swagger UI</title>" in response.text


CONTACT = "info@datafund.io"
NOTICE = "https://github.com/datafund/swarm_connect/blob/main/ALPHA_NOTICE.md"


# --- Swagger description ---

def _description(monkeypatch, network, enabled=True, contact="", terms=""):
    from app.main import build_api_description
    monkeypatch.setattr(settings, "X402_ENABLED", enabled)
    monkeypatch.setattr(settings, "X402_NETWORK", network)
    monkeypatch.setattr(settings, "SUPPORT_CONTACT", contact)
    monkeypatch.setattr(settings, "TERMS_URL", terms)
    return build_api_description()


@pytest.mark.parametrize("network", ["base", "eip155:8453"])
def test_description_on_mainnet_warns_of_real_money(monkeypatch, network):
    text = _description(monkeypatch, network)
    assert "Alpha software" in text
    assert "real USDC on Base mainnet" in text
    assert "no guarantees" in text
    assert "refunds should not be expected" in text
    assert "test USDC" not in text


@pytest.mark.parametrize("network", ["base-sepolia", "eip155:84532"])
def test_description_on_testnet_says_no_real_value(monkeypatch, network):
    text = _description(monkeypatch, network)
    assert "Alpha software" in text
    assert "Base Sepolia test USDC" in text
    assert "no real value" in text
    assert "real USDC on Base mainnet" not in text


def test_description_without_x402_mentions_no_payments(monkeypatch):
    text = _description(monkeypatch, "base", enabled=False)
    assert "Alpha software" in text
    assert "USDC" not in text


def test_description_links_only_what_is_set(monkeypatch):
    bare = _description(monkeypatch, "base")
    assert CONTACT not in bare and NOTICE not in bare
    assert "None" not in bare
    full = _description(monkeypatch, "base", contact=CONTACT, terms=NOTICE)
    assert CONTACT in full and NOTICE in full


def test_app_uses_the_description():
    from app.main import app, build_api_description
    assert app.description == build_api_description()
    spec = TestClient(app).get(f"{settings.API_V1_STR}/openapi.json").json()
    assert spec["info"]["description"] == app.description
    assert spec["info"]["title"] == f"{settings.PROJECT_NAME} (alpha)"
