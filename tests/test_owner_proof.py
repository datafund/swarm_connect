"""Owner addresses are canonical, and an owner can prove ownership by signature (#384).

Before this, an owned batch was writable only on a paid request whose x402 payer
matched the owner string exactly: the owner paid again on every upload just to
say who they were, and a casing difference locked them out of their own batch.
"""
import json
import time
from unittest.mock import patch

import httpx
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


class TestOwnerProofSpentOnlyOnSuccess:
    """A proof is spent only by an upload that reached Bee and stored data (#470).

    It used to be marked used as soon as it granted access, so a request
    refused afterwards (bad redundancy, stamp validation, the exact size check,
    an unreachable Bee) cost the owner the proof and they had to sign again.
    """

    @pytest.fixture
    def ds(self, manager, x402_on):
        manager.register_stamp(STAMP, owner=OWNER.address, mode="paid", source="direct_purchase")
        with patch("app.api.endpoints.data.settings") as ds, \
             patch("app.api.endpoints.data.stamp_ownership_manager", manager), \
             patch("app.api.endpoints.data.check_upload_failure_reason", return_value=None), \
             patch("app.api.endpoints.data.get_batch_expiry", return_value=None):
            ds.X402_ENABLED = True
            ds.MAX_UPLOAD_SIZE_MB = 10
            yield ds

    @pytest.fixture
    def client(self, ds):
        from app.main import app
        return TestClient(app)

    def _upload(self, client, headers, query="", body=b'{"data": 1}'):
        return client.post(
            f"/api/v1/data/?stamp_id={STAMP}{query}",
            files={"file": ("t.json", body, "application/json")},
            headers=headers,
        )

    def _headers(self):
        ts, sig = _proof(OWNER)
        return {"X-Owner-Timestamp": ts, "X-Owner-Signature": sig}

    def _retry_succeeds(self, client, h):
        with patch("app.api.endpoints.data.upload_data_to_swarm", return_value="ref123"):
            r = self._upload(client, h)
        assert r.status_code == 200, r.text

    def _retry_refused(self, client, h, match="already been used"):
        with patch("app.api.endpoints.data.upload_data_to_swarm", return_value="ref123") as up:
            r = self._upload(client, h)
        assert r.status_code == 401, r.text
        assert r.json()["detail"]["code"] == "OWNER_PROOF_INVALID"
        assert match in r.json()["detail"]["message"]
        up.assert_not_called()

    # --- refused before Bee: the proof is released --------------------------

    def test_failed_stamp_validation_does_not_spend_the_proof(self, client):
        from app.services.swarm_api import StampValidationError
        h = self._headers()
        err = StampValidationError("full", status="full", code="STAMP_FULL")
        with patch("app.api.endpoints.data.validate_stamp_for_upload", side_effect=err):
            assert self._upload(client, h, "&validate_stamp=true").status_code == 400
        self._retry_succeeds(client, h)

    def test_exact_size_check_does_not_spend_the_proof(self, client, ds):
        h = self._headers()
        ds.MAX_UPLOAD_SIZE_MB = 0  # envelope allowance passes; the file's own length does not
        assert self._upload(client, h).status_code == 413
        ds.MAX_UPLOAD_SIZE_MB = 10
        self._retry_succeeds(client, h)

    def test_invalid_sign_option_does_not_spend_the_proof(self, client):
        h = self._headers()
        assert self._upload(client, h, "&sign=bogus").status_code == 400
        self._retry_succeeds(client, h)

    def test_unreachable_bee_does_not_spend_the_proof(self, client):
        h = self._headers()
        with patch("app.api.endpoints.data.upload_data_to_swarm",
                   side_effect=httpx.ConnectError("refused")):
            assert self._upload(client, h).status_code == 502
        self._retry_succeeds(client, h)

    def test_bee_4xx_refusal_does_not_spend_the_proof(self, client):
        h = self._headers()
        req = httpx.Request("POST", "http://bee/bytes")
        err = httpx.HTTPStatusError("bad", request=req, response=httpx.Response(400, request=req))
        with patch("app.api.endpoints.data.upload_data_to_swarm", side_effect=err):
            assert self._upload(client, h).status_code == 502
        self._retry_succeeds(client, h)

    def test_manifest_invalid_tar_does_not_spend_the_proof(self, client):
        h = self._headers()

        def manifest():
            return client.post(
                f"/api/v1/data/manifest?stamp_id={STAMP}",
                files={"file": ("t.tar", b"x", "application/x-tar")},
                headers=h,
            )
        assert manifest().status_code == 400
        # The same proof is accepted again: it gets past access to the TAR check.
        assert manifest().status_code == 400
        self._retry_succeeds(client, h)

    def test_cancellation_before_bee_does_not_spend_the_proof(self, client):
        # A client disconnect reaches the handler as a cancellation.
        import asyncio
        h = self._headers()
        with patch("app.api.endpoints.data.validate_stamp_for_upload",
                   side_effect=asyncio.CancelledError()), \
             patch("app.api.endpoints.data.upload_data_to_swarm", return_value="ref123") as up:
            with pytest.raises(BaseException):
                self._upload(client, h, "&validate_stamp=true")
            up.assert_not_called()
        assert signed_auth._used_owner_proofs == {}
        self._retry_succeeds(client, h)

    # --- Bee may have stored the data: the proof stays spent -----------------

    def test_bee_timeout_spends_the_proof(self, client):
        h = self._headers()
        with patch("app.api.endpoints.data.upload_data_to_swarm",
                   side_effect=httpx.ReadTimeout("slow")):
            assert self._upload(client, h).status_code == 502
        self._retry_refused(client, h)

    def test_bee_5xx_spends_the_proof(self, client):
        h = self._headers()
        req = httpx.Request("POST", "http://bee/bytes")
        err = httpx.HTTPStatusError("bad", request=req, response=httpx.Response(500, request=req))
        with patch("app.api.endpoints.data.upload_data_to_swarm", side_effect=err):
            assert self._upload(client, h).status_code == 502
        self._retry_refused(client, h)

    def test_failure_after_a_stored_upload_spends_the_proof(self, client):
        h = self._headers()
        with patch("app.api.endpoints.data.upload_data_to_swarm", return_value="ref123"), \
             patch("app.api.endpoints.data.get_batch_expiry", side_effect=RuntimeError("boom")):
            assert self._upload(client, h).status_code == 500
        self._retry_refused(client, h)

    def test_success_spends_the_proof(self, client):
        h = self._headers()
        self._retry_succeeds(client, h)
        self._retry_refused(client, h)

    # --- concurrency -----------------------------------------------------------

    def test_concurrent_request_with_the_same_proof_is_refused_while_in_flight(self, client):
        import asyncio
        import threading
        h = self._headers()
        entered, finish = threading.Event(), threading.Event()

        async def slow_upload(**kwargs):
            entered.set()
            await asyncio.to_thread(finish.wait, 10)
            return "ref123"

        results = {}
        with patch("app.api.endpoints.data.upload_data_to_swarm", side_effect=slow_upload) as up:
            t = threading.Thread(target=lambda: results.setdefault("a", self._upload(client, h)))
            t.start()
            assert entered.wait(10)
            r = self._upload(client, h)
            assert r.status_code == 401, r.text
            assert r.json()["detail"]["code"] == "OWNER_PROOF_INVALID"
            assert "in use" in r.json()["detail"]["message"]
            finish.set()
            t.join(10)
            assert up.call_count == 1
        assert results["a"].status_code == 200, results["a"].text
        self._retry_refused(client, h)


