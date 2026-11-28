"""
A sign=notary document nested too deeply is refused with 400 JSON_TOO_DEEP,
before payment, instead of a RecursionError answered as 500 (#695).
"""
import json
import time
from unittest.mock import AsyncMock, patch

import pytest
from eth_account import Account
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings, settings
from app.main import app
from app.services.metrics import notary_signatures_total, uploads_total
from app.services.provenance import (
    DocumentTooDeepError,
    DocumentValidationError,
    ProvenanceService,
)
from app.services.signing import SigningService

STAMP_ID = "a" * 64
REFERENCE = "b" * 64


def document(depth: int, kind: str = "array") -> bytes:
    """A notary document whose deepest nesting is exactly `depth` levels,
    counting the root object as level 1 and the data value as level 2."""
    inner = depth - 1
    if kind == "array":
        data = b"[" * inner + b"]" * inner
    else:
        data = b'{"a":' * (inner - 1) + b"{}" + b"}" * (inner - 1)
    return b'{"data":' + data + b"}"


@pytest.fixture
def service():
    return ProvenanceService(SigningService(Account.create().key.hex()[2:]))


@pytest.fixture
def notary_on():
    with patch.object(settings, "NOTARY_ENABLED", True):
        yield


def counter(metric, status):
    return metric.labels(status=status)._value.get()


class TestSetting:
    def test_default_is_64_and_separate_from_body_limit(self):
        s = Settings(_env_file=None, SWARM_BEE_API_URL="http://localhost:1")
        assert s.NOTARY_MAX_JSON_DEPTH == 64
        assert s.MAX_JSON_DEPTH == 20

    @pytest.mark.parametrize("value", [1, 1000])
    def test_bounds_accepted(self, value):
        s = Settings(_env_file=None, SWARM_BEE_API_URL="http://localhost:1", NOTARY_MAX_JSON_DEPTH=value)
        assert s.NOTARY_MAX_JSON_DEPTH == value

    @pytest.mark.parametrize("value", [0, -1, 1001, "deep"])
    def test_out_of_range_fails_startup(self, value):
        with pytest.raises(ValidationError, match="NOTARY_MAX_JSON_DEPTH"):
            Settings(_env_file=None, SWARM_BEE_API_URL="http://localhost:1", NOTARY_MAX_JSON_DEPTH=value)


class TestValidateDocument:
    @pytest.mark.parametrize("kind", ["array", "object"])
    def test_just_under_and_at_limit_accepted(self, service, kind):
        limit = settings.NOTARY_MAX_JSON_DEPTH
        for depth in (limit - 1, limit):
            raw = document(depth, kind)
            parsed, _ = service.validate_document(raw)
            assert parsed == json.loads(raw)

    @pytest.mark.parametrize("kind", ["array", "object"])
    def test_over_limit_refused(self, service, kind):
        with pytest.raises(DocumentTooDeepError, match="too deeply"):
            service.validate_document(document(settings.NOTARY_MAX_JSON_DEPTH + 1, kind))

    def test_refused_before_parsing(self, service):
        with patch("app.services.provenance.json.loads") as loads:
            with pytest.raises(DocumentTooDeepError):
                service.validate_document(document(100_000))
        loads.assert_not_called()

    def test_recursion_error_is_still_a_validation_error(self, service):
        # Fallback: should the pre-check ever let one through, the real
        # RecursionError from json.loads (100000 levels) is still refused.
        with patch("app.services.provenance.json_depth_within", return_value=True):
            with pytest.raises(DocumentTooDeepError):
                service.validate_document(document(100_000))

    @pytest.mark.parametrize("call", ["canonical", "indented"])
    def test_recursion_error_while_encoding_is_too_deep(self, service, notary_on, call):
        # Both json.dumps calls in sign_document: the canonical one hashed for
        # the signature, and json.dumps(indent=2) of the signed document,
        # which uses the pure-Python encoder and recurses once per level.
        real_dumps = json.dumps

        def dumps(*args, **kwargs):
            if ("indent" in kwargs) == (call == "indented"):
                raise RecursionError
            return real_dumps(*args, **kwargs)

        with patch("app.services.provenance.json.dumps", side_effect=dumps):
            with pytest.raises(DocumentTooDeepError):
                service.sign_document(document(3))

    def test_largest_allowed_setting_still_signs(self, service, notary_on):
        # At NOTARY_MAX_JSON_DEPTH=1000, a document at the limit is parsed,
        # hashed and re-encoded without a RecursionError. This holds because
        # eth_account's py_ecc raises the interpreter's recursion limit to
        # 100000 when imported; with the default 1000, json.dumps(indent=2)
        # failed at about 995 levels (measured on 3.12.13).
        with patch.object(settings, "NOTARY_MAX_JSON_DEPTH", 1000):
            signed = service.sign_document(document(1000))
        assert json.loads(signed.raw_json)["signatures"][-1]["type"] == "notary"

    def test_brackets_in_strings_do_not_count(self, service):
        raw = json.dumps({"data": {"text": "[" * 10_000 + '\\"{' * 1000}}).encode()
        service.validate_document(raw)

    def test_too_deep_is_a_document_validation_error(self):
        assert issubclass(DocumentTooDeepError, DocumentValidationError)

    def test_limit_follows_the_setting(self, service):
        with patch.object(settings, "NOTARY_MAX_JSON_DEPTH", 3):
            service.validate_document(document(3))
            with pytest.raises(DocumentTooDeepError):
                service.validate_document(document(4))


