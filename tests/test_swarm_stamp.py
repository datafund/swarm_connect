# tests/test_swarm_stamp.py
"""
Hermetic unit tests for the stamp tooling (tests/tools/swarm_stamp.py).

No node/network required — validates BMT chunk addressing and that client-side
sign_stamp produces a correctly-formatted, EIP-191-signed postage stamp that
recovers to the owner (the format Bee's crypto.Recover validates).
"""
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_hash.auto import keccak

from tests.tools.swarm_stamp import chunk_address, make_chunk, sign_stamp

OWNER_KEY = "0x" + "11" * 32
OWNER = Account.from_key(OWNER_KEY).address
BATCH = "0x" + "ab" * 32


def test_make_chunk_span_is_little_endian_length():
    c = make_chunk(b"hello")
    assert c[:8] == (5).to_bytes(8, "little")
    assert c[8:] == b"hello"


def test_chunk_address_is_32_bytes_and_deterministic():
    c = make_chunk(b"some-data")
    a1, a2 = chunk_address(c), chunk_address(c)
    assert len(a1) == 32
    assert a1 == a2


def test_sign_stamp_format_and_eip191_recovery():
    chunk = make_chunk(b"unit-test-chunk")
    ts = 1_700_000_000_000  # fixed so we can recompute the digest
    stamp_hex, addr_hex, signer = sign_stamp(OWNER_KEY, chunk, BATCH, height=0, timestamp_ms=ts)

    # format
    assert signer.lower() == OWNER.lower()
    assert len(stamp_hex) == 226   # 113-byte marshaled stamp
    assert len(addr_hex) == 64
    assert stamp_hex.startswith(BATCH[2:].lower())  # batchID first

    # reconstruct the digest Bee re-derives, and confirm EIP-191 recovery -> owner
    addr = bytes.fromhex(addr_hex)
    bid = bytes.fromhex(BATCH[2:])
    bucket = int.from_bytes(addr[:2], "big")
    index = bucket.to_bytes(4, "big") + (0).to_bytes(4, "big")
    to_sign = keccak(addr + bid + index + ts.to_bytes(8, "big"))

    sig = bytes.fromhex(stamp_hex)[-65:]
    assert sig[64] in (27, 28)  # v in Bee's accepted range
    recovered = Account.recover_message(encode_defunct(primitive=to_sign), signature=sig)
    assert recovered.lower() == OWNER.lower()


def test_sign_stamp_bucket_matches_chunk_address():
    chunk = make_chunk(b"bucket-check")
    stamp_hex, addr_hex, _ = sign_stamp(OWNER_KEY, chunk, BATCH, timestamp_ms=1_700_000_000_000)
    addr = bytes.fromhex(addr_hex)
    index = bytes.fromhex(stamp_hex)[32:40]
    bucket = int.from_bytes(index[:4], "big")
    assert bucket == int.from_bytes(addr[:2], "big")  # top 16 bits of the chunk address
