"""POST /api/v1/pool/check must not be reachable by anyone who can resolve the host.

The endpoint calls check_and_replenish(), which buys postage batches with the
gateway's own Gnosis funds. It had no authentication and no payment gate, so any
caller could spend them — against a production wallet holding real BZZ (#292).
It also awaited the whole check, and a purchase takes about sixteen seconds, so
repeated calls tied up workers.

These cover both halves, plus the part that is easy to get wrong: a signature
authorising Bee diagnostics must NOT also authorise spending.
"""
import time

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.services.signed_auth import DEBUG_PREFIX, POOL_CHECK_PREFIX

client = TestClient(app)

ADMIN = Account.create()
OUTSIDER = Account.create()


def _sign(account, prefix, ts=None):
    ts = ts if ts is not None else int(time.time())
    sig = account.sign_message(encode_defunct(text=f"{prefix}{ts}"))
    return {"X-Debug-Timestamp": str(ts), "X-Debug-Signature": sig.signature.hex()}


@pytest.fixture
def pool_admin(monkeypatch):
    monkeypatch.setattr(settings, "STAMP_POOL_ENABLED", True)
    monkeypatch.setattr(settings, "POOL_ADMIN_ADDRESSES", ADMIN.address)
    return ADMIN


@pytest.fixture(autouse=True)
def never_actually_spend(monkeypatch):
    """Guarantee no test here can reach the purchasing code."""
    called = []

    async def _fake():
        called.append(True)
        return {"checked_at": "now", "stamps_purchased": 0, "errors": []}

    from app.services import stamp_pool
    monkeypatch.setattr(stamp_pool.stamp_pool_manager, "check_and_replenish", _fake)
    return called


class TestUnauthorizedCannotSpend:
    def test_no_signature_is_refused(self, pool_admin, never_actually_spend):
        resp = client.post("/api/v1/pool/check")
        assert resp.status_code == 401, (
            f"unauthenticated caller got {resp.status_code} — this endpoint spends BZZ"
        )
        assert not never_actually_spend, "maintenance ran for an unauthenticated caller"

    def test_signature_from_unknown_address_is_refused(self, pool_admin, never_actually_spend):
        resp = client.post("/api/v1/pool/check", headers=_sign(OUTSIDER, POOL_CHECK_PREFIX))
        assert resp.status_code == 403
        assert not never_actually_spend

    def test_garbage_signature_is_refused(self, pool_admin, never_actually_spend):
        resp = client.post("/api/v1/pool/check", headers={
            "X-Debug-Timestamp": str(int(time.time())),
            "X-Debug-Signature": "0x" + "11" * 65,
        })
        assert resp.status_code == 401
        assert not never_actually_spend

    def test_stale_timestamp_is_refused(self, pool_admin, never_actually_spend):
        old = int(time.time()) - settings.DEBUG_SIG_MAX_AGE_SECONDS - 60
        resp = client.post("/api/v1/pool/check", headers=_sign(ADMIN, POOL_CHECK_PREFIX, ts=old))
        assert resp.status_code == 401, "a stale signature was accepted — replay is possible"
        assert not never_actually_spend

    def test_future_timestamp_is_refused(self, pool_admin, never_actually_spend):
        """Otherwise a signature could be minted now and held until convenient."""
        future = int(time.time()) + settings.DEBUG_SIG_MAX_AGE_SECONDS + 60
        resp = client.post("/api/v1/pool/check", headers=_sign(ADMIN, POOL_CHECK_PREFIX, ts=future))
        assert resp.status_code == 401
        assert not never_actually_spend


