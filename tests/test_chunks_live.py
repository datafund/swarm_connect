# tests/test_chunks_live.py
"""
Live end-to-end tests for chunk forwarding against a RUNNING gateway + Bee node.

Opt-in: set CHUNK_LIVE_GATEWAY_URL to the base URL of a gateway started with
CHUNK_UPLOAD_ENABLED=true and X402_ENABLED=false (pure-forwarder mode), e.g.

    SWARM_BEE_API_URL=http://localhost:1633 CHUNK_UPLOAD_ENABLED=true \
        X402_ENABLED=false PORT=8011 python run.py

    CHUNK_LIVE_GATEWAY_URL=http://127.0.0.1:8011 \
        python -m pytest tests/test_chunks_live.py -v

These tests send real chunks through the gateway to the Bee node and assert the
gateway validates input, forwards to Bee, and surfaces Bee's verdict.

NOTE on the signed happy path: a chunk that Bee *accepts* (201 + retrievable)
requires a marshaled postage stamp signed by the batch owner key. On a local
node all batches are node-owned and that key is not exposed, so the valid-stamp
happy path cannot be exercised here — it is the client-stamping path validated by
the Flow B spike (#226). These tests therefore prove validation + real forwarding
+ error surfacing; a well-formed-but-unsigned stamp is expected to be rejected by
Bee (502), which confirms the chunk truly reached the node.
"""
import os

import pytest
import requests

GATEWAY_URL = os.environ.get("CHUNK_LIVE_GATEWAY_URL")

pytestmark = pytest.mark.skipif(
    not GATEWAY_URL,
    reason="set CHUNK_LIVE_GATEWAY_URL to run live chunk tests against a running gateway",
)

# 226 hex chars = a well-formed 113-byte marshaled stamp shape (not a real signature).
WELL_FORMED_STAMP = "ab" * 113


def _chunks_url():
    return f"{GATEWAY_URL}/api/v1/chunks"


def test_gateway_healthy():
    r = requests.get(f"{GATEWAY_URL}/health", timeout=10)
    assert r.status_code == 200
    assert r.json().get("status") == "ok"


def test_missing_stamp_returns_400():
    r = requests.post(_chunks_url() + "/", data=b"hello", timeout=15)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "MISSING_STAMP"


def test_invalid_stamp_returns_400():
    r = requests.post(
        _chunks_url() + "/", data=b"hello",
        headers={"Swarm-Postage-Stamp": "abcd"}, timeout=15,
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "INVALID_STAMP"


def test_empty_body_returns_400():
    r = requests.post(
        _chunks_url() + "/", data=b"",
        headers={"Swarm-Postage-Stamp": WELL_FORMED_STAMP}, timeout=15,
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "EMPTY_CHUNK"


def test_oversize_body_returns_413():
    r = requests.post(
        _chunks_url() + "/", data=b"x" * 5000,
        headers={"Swarm-Postage-Stamp": WELL_FORMED_STAMP}, timeout=15,
    )
    assert r.status_code == 413
    assert r.json()["detail"]["code"] == "CHUNK_TOO_LARGE"


def test_wellformed_stamp_is_forwarded_to_bee():
    """A well-formed stamp passes gateway validation and is forwarded to Bee.

    Bee rejects the unsigned stamp (-> 502 from the gateway). Either way the
    response is NOT a gateway-side 400, proving the chunk reached Bee.
    """
    r = requests.post(
        _chunks_url() + "/", data=b"hello-swarm-chunk",
        headers={"Swarm-Postage-Stamp": WELL_FORMED_STAMP}, timeout=30,
    )
    assert r.status_code in (201, 502)
    if r.status_code == 201:
        assert "reference" in r.json()


def test_credit_topup_disabled_without_x402():
    """In pure-forwarder mode (X402 off) the credit top-up is unavailable."""
    r = requests.post(_chunks_url() + "/credit?mb=100", timeout=15)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "BILLING_DISABLED"
