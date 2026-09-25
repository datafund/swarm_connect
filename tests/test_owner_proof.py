"""Owner addresses are canonical, and an owner can prove ownership by signature (#384).

Before this, an owned batch was writable only on a paid request whose x402 payer
matched the owner string exactly: the owner paid again on every upload just to
say who they were, and a casing difference locked them out of their own batch.
"""
import json
import time
from unittest.mock import patch

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

from app.core.atomic_io import StateLoadError
from app.services import signed_auth
from app.services.signed_auth import (
    DEBUG_PREFIX,
    OwnerProofError,
    consume_owner_proof,
    owner_proof_message,
    verify_owner_proof,
)
from app.services.stamp_ownership import StampOwnershipManager, normalize_address

STAMP = "ab" * 32
OWNER = Account.create()
OTHER = Account.create()


def _sign(account, text):
    return account.sign_message(encode_defunct(text=text)).signature.hex()


def _proof(account, batch_id=STAMP, ts=None):
    ts = int(time.time()) if ts is None else ts
    return str(ts), _sign(account, owner_proof_message(batch_id, ts))


@pytest.fixture(autouse=True)
def _fresh_used_set():
    signed_auth._used_owner_proofs.clear()
    yield
    signed_auth._used_owner_proofs.clear()


@pytest.fixture
def x402_on():
    with patch("app.services.stamp_ownership.settings") as s:
        s.X402_ENABLED = True
        s.STAMP_OWNERSHIP_ALLOW_UNTRACKED = False
        yield s


@pytest.fixture
def manager(tmp_path):
    return StampOwnershipManager(state_file=str(tmp_path / "owners.json"))


class TestAddressNormalisation:
    def test_normalize_address(self):
        a = "0x" + "AbCdEf" * 6 + "0123"
        assert normalize_address(a) == a.lower()
        assert normalize_address(a[2:]) == a.lower()
        for bad in (None, "", "shared", "0x123", "0x" + "g" * 40, 42):
            assert normalize_address(bad) is None

    def test_register_stores_canonical_and_any_casing_matches(self, manager, x402_on):
        manager.register_stamp(STAMP, owner=OWNER.address, mode="paid", source="direct_purchase")
        assert manager.get_stamp_info(STAMP)["owner"] == OWNER.address.lower()
        for form in (OWNER.address, OWNER.address.lower(), "0x" + OWNER.address[2:].upper()):
            assert manager.check_access(STAMP, form, "paid")[0] is True, form
        assert manager.check_access(STAMP, OTHER.address, "paid")[0] is False

    def test_register_rejects_invalid_owner(self, manager):
        with pytest.raises(ValueError):
            manager.register_stamp(STAMP, owner="0xnotanaddress", mode="paid", source="x")

    def test_persisted_mixed_case_owner_is_normalised_at_load(self, tmp_path, x402_on):
        path = tmp_path / "owners.json"
        path.write_text(json.dumps({
            STAMP: {"owner": OWNER.address, "mode": "paid", "source": "created_for_owner"},
            "cd" * 32: {"owner": "shared", "mode": "free", "source": "pool_acquire"},
        }))
        mgr = StampOwnershipManager(state_file=str(path))
        mgr.load_on_startup()
        assert mgr.get_stamp_info(STAMP)["owner"] == OWNER.address.lower()
        assert mgr.get_stamp_info("cd" * 32)["owner"] == "shared"
        assert mgr.check_access(STAMP, OWNER.address.lower(), "paid")[0] is True

    def test_persisted_invalid_owner_refuses_to_load(self, tmp_path):
        path = tmp_path / "owners.json"
        path.write_text(json.dumps({STAMP: {"owner": "0xOwner", "mode": "paid"}}))
        with pytest.raises(StateLoadError):
            StampOwnershipManager(state_file=str(path)).load_on_startup()