class TestPrivilegeSeparation:
    """A signature for one privilege must not grant another."""

    def test_debug_signature_does_not_authorize_spending(self, pool_admin, never_actually_spend, monkeypatch):
        """The whole reason the two use different message prefixes.

        The admin address is also allow-listed for diagnostics here, so the only
        thing standing between a captured debug signature and a stamp purchase
        is the prefix.
        """
        monkeypatch.setattr(settings, "DEBUG_ALLOWED_ADDRESSES", ADMIN.address)
        resp = client.post("/api/v1/pool/check", headers=_sign(ADMIN, DEBUG_PREFIX))
        # 403, not 401: recovering a signature against a message it was not made
        # over yields a valid-looking but different address, which then fails the
        # allow-list. Either code is a refusal; what matters is that spending did
        # not happen.
        assert resp.status_code in (401, 403), (
            "a diagnostics signature authorised spending — the prefixes are not separating"
        )
        assert not never_actually_spend

    def test_pool_signature_does_not_authorize_diagnostics(self, monkeypatch):
        monkeypatch.setattr(settings, "DEBUG_ALLOWED_ADDRESSES", ADMIN.address)
        monkeypatch.setattr(settings, "POOL_ADMIN_ADDRESSES", ADMIN.address)
        resp = client.get("/api/v1/debug/bee/topology", headers=_sign(ADMIN, POOL_CHECK_PREFIX))
        assert resp.status_code in (401, 403)


class TestDisabledByDefault:
    def test_empty_allow_list_hides_the_endpoint(self, monkeypatch, never_actually_spend):
        """404, not 401 — an unconfigured gateway should not advertise the door.

        This is what closes the exposure on a deployment that has not been
        configured yet, which at the time of writing includes production.
        """
        monkeypatch.setattr(settings, "STAMP_POOL_ENABLED", True)
        monkeypatch.setattr(settings, "POOL_ADMIN_ADDRESSES", "")
        resp = client.post("/api/v1/pool/check", headers=_sign(ADMIN, POOL_CHECK_PREFIX))
        assert resp.status_code == 404
        assert not never_actually_spend


class TestAuthorizedCaller:
    def test_allow_listed_signature_is_accepted(self, pool_admin, never_actually_spend):
        resp = client.post("/api/v1/pool/check", headers=_sign(ADMIN, POOL_CHECK_PREFIX))
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert "scheduled_at" in body
        assert "pool/status" in body["message"], "the response should say where to find the result"

    def test_response_is_sent_before_maintenance_finishes(self, pool_admin, monkeypatch):
        """The second half of #292: awaiting the check tied up the caller.

        A purchase takes ~16s and the reserve is five, so a caller could hold a
        connection for over a minute.

        Asserted at the ASGI level rather than by wall-clock through TestClient.
        TestClient drives the full ASGI cycle, and Starlette runs background
        tasks inside it — after the response is sent, but before the client call
        returns. So a stopwatch around client.post() measures the background work
        too and would fail even though the real client was released. What
        actually matters is the ORDER: the response body must be emitted before
        maintenance completes.
        """
        import asyncio

        order = []

        async def _slow():
            await asyncio.sleep(0.2)
            order.append("maintenance-finished")
            return {}

        from app.services import stamp_pool
        monkeypatch.setattr(stamp_pool.stamp_pool_manager, "check_and_replenish", _slow)

        headers = _sign(ADMIN, POOL_CHECK_PREFIX)
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "POST", "path": "/api/v1/pool/check", "raw_path": b"/api/v1/pool/check",
            "query_string": b"", "root_path": "", "scheme": "http",
            "client": ("127.0.0.1", 1234), "server": ("testserver", 80),
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        }

        status_seen = {}

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            if message["type"] == "http.response.start":
                status_seen["code"] = message["status"]
            elif message["type"] == "http.response.body" and not message.get("more_body"):
                order.append("response-sent")

        asyncio.run(app(scope, receive, send))

        assert status_seen.get("code") == 202, status_seen
        assert order == ["response-sent", "maintenance-finished"], (
            f"the handler awaited maintenance before responding: {order}"
        )

    def test_pool_disabled_is_404_regardless_of_signature(self, monkeypatch, never_actually_spend):
        monkeypatch.setattr(settings, "STAMP_POOL_ENABLED", False)
        monkeypatch.setattr(settings, "POOL_ADMIN_ADDRESSES", ADMIN.address)
        resp = client.post("/api/v1/pool/check", headers=_sign(ADMIN, POOL_CHECK_PREFIX))
        assert resp.status_code == 404
        assert not never_actually_spend
