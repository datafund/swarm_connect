# tests/tools/swarm_stamp.py
"""
Pure-Python helpers to produce a REAL, node-signed postage stamp for a chunk so the
chunk-forwarding happy path can be exercised end-to-end against a local Bee node.

No batch owner private key is required: the Bee node signs a presigned stamp
("envelope") for a given chunk address using a batch it owns, via POST
/envelope/{address}. We compute the chunk address (Swarm BMT), fetch the envelope,
and marshal it into the 113-byte `Swarm-Postage-Stamp` wire format the gateway
forwards verbatim.

Stamp wire format (ethersphere/bee pkg/postage/stamp.go):
    batchID[32] | index[8] | timestamp[8] | signature[65]  = 113 bytes

Chunk address:
    keccak256( span[8 LE] || bmt_root(payload) )
where bmt_root is the binary-Merkle (pairwise keccak256) root over the payload
zero-padded to 4096 bytes, taken as 128 raw 32-byte segments.
"""
from __future__ import annotations

import requests
from eth_hash.auto import keccak

SEGMENT_SIZE = 32
CHUNK_SIZE = 4096
MAX_PAYLOAD = 4096


def bmt_root(payload: bytes) -> bytes:
    """Binary-Merkle root of a payload (zero-padded to 4096 bytes)."""
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"payload exceeds {MAX_PAYLOAD} bytes")
    data = payload.ljust(CHUNK_SIZE, b"\x00")
    level = [data[i:i + SEGMENT_SIZE] for i in range(0, CHUNK_SIZE, SEGMENT_SIZE)]
    while len(level) > 1:
        level = [keccak(level[i] + level[i + 1]) for i in range(0, len(level), 2)]
    return level[0]


def make_chunk(payload: bytes) -> bytes:
    """Build a chunk: 8-byte little-endian span + payload."""
    span = len(payload).to_bytes(8, "little")
    return span + payload


def chunk_address(chunk: bytes) -> bytes:
    """Compute the Swarm content address of a chunk (span + payload)."""
    span, payload = chunk[:8], chunk[8:]
    return keccak(span + bmt_root(payload))


def get_envelope(bee_url: str, batch_id_hex: str, address_hex: str, timeout: int = 15) -> dict:
    """Ask the Bee node to sign a presigned stamp (envelope) for a chunk address."""
    url = f"{bee_url.rstrip('/')}/envelope/{address_hex}"
    resp = requests.post(url, headers={"Swarm-Postage-Batch-Id": batch_id_hex}, timeout=timeout)
    resp.raise_for_status()
    return resp.json()  # {issuer, index, timestamp, signature} (all hex)


def marshal_stamp(batch_id_hex: str, envelope: dict) -> str:
    """Marshal an envelope into the 113-byte Swarm-Postage-Stamp hex string."""
    batch_id = bytes.fromhex(batch_id_hex[2:] if batch_id_hex.startswith("0x") else batch_id_hex)
    index = bytes.fromhex(envelope["index"])
    timestamp = bytes.fromhex(envelope["timestamp"])
    signature = bytes.fromhex(envelope["signature"])
    assert len(batch_id) == 32, f"batchID must be 32 bytes, got {len(batch_id)}"
    assert len(index) == 8, f"index must be 8 bytes, got {len(index)}"
    assert len(timestamp) == 8, f"timestamp must be 8 bytes, got {len(timestamp)}"
    assert len(signature) == 65, f"signature must be 65 bytes, got {len(signature)}"
    return (batch_id + index + timestamp + signature).hex()


def stamp_chunk(bee_url: str, batch_id_hex: str, payload: bytes) -> tuple[bytes, str, str]:
    """End-to-end: build a chunk, get a node-signed envelope, marshal the stamp.

    Returns (chunk_bytes, marshaled_stamp_hex, chunk_address_hex).
    """
    chunk = make_chunk(payload)
    addr_hex = chunk_address(chunk).hex()
    envelope = get_envelope(bee_url, batch_id_hex, addr_hex)
    stamp_hex = marshal_stamp(batch_id_hex, envelope)
    return chunk, stamp_hex, addr_hex