class TestUploadEndpoint:
    @pytest.fixture
    def client(self, service, notary_on):
        with patch("app.api.endpoints.data.get_provenance_service", return_value=service), \
             patch("app.api.endpoints.data.get_batch_expiry", return_value=None):
            yield TestClient(app, raise_server_exceptions=False)

    def upload(self, client, body: bytes):
        with patch("app.api.endpoints.data.settle_payment", new_callable=AsyncMock) as settle, \
             patch("app.api.endpoints.data.upload_data_to_swarm",
                   new_callable=AsyncMock, return_value=REFERENCE) as upload:
            start = time.perf_counter()
            response = client.post(
                f"/api/v1/data/?stamp_id={STAMP_ID}&sign=notary",
                files={"file": ("doc.json", body, "application/json")},
            )
            elapsed = time.perf_counter() - start
        return response, settle, upload, elapsed

    def assert_refused(self, response, settle, upload):
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["code"] == "JSON_TOO_DEEP"
        assert str(settings.NOTARY_MAX_JSON_DEPTH) in response.json()["detail"]["message"]
        settle.assert_not_called()
        upload.assert_not_called()

    @pytest.mark.parametrize("kind", ["array", "object"])
    def test_one_over_the_limit(self, client, kind):
        response, settle, upload, _ = self.upload(
            client, document(settings.NOTARY_MAX_JSON_DEPTH + 1, kind))
        self.assert_refused(response, settle, upload)

    def test_very_deep_body_at_the_upload_size_limit(self, client):
        # The triage case: about 10 MB of nesting, which made json.loads
        # raise RecursionError and the endpoint answer 500.
        size = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
        body = b'{"data":' + b"[" * (size - 9) + b"}"
        assert len(body) == size
        response, settle, upload, elapsed = self.upload(client, body)
        self.assert_refused(response, settle, upload)
        assert elapsed < 10.0

    def test_recursion_error_fallback_is_400_not_500(self, client):
        # The pre-check switched off: json.loads raises a real RecursionError.
        with patch("app.services.provenance.json_depth_within", return_value=True):
            response, settle, upload, _ = self.upload(client, document(100_000))
        self.assert_refused(response, settle, upload)

    def test_metrics_for_a_refused_document(self, client):
        notary_before = counter(notary_signatures_total, "error")
        uploads_before = counter(uploads_total, "error")
        success_before = counter(notary_signatures_total, "success")
        self.upload(client, document(settings.NOTARY_MAX_JSON_DEPTH + 1))
        # Counted as a refused signing and an upload error, as any other
        # invalid notary document is; never as a success.
        assert counter(notary_signatures_total, "error") == notary_before + 1
        assert counter(uploads_total, "error") == uploads_before + 1
        assert counter(notary_signatures_total, "success") == success_before

    def test_document_at_the_limit_is_signed_and_uploaded(self, client):
        raw = document(settings.NOTARY_MAX_JSON_DEPTH, "object")
        success_before = counter(notary_signatures_total, "success")
        response, settle, upload, _ = self.upload(client, raw)
        assert response.status_code == 200, response.text
        assert response.json()["reference"] == REFERENCE
        settle.assert_awaited_once()
        stored = json.loads(upload.call_args.kwargs["data"])
        assert stored["data"] == json.loads(raw)["data"]
        assert stored["signatures"][-1]["type"] == "notary"
        assert counter(notary_signatures_total, "success") == success_before + 1

    def test_invalid_document_still_gets_its_own_code(self, client):
        response, settle, _, _ = self.upload(client, json.dumps({"no_data": 1}).encode())
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "INVALID_DOCUMENT_FORMAT"
        settle.assert_not_called()
