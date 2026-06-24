# tests/test_debug_proxy.py
"""
Tests for the signature-gated read-only Bee diagnostics proxy.
"""
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

from app.main import app

KEY = "0x" + "22" * 32
ADDR = Account.from_key(KEY).address
OTHER_KEY = "0x" + "33" * 32


@pytest.fixture
def client():
    return TestClient(app)


def _settings(allowed):
    ms = MagicMock()
    ms.get_debug_allowed_addresses.return_value = [a.lower() for a in allowed]
    ms.DEBUG_SIG_MAX_AGE_SECONDS = 300
    ms.SWARM_BEE_API_URL = "http://bee.local/"
    return ms


def _headers(key, ts=None):
    ts = ts if ts is not None else int(time.time())
    signed = Account.sign_message(encode_defunct(text=f"swarm-connect-debug:{ts}"), private_key=key)
    return {"X-Debug-Timestamp": str(ts), "X-Debug-Signature": "0x" + bytes(signed.signature).hex()}


def _mock_bee(body=b'{"depth":4,"connected":137}', status_code=200):
    resp = MagicMock()
    resp.content = body
    resp.status_code = status_code
    resp.headers = {"content-type": "application/json"}
    c = MagicMock()
    c.get = AsyncMock(return_value=resp)
    return c


def test_disabled_when_no_allowlist_returns_404(client):
    with patch("app.api.endpoints.debug.settings", _settings([])):
        r = client.get("/api/v1/debug/bee/topology", headers=_headers(KEY))
    assert r.status_code == 404


def test_missing_signature_401(client):
    with patch("app.api.endpoints.debug.settings", _settings([ADDR])):
        r = client.get("/api/v1/debug/bee/topology")
    assert r.status_code == 401


def test_stale_timestamp_401(client):
    with patch("app.api.endpoints.debug.settings", _settings([ADDR])):
        r = client.get("/api/v1/debug/bee/topology", headers=_headers(KEY, ts=int(time.time()) - 10000))
    assert r.status_code == 401


def test_non_allowlisted_signer_403(client):
    with patch("app.api.endpoints.debug.settings", _settings([ADDR])):
        # signed by a different key than the allow-listed address
        r = client.get("/api/v1/debug/bee/topology", headers=_headers(OTHER_KEY))
    assert r.status_code == 403


def test_disallowed_path_403(client):
    with patch("app.api.endpoints.debug.settings", _settings([ADDR])):
        r = client.get("/api/v1/debug/bee/pinning", headers=_headers(KEY))  # 'pinning' not allow-listed
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "PATH_NOT_ALLOWED"


def test_valid_request_proxies_to_bee(client):
    with patch("app.api.endpoints.debug.settings", _settings([ADDR])):
        with patch("app.api.endpoints.debug.get_client", return_value=_mock_bee()):
            r = client.get("/api/v1/debug/bee/topology", headers=_headers(KEY))
    assert r.status_code == 200
    assert r.json()["connected"] == 137