class TestOwnerProofReservation:
    """reserve / confirm / release on the proof set itself (#470)."""

    def _verified(self):
        return verify_owner_proof(STAMP, *_proof(OWNER))

    def test_reserved_proof_is_refused_as_in_use_until_released(self):
        ts, sig = _proof(OWNER)
        proof = verify_owner_proof(STAMP, ts, sig)
        hold = signed_auth.reserve_owner_proof(proof)
        with pytest.raises(OwnerProofError, match="in use"):
            signed_auth.reserve_owner_proof(proof)
        with pytest.raises(OwnerProofError, match="in use"):
            verify_owner_proof(STAMP, ts, sig)
        signed_auth.release_owner_proof(hold)
        assert signed_auth._used_owner_proofs == {}
        signed_auth.reserve_owner_proof(proof)

    def test_confirmed_proof_is_used_and_cannot_be_released(self):
        proof = self._verified()
        hold = signed_auth.reserve_owner_proof(proof)
        signed_auth.confirm_owner_proof(hold)
        signed_auth.release_owner_proof(hold)
        with pytest.raises(OwnerProofError, match="already been used"):
            signed_auth.reserve_owner_proof(proof)

    def test_a_settled_hold_cannot_release_a_later_reservation(self):
        proof = self._verified()
        first = signed_auth.reserve_owner_proof(proof)
        signed_auth.release_owner_proof(first)
        second = signed_auth.reserve_owner_proof(proof)
        signed_auth.release_owner_proof(first)  # late, duplicate release
        with pytest.raises(OwnerProofError, match="in use"):
            signed_auth.reserve_owner_proof(proof)
        signed_auth.confirm_owner_proof(second)

    def test_none_is_a_no_op(self):
        signed_auth.confirm_owner_proof(None)
        signed_auth.release_owner_proof(None)

    def test_an_abandoned_reservation_expires_with_the_proof_window(self):
        proof = self._verified()
        signed_auth.reserve_owner_proof(proof)
        with patch("app.services.signed_auth.time.time", return_value=proof.expires + 1):
            other = verify_owner_proof(STAMP, *_proof(OTHER))
            signed_auth.reserve_owner_proof(other)  # purges expired entries
        assert proof.key not in signed_auth._used_owner_proofs


class TestUploadToBeeSettlesTheProof:
    """How _upload_to_bee settles the reservation for each outcome (#470)."""

    def _run(self, upload):
        import asyncio
        from app.api.endpoints.data import _upload_to_bee
        proof = verify_owner_proof(STAMP, *_proof(OWNER))
        hold = signed_auth.reserve_owner_proof(proof)

        async def go():
            return await _upload_to_bee(hold, upload())
        try:
            result = asyncio.run(go())
        except BaseException as exc:
            result = exc
        return proof, result

    def _state(self, proof):
        entry = signed_auth._used_owner_proofs.get(proof.key)
        return entry[1] if entry else None

    def test_success_confirms(self):
        async def ok():
            return "ref"
        proof, result = self._run(ok)
        assert result == "ref" and self._state(proof) == "used"

    def test_connect_error_releases(self):
        async def fail():
            raise httpx.ConnectError("refused")
        proof, result = self._run(fail)
        assert isinstance(result, httpx.ConnectError) and self._state(proof) is None

    def test_cancellation_during_the_bee_call_keeps_it_spent(self):
        import asyncio

        async def cancelled():
            raise asyncio.CancelledError()
        proof, result = self._run(cancelled)
        assert isinstance(result, asyncio.CancelledError) and self._state(proof) == "used"

    def test_unrecognised_error_keeps_it_spent(self):
        async def missing_reference():
            raise ValueError("API Response missing 'reference' from upload")
        proof, _ = self._run(missing_reference)
        assert self._state(proof) == "used"
