# tests/test_notary_provenance.py
"""
Unit tests for the ProvenanceService (app/services/provenance.py).
"""
import json
import pytest
from datetime import datetime, timezone
from eth_account import Account
from unittest.mock import MagicMock, patch

from app.services.provenance import (
    ProvenanceService,
    ProvenanceError,
    DocumentValidationError,
    NotaryNotEnabledError,
    SignedDocument,
    get_provenance_service,
)
from app.services.signing import SigningService, NotConfiguredError


class TestDocumentValidation:
    """Tests for document validation functionality."""

    @pytest.fixture
    def service(self):
        """Create a provenance service with a mock signing service."""
        mock_signer = MagicMock(spec=SigningService)
        mock_signer.is_configured = True
        mock_signer.public_address = "0x" + "a" * 40
        return ProvenanceService(mock_signer)

    def test_validate_valid_document(self, service):
        """Test validation of a valid document."""
        doc = {"data": {"test": "value"}}
        raw = json.dumps(doc).encode('utf-8')

        document, data = service.validate_document(raw)

        assert document == doc
        assert data == {"test": "value"}

    def test_validate_document_with_signatures(self, service):
        """Test validation of document with existing signatures."""
        doc = {
            "data": {"test": "value"},
            "signatures": [{"existing": "sig"}]
        }
        raw = json.dumps(doc).encode('utf-8')

        document, data = service.validate_document(raw)

        assert len(document["signatures"]) == 1

    def test_validate_rejects_non_json(self, service):
        """Test that non-JSON content is rejected."""
        raw = b"not json content"

        with pytest.raises(DocumentValidationError, match="valid JSON"):
            service.validate_document(raw)

    def test_validate_rejects_non_utf8(self, service):
        """Test that non-UTF-8 content is rejected."""
        raw = b"\xff\xfe invalid utf8"

        with pytest.raises(DocumentValidationError, match="valid UTF-8"):
            service.validate_document(raw)

    def test_validate_rejects_non_object(self, service):
        """Test that non-object JSON is rejected."""
        raw = json.dumps([1, 2, 3]).encode('utf-8')

        with pytest.raises(DocumentValidationError, match="JSON object"):
            service.validate_document(raw)

    def test_validate_rejects_missing_data_field(self, service):
        """Test that missing data field is rejected."""
        doc = {"other": "field"}
        raw = json.dumps(doc).encode('utf-8')

        with pytest.raises(DocumentValidationError, match="'data' field"):
            service.validate_document(raw)

    def test_validate_rejects_invalid_signatures_type(self, service):
        """Test that non-array signatures field is rejected."""
        doc = {"data": "test", "signatures": "not an array"}
        raw = json.dumps(doc).encode('utf-8')

        with pytest.raises(DocumentValidationError, match="must be an array"):
            service.validate_document(raw)

    def test_validate_accepts_any_data_type(self, service):
        """Test that data field can be any JSON type."""
        # String
        doc = {"data": "string data"}
        raw = json.dumps(doc).encode('utf-8')
        _, data = service.validate_document(raw)
        assert data == "string data"

        # Number
        doc = {"data": 123}
        raw = json.dumps(doc).encode('utf-8')
        _, data = service.validate_document(raw)
        assert data == 123

        # Array
        doc = {"data": [1, 2, 3]}
        raw = json.dumps(doc).encode('utf-8')
        _, data = service.validate_document(raw)
        assert data == [1, 2, 3]

        # Object
        doc = {"data": {"nested": "object"}}
        raw = json.dumps(doc).encode('utf-8')
        _, data = service.validate_document(raw)
        assert data == {"nested": "object"}


