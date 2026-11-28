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


# --- #693: path traversal and the exact allow-list -------------------------

import asyncio  # noqa: E402

BATCH = "36b7efd913ca4cf880b8eeac5093fa27b0825906c600685b6abdd6566e6cfe8f"
PEER = "ab" * 32


def _proxy(client, path, bee=None, headers=None, base="http://bee.local/"):
    """GET /api/v1/debug/bee/<path> as sent (no client-side normalisation of
    percent-encoding), returning (response, mocked Bee client)."""
    bee = bee or _mock_bee()
    ms = _settings([ADDR])
    ms.SWARM_BEE_API_URL = base
    with patch("app.api.endpoints.debug.settings", ms):
        with patch("app.api.endpoints.debug.get_client", return_value=bee):
            r = client.get("/api/v1/debug/bee/" + path,
                           headers=_headers(KEY) if headers is None else headers)
    return r, bee


# Every form from the issue and from the triage on a local gateway, where each
# reached a Bee endpoint outside the allow-list before the fix.
BYPASSES = [
    "topology/%2e%2e/keys",
    "topology/%2E%2E/balances",
    "topology/%2e%2e/accounting",
    "%2e%2e/keys",
    "stamps/%2e%2e%2ftransactions",
    "wallet/..%2f..%2fpins",
    "topology/%2e%2e/%2e%2e/admin",
    "topology/%2e/settlements",
    "topology/%252e%252e/keys",       # decodes once more to ..
    "topology%252f..%252fkeys",       # slash encoded twice
    "topology/%25",                   # any remaining % is a second encoding
    "topology%5c..%5ckeys",           # backslash
    "topology/%00",
    "topology/%09",
    "topology/%0a",
    "topology/%7f",
    "topology//x",
    "%2ftopology",                    # leading empty segment
    "topology/",                      # trailing slash: empty segment
]


@pytest.mark.parametrize("path", BYPASSES)
def test_bypass_forms_refused_before_bee(client, path):
    r, bee = _proxy(client, path)
    assert r.status_code == 403, (path, r.text)
    assert r.json()["detail"]["code"] == "DEBUG_PATH_INVALID"
    assert r.json()["code"] == "DEBUG_PATH_INVALID"
    bee.get.assert_not_called()


def _raw_asgi_get(path: str, headers: dict):
    """Send a GET straight to the ASGI app with `path` as is, so literal '..'
    segments reach the server (an HTTP client would resolve them first, as
    curl does without --path-as-is)."""
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(),
        "root_path": "", "query_string": b"", "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]
        + [(b"host", b"testserver")],
    }
    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], body


@pytest.mark.parametrize("path", ["topology/../settlements", "topology/../../admin", "../keys", "stamps/./x"])
def test_literal_dot_segments_refused_before_bee(path):
    bee = _mock_bee()
    with patch("app.api.endpoints.debug.settings", _settings([ADDR])):
        with patch("app.api.endpoints.debug.get_client", return_value=bee):
            code, body = _raw_asgi_get("/api/v1/debug/bee/" + path, _headers(KEY))
    assert code == 403, (path, body)
    assert b"DEBUG_PATH_INVALID" in body
    bee.get.assert_not_called()


ALLOWED = [
    "topology", "addresses", "health", "readiness", "peers", "chainstate",
    "reservestate", "redistributionstate", "node", "wallet",
    "status", "status/peers", "status/neighborhoods",
    "stamps", f"stamps/{BATCH}", f"stamps/{BATCH.upper()}/buckets",
    "batches", f"batches/{BATCH}",
    "chequebook/address", "chequebook/balance", "chequebook/cheque",
    f"chequebook/cheque/{PEER}", f"chequebook/cashout/{PEER}",
]


@pytest.mark.parametrize("path", ALLOWED)
def test_allowed_paths_reach_exact_bee_url(client, path):
    r, bee = _proxy(client, path)
    assert r.status_code == 200, (path, r.text)
    bee.get.assert_awaited_once()
    assert bee.get.await_args.args[0] == f"http://bee.local/{path}"


@pytest.mark.parametrize("base", ["http://bee.local/prefix/", "http://bee.local/prefix"])
def test_bee_base_path_is_kept(client, base):
    r, bee = _proxy(client, "status/peers", base=base)
    assert r.status_code == 200
    assert bee.get.await_args.args[0] == "http://bee.local/prefix/status/peers"


SUBPATH_REFUSED = [
    "topology/x", "wallet/withdraw", "wallet/withdraw/bzz", "chequebook",
    "chequebook/deposit", "chequebook/cheque/xyz", f"chequebook/cashout/{PEER}/x",
    "stamps/" + BATCH[:-1], "stamps/" + "g" * 64, f"stamps/{BATCH}/buckets/x",
    f"stamps/{BATCH}/topup", "status/x", "batches/x", "peers/" + PEER,
    "accounting", "balances", "settlements", "transactions", "pins", "keys",
]


@pytest.mark.parametrize("path", SUBPATH_REFUSED)
def test_subpath_rules(client, path):
    r, bee = _proxy(client, path)
    assert r.status_code == 403, (path, r.text)
    assert r.json()["detail"]["code"] == "PATH_NOT_ALLOWED"
    assert r.json()["detail"]["allowed"] == sorted({
        "topology", "addresses", "health", "readiness", "peers", "chainstate",
        "reservestate", "redistributionstate", "status", "node", "stamps",
        "batches", "chequebook", "wallet"})
    bee.get.assert_not_called()


def test_path_not_allowed_shape_unchanged(client):
    r, _ = _proxy(client, "pinning")
    assert r.json()["detail"] == {
        "code": "PATH_NOT_ALLOWED",
        "message": "'pinning' is not a permitted debug path",
        "allowed": ["addresses", "batches", "chainstate", "chequebook", "health", "node",
                    "peers", "readiness", "redistributionstate", "reservestate", "stamps",
                    "status", "topology", "wallet"],
    }


@pytest.mark.parametrize("path", ["topology?x=1", "stamps?all=true", f"stamps/{BATCH}?a=b"])
def test_query_string_refused(client, path):
    r, bee = _proxy(client, path)
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "DEBUG_QUERY_NOT_ALLOWED"
    bee.get.assert_not_called()


@pytest.mark.parametrize("path", ["topology/%2e%2e/accounting", "wallet/..%2f..%2fpins", "topology?x=1"])
def test_unsigned_bypass_still_401(client, path):
    r, bee = _proxy(client, path, headers={})
    assert r.status_code == 401
    bee.get.assert_not_called()