class TestVerifyOwnerProof:
    def test_valid_proof_recovers_signer(self):
        ts, sig = _proof(OWNER)
        assert verify_owner_proof(STAMP, ts, sig).signer == OWNER.address

    def test_batch_id_casing_does_not_matter(self):
        ts, sig = _proof(OWNER, batch_id=STAMP.upper())
        assert verify_owner_proof(STAMP, ts, sig).signer == OWNER.address

    def test_proof_is_single_use_once_consumed(self):
        ts, sig = _proof(OWNER)
        proof = verify_owner_proof(STAMP, ts, sig)
        # Verifying alone does not spend it.
        assert verify_owner_proof(STAMP, ts, sig) == proof
        consume_owner_proof(proof)
        with pytest.raises(OwnerProofError, match="already been used"):
            verify_owner_proof(STAMP, ts, sig)
        with pytest.raises(OwnerProofError, match="already been used"):
            consume_owner_proof(proof)

    def test_proof_signed_before_process_start_rejected(self):
        """A restart empties the used set, so older proofs are refused outright."""
        ts, sig = _proof(OWNER, ts=int(time.time()) - 5)
        with patch.object(signed_auth, "_process_started_at", int(time.time())):
            with pytest.raises(OwnerProofError, match="restarted"):
                verify_owner_proof(STAMP, ts, sig)

    def test_proof_for_another_batch_names_a_different_signer(self):
        ts, sig = _proof(OWNER, batch_id="ef" * 32)
        assert verify_owner_proof(STAMP, ts, sig).signer != OWNER.address

    def test_debug_signature_is_not_an_owner_proof(self):
        ts = int(time.time())
        sig = _sign(OWNER, f"{DEBUG_PREFIX}{ts}")
        assert verify_owner_proof(STAMP, str(ts), sig).signer != OWNER.address

    @pytest.mark.parametrize("offset", [-10_000, 10_000])
    def test_stale_or_future_timestamp_rejected(self, offset):
        ts, sig = _proof(OWNER, ts=int(time.time()) + offset)
        with pytest.raises(OwnerProofError, match="stale"):
            verify_owner_proof(STAMP, ts, sig)

    @pytest.mark.parametrize("ts,sig", [(None, "0x00"), ("123", None), ("abc", "0x00")])
    def test_missing_or_malformed_rejected(self, ts, sig):
        with pytest.raises(OwnerProofError):
            verify_owner_proof(STAMP, ts, sig)

    def test_garbage_signature_rejected(self):
        with pytest.raises(OwnerProofError, match="not valid"):
            verify_owner_proof(STAMP, str(int(time.time())), "0x1234")


class TestUploadWithOwnerProof:
    """The upload routes accept the proof in place of a payment."""

    @pytest.fixture
    def client(self, manager, x402_on):
        from app.main import app
        manager.register_stamp(STAMP, owner=OWNER.address, mode="paid", source="direct_purchase")
        with patch("app.api.endpoints.data.settings") as ds, \
             patch("app.api.endpoints.data.stamp_ownership_manager", manager), \
             patch("app.api.endpoints.data.upload_data_to_swarm", return_value="ref123"):
            ds.X402_ENABLED = True
            ds.MAX_UPLOAD_SIZE_MB = 10
            yield TestClient(app)

    def _upload(self, client, headers):
        return client.post(
            f"/api/v1/data/?stamp_id={STAMP}",
            files={"file": ("t.json", b'{"data": 1}', "application/json")},
            headers=headers,
        )

    def _headers(self, account, **kw):
        ts, sig = _proof(account, **kw)
        return {"X-Owner-Timestamp": ts, "X-Owner-Signature": sig}

    def test_no_proof_denied(self, client):
        r = self._upload(client, {})
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "STAMP_OWNERSHIP_DENIED"

    def test_owner_proof_allowed(self, client):
        r = self._upload(client, self._headers(OWNER))
        assert r.status_code == 200, r.text

    def test_wrong_signer_denied(self, client):
        r = self._upload(client, self._headers(OTHER))
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "STAMP_OWNERSHIP_DENIED"
        # A refused proof is not recorded as used.
        assert signed_auth._used_owner_proofs == {}

    def test_replayed_proof_rejected(self, client):
        h = self._headers(OWNER)
        assert self._upload(client, h).status_code == 200
        r = self._upload(client, h)
        assert r.status_code == 401
        assert r.json()["detail"]["code"] == "OWNER_PROOF_INVALID"

    def test_stale_proof_rejected(self, client):
        r = self._upload(client, self._headers(OWNER, ts=int(time.time()) - 10_000))
        assert r.status_code == 401
        assert r.json()["detail"]["code"] == "OWNER_PROOF_INVALID"

    def test_manifest_route_checks_proof(self, client):
        def manifest(headers):
            return client.post(
                f"/api/v1/data/manifest?stamp_id={STAMP}",
                files={"file": ("t.tar", b"x", "application/x-tar")},
                headers=headers,
            )
        # Wrong signer is refused before any TAR handling; the owner gets past
        # the ownership check (and is then refused for the invalid TAR).
        assert manifest(self._headers(OTHER)).status_code == 403
        assert manifest(self._headers(OWNER)).status_code == 400