class TestSignDocument:
    """Tests for document signing functionality."""

    @pytest.fixture
    def real_service(self):
        """Create a provenance service with a real signing service."""
        account = Account.create()
        signer = SigningService(account.key.hex()[2:])
        return ProvenanceService(signer), account.address

    def test_sign_document_adds_signature(self, real_service):
        """Test that signing adds a signature to the document."""
        service, address = real_service
        doc = {"data": {"test": "value"}}
        raw = json.dumps(doc).encode('utf-8')

        with patch.object(type(service), 'is_available', property(lambda s: True)):
            with patch('app.services.provenance.settings') as mock_settings:
                mock_settings.NOTARY_ENABLED = True
                result = service.sign_document(raw)

        assert isinstance(result, SignedDocument)
        assert len(result.signatures) == 1

    def test_sign_document_preserves_existing_signatures(self, real_service):
        """Test that existing signatures are preserved."""
        service, address = real_service
        doc = {
            "data": {"test": "value"},
            "signatures": [{"existing": "signature"}]
        }
        raw = json.dumps(doc).encode('utf-8')

        with patch.object(type(service), 'is_available', property(lambda s: True)):
            with patch('app.services.provenance.settings') as mock_settings:
                mock_settings.NOTARY_ENABLED = True
                result = service.sign_document(raw)

        assert len(result.signatures) == 2
        assert result.signatures[0] == {"existing": "signature"}

    def test_sign_document_signature_structure(self, real_service):
        """Test that notary signature has correct structure."""
        service, address = real_service
        doc = {"data": {"test": "value"}}
        raw = json.dumps(doc).encode('utf-8')

        with patch.object(type(service), 'is_available', property(lambda s: True)):
            with patch('app.services.provenance.settings') as mock_settings:
                mock_settings.NOTARY_ENABLED = True
                result = service.sign_document(raw)

        sig = result.signatures[0]
        assert sig["type"] == "notary"
        # Check that signer is an Ethereum address format
        assert sig["signer"].startswith("0x")
        assert len(sig["signer"]) == 42
        assert "timestamp" in sig
        assert "data_hash" in sig
        assert "signature" in sig
        assert sig["hashed_fields"] == ["data"]
        assert sig["signed_message_format"] == "{data_hash}|{timestamp}"

    def test_sign_document_with_custom_timestamp(self, real_service):
        """Test signing with a custom timestamp."""
        service, _ = real_service
        doc = {"data": {"test": "value"}}
        raw = json.dumps(doc).encode('utf-8')
        custom_time = datetime(2026, 1, 21, 12, 0, 0, tzinfo=timezone.utc)

        with patch.object(type(service), 'is_available', property(lambda s: True)):
            with patch('app.services.provenance.settings') as mock_settings:
                mock_settings.NOTARY_ENABLED = True
                result = service.sign_document(raw, custom_time)

        sig = result.signatures[0]
        assert "2026-01-21T12:00:00" in sig["timestamp"]

    def test_sign_document_returns_valid_json(self, real_service):
        """Test that raw_json is valid JSON."""
        service, _ = real_service
        doc = {"data": {"test": "value"}}
        raw = json.dumps(doc).encode('utf-8')

        with patch.object(type(service), 'is_available', property(lambda s: True)):
            with patch('app.services.provenance.settings') as mock_settings:
                mock_settings.NOTARY_ENABLED = True
                result = service.sign_document(raw)

        # Should parse without error
        parsed = json.loads(result.raw_json)
        assert "data" in parsed
        assert "signatures" in parsed

    def test_sign_document_fails_when_not_enabled(self):
        """Test that signing fails when notary is not enabled."""
        mock_signer = MagicMock(spec=SigningService)
        mock_signer.is_configured = True
        service = ProvenanceService(mock_signer)

        doc = {"data": "test"}
        raw = json.dumps(doc).encode('utf-8')

        with patch('app.services.provenance.settings') as mock_settings:
            mock_settings.NOTARY_ENABLED = False
            with pytest.raises(NotaryNotEnabledError):
                service.sign_document(raw)

    def test_sign_document_fails_when_not_configured(self):
        """Test that signing fails when signer is not configured."""
        mock_signer = MagicMock(spec=SigningService)
        mock_signer.is_configured = False
        service = ProvenanceService(mock_signer)

        doc = {"data": "test"}
        raw = json.dumps(doc).encode('utf-8')

        with patch('app.services.provenance.settings') as mock_settings:
            mock_settings.NOTARY_ENABLED = True
            with pytest.raises(NotConfiguredError):
                service.sign_document(raw)

    def test_sign_invalid_document_fails(self, real_service):
        """Test that signing invalid document fails."""
        service, _ = real_service
        raw = b"not json"

        with patch('app.services.provenance.settings') as mock_settings:
            mock_settings.NOTARY_ENABLED = True
            with pytest.raises(DocumentValidationError):
                service.sign_document(raw)


