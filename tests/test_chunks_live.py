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

from tests.tools.swarm_stamp import chunk_address, make_chunk, sign_stamp, stamp_chunk

GATEWAY_URL = os.environ.get("CHUNK_LIVE_GATEWAY_URL")
# Bee node used to mint real (node-signed) stamps via its /envelope endpoint.
BEE_URL = os.environ.get("CHUNK_LIVE_BEE_URL", "http://localhost:1633")

pytestmark = pytest.mark.skipif(
    not GATEWAY_URL,
    reason="set CHUNK_LIVE_GATEWAY_URL to run live chunk tests against a running gateway",
)

# 226 hex chars = a well-formed 113-byte marshaled stamp shape (not a real signature).
WELL_FORMED_STAMP = "ab" * 113


def _pick_usable_batch():
    """Return a usable batch ID on the Bee node, or None (skip)."""
    try:
        resp = requests.get(f"{BEE_URL}/stamps", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        stamps = data.get("stamps", data) if isinstance(data, dict) else data
        for s in stamps:
            if s.get("usable") and s.get("batchID"):
                return s["batchID"]
    except Exception:
        return None
    return None


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


def test_real_signed_chunk_roundtrip():
    """Full happy path: a real node-signed chunk is accepted and retrievable.

    Mints a valid stamp via the Bee node's /envelope endpoint (no private key
    needed), uploads the chunk through the gateway, and confirms Bee accepts it
    (201), the returned reference equals the locally-computed chunk address, and
    the chunk reads back byte-for-byte.
    """
    batch_id = _pick_usable_batch()
    if not batch_id:
        pytest.skip(f"no usable postage batch on Bee node at {BEE_URL}")

    payload = b"chunk-forwarding-live-test-" + os.urandom(8).hex().encode()
    chunk, stamp_hex, addr_hex = stamp_chunk(BEE_URL, batch_id, payload)

    # Sanity: our BMT address matches a fresh recomputation.
    assert chunk_address(make_chunk(payload)).hex() == addr_hex

    r = requests.post(
        _chunks_url() + "/", data=chunk,
        headers={
            "Swarm-Postage-Stamp": stamp_hex,
            "Content-Type": "application/octet-stream",
        },
        timeout=30,
    )
    assert r.status_code == 201, r.text
    reference = r.json()["reference"]
    assert reference.lower() == addr_hex.lower()

    got = requests.get(f"{BEE_URL}/chunks/{reference}", timeout=15)
    assert got.status_code == 200
    assert got.content == chunk


def test_owner_signed_chunk_through_gateway():
    """Full self-custody flow: the batch OWNER signs its own stamp (client-side,
    EIP-191) and pushes the chunk through the gateway to a same-network Bee node.

    Needs CHUNK_LIVE_OWNER_KEY (owner private key) and CHUNK_LIVE_BATCH_ID (a batch
    owned by that key on the network BEE_URL/the gateway is connected to).
    """
    owner_key = os.environ.get("CHUNK_LIVE_OWNER_KEY")
    batch_id = os.environ.get("CHUNK_LIVE_BATCH_ID")
    if not (owner_key and batch_id):
        pytest.skip("set CHUNK_LIVE_OWNER_KEY and CHUNK_LIVE_BATCH_ID for the self-custody test")

    payload = b"self-custody-live-" + os.urandom(8).hex().encode()
    chunk = make_chunk(payload)
    stamp_hex, addr_hex, signer = sign_stamp(owner_key, chunk, batch_id)

    r = requests.post(
        _chunks_url() + "/", data=chunk,
        headers={"Swarm-Postage-Stamp": stamp_hex, "Content-Type": "application/octet-stream"},
        timeout=30,
    )
    assert r.status_code == 201, r.text
    assert r.json()["reference"].lower() == addr_hex.lower()

    got = requests.get(f"{BEE_URL}/chunks/{addr_hex}", timeout=20)
    assert got.status_code == 200
    assert got.content == chunk