class TestVerifyNotarySignature:
    """Tests for signature verification."""

    @pytest.fixture
    def real_service(self):
        """Create a provenance service with a real signing service."""
        account = Account.create()
        signer = SigningService(account.key.hex()[2:])
        return ProvenanceService(signer), account.address

    def test_verify_valid_signature(self, real_service):
        """Test verification of a valid signature."""
        service, _ = real_service
        doc = {"data": {"test": "value"}}
        raw = json.dumps(doc).encode('utf-8')

        with patch('app.services.provenance.settings') as mock_settings:
            mock_settings.NOTARY_ENABLED = True
            signed = service.sign_document(raw)

        # Get the signer address from the signed document
        signer_address = signed.signatures[-1]["signer"]

        # Verify the signed document using the address from the signature
        is_valid, error = service.verify_notary_signature(
            signed.raw_json.encode('utf-8'),
            signer_address
        )

        assert is_valid is True
        assert error is None

    def test_verify_wrong_signer_fails(self, real_service):
        """Test verification fails with wrong expected signer."""
        service, address = real_service
        doc = {"data": {"test": "value"}}
        raw = json.dumps(doc).encode('utf-8')

        with patch('app.services.provenance.settings') as mock_settings:
            mock_settings.NOTARY_ENABLED = True
            signed = service.sign_document(raw)

        wrong_address = "0x" + "b" * 40
        is_valid, error = service.verify_notary_signature(
            signed.raw_json.encode('utf-8'),
            wrong_address
        )

        assert is_valid is False
        assert "mismatch" in error.lower()

    def test_verify_tampered_data_fails(self, real_service):
        """Test verification fails if data was modified."""
        service, address = real_service
        doc = {"data": {"test": "value"}}
        raw = json.dumps(doc).encode('utf-8')

        with patch('app.services.provenance.settings') as mock_settings:
            mock_settings.NOTARY_ENABLED = True
            signed = service.sign_document(raw)

        # Get the signer address from the signed document
        signer_address = signed.signatures[-1]["signer"]

        # Tamper with the signed document
        tampered = json.loads(signed.raw_json)
        tampered["data"]["test"] = "tampered"
        tampered_raw = json.dumps(tampered).encode('utf-8')

        is_valid, error = service.verify_notary_signature(
            tampered_raw,
            signer_address
        )

        assert is_valid is False
        assert "hash mismatch" in error.lower()

    def test_verify_no_signatures_fails(self, real_service):
        """Test verification fails when no signatures present."""
        service, _ = real_service
        doc = {"data": {"test": "value"}}
        raw = json.dumps(doc).encode('utf-8')

        is_valid, error = service.verify_notary_signature(raw)

        assert is_valid is False
        assert "no signatures" in error.lower()

    def test_verify_no_notary_signature_fails(self, real_service):
        """Test verification fails when no notary signature present."""
        service, _ = real_service
        doc = {
            "data": {"test": "value"},
            "signatures": [{"type": "other", "value": "xxx"}]
        }
        raw = json.dumps(doc).encode('utf-8')

        is_valid, error = service.verify_notary_signature(raw)

        assert is_valid is False
        assert "no notary signature" in error.lower()


class TestProvenanceServiceProperties:
    """Tests for ProvenanceService properties."""

    def test_is_available_when_enabled_and_configured(self):
        """Test is_available returns True when properly configured."""
        mock_signer = MagicMock(spec=SigningService)
        mock_signer.is_configured = True
        service = ProvenanceService(mock_signer)

        with patch('app.services.provenance.settings') as mock_settings:
            mock_settings.NOTARY_ENABLED = True
            assert service.is_available is True

    def test_is_available_false_when_disabled(self):
        """Test is_available returns False when disabled."""
        mock_signer = MagicMock(spec=SigningService)
        mock_signer.is_configured = True
        service = ProvenanceService(mock_signer)

        with patch('app.services.provenance.settings') as mock_settings:
            mock_settings.NOTARY_ENABLED = False
            assert service.is_available is False

    def test_is_available_false_when_not_configured(self):
        """Test is_available returns False when not configured."""
        mock_signer = MagicMock(spec=SigningService)
        mock_signer.is_configured = False
        service = ProvenanceService(mock_signer)

        with patch('app.services.provenance.settings') as mock_settings:
            mock_settings.NOTARY_ENABLED = True
            assert service.is_available is False

    def test_notary_address_when_configured(self):
        """Test notary_address returns address when configured."""
        mock_signer = MagicMock(spec=SigningService)
        mock_signer.is_configured = True
        mock_signer.public_address = "0x123"
        service = ProvenanceService(mock_signer)

        assert service.notary_address == "0x123"

    def test_notary_address_none_when_not_configured(self):
        """Test notary_address returns None when not configured."""
        mock_signer = MagicMock(spec=SigningService)
        mock_signer.is_configured = False
        service = ProvenanceService(mock_signer)

        assert service.notary_address is None


class TestGetProvenanceService:
    """Tests for get_provenance_service singleton."""

    def test_get_provenance_service_returns_service(self):
        """Test that get_provenance_service returns a ProvenanceService."""
        import app.services.provenance as provenance_module
        provenance_module._provenance_service = None

        service = get_provenance_service()

        assert isinstance(service, ProvenanceService)

    def test_get_provenance_service_returns_same_instance(self):
        """Test that get_provenance_service returns singleton."""
        import app.services.provenance as provenance_module
        provenance_module._provenance_service = None

        service1 = get_provenance_service()
        service2 = get_provenance_service()

        assert service1 is service2
