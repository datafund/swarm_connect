# tests/test_notary_signing.py
"""
Unit tests for the SigningService (app/services/signing.py).
"""
import hashlib
import pytest
from datetime import datetime, timezone
from eth_account import Account

from app.services.signing import (
    SigningService,
    SigningServiceError,
    NotConfiguredError,
    get_signing_service,
)


class TestSigningServiceInit:
    """Tests for SigningService initialization."""

    def test_init_with_valid_key(self):
        """Test initialization with a valid private key."""
        account = Account.create()
        key_hex = account.key.hex()  # With 0x prefix

        # Create service with the same key
        service = SigningService(key_hex)

        assert service.is_configured is True
        # Both should derive from the same key
        assert service.public_address.lower() == account.address.lower()

    def test_init_with_key_with_0x_prefix(self):
        """Test initialization handles 0x prefix correctly."""
        account = Account.create()
        key_hex = account.key.hex()  # With 0x prefix

        service = SigningService(key_hex)

        assert service.is_configured is True
        assert service.public_address == account.address

    def test_init_with_short_key_pads_correctly(self):
        """Test that short keys are zero-padded to 32 bytes."""
        # Very short key (will be padded)
        short_key = "1234"

        service = SigningService(short_key)

        assert service.is_configured is True
        assert service.public_address is not None

    def test_init_with_no_key_not_configured(self):
        """Test that service without key is not configured."""
        service = SigningService(None)

        assert service.is_configured is False
        assert service.public_address is None

    def test_init_with_invalid_key_raises_error(self):
        """Test that invalid key raises SigningServiceError."""
        with pytest.raises(SigningServiceError, match="Invalid private key"):
            SigningService("not-a-valid-hex-key")


class TestSigningServiceHashing:
    """Tests for SigningService hashing functionality."""

    def test_hash_data_returns_sha256(self):
        """Test that hash_data returns correct SHA-256 hash."""
        data = b"test data"
        expected = hashlib.sha256(data).hexdigest()

        result = SigningService.hash_data(data)

        assert result == expected

    def test_hash_data_is_deterministic(self):
        """Test that hashing is deterministic."""
        data = b"test data"

        hash1 = SigningService.hash_data(data)
        hash2 = SigningService.hash_data(data)

        assert hash1 == hash2

    def test_hash_data_different_inputs_different_hashes(self):
        """Test that different inputs produce different hashes."""
        hash1 = SigningService.hash_data(b"data1")
        hash2 = SigningService.hash_data(b"data2")

        assert hash1 != hash2

    def test_hash_data_empty_input(self):
        """Test hashing empty data."""
        expected = hashlib.sha256(b"").hexdigest()

        result = SigningService.hash_data(b"")

        assert result == expected


class TestSigningServiceSigning:
    """Tests for SigningService signing functionality."""

    @pytest.fixture
    def service(self):
        """Create a signing service with a test key."""
        account = Account.create()
        return SigningService(account.key.hex()[2:])

    def test_sign_with_timestamp_returns_tuple(self, service):
        """Test that sign_with_timestamp returns correct tuple structure."""
        data = b"test data"

        result = service.sign_with_timestamp(data)

        assert len(result) == 4
        data_hash, timestamp, signature, address = result

        assert isinstance(data_hash, str)
        assert isinstance(timestamp, str)
        assert isinstance(signature, str)
        assert isinstance(address, str)

    def test_sign_with_timestamp_hash_is_correct(self, service):
        """Test that returned hash matches data."""
        data = b"test data"
        expected_hash = SigningService.hash_data(data)

        data_hash, _, _, _ = service.sign_with_timestamp(data)

        assert data_hash == expected_hash

    def test_sign_with_timestamp_address_matches_service(self, service):
        """Test that returned address matches service address."""
        data = b"test data"

        _, _, _, address = service.sign_with_timestamp(data)

        assert address == service.public_address

    def test_sign_with_timestamp_iso_format(self, service):
        """Test that timestamp is ISO 8601 format."""
        data = b"test data"

        _, timestamp, _, _ = service.sign_with_timestamp(data)

        # Should contain T for ISO format
        assert "T" in timestamp
        # Should parse without error
        datetime.fromisoformat(timestamp.replace("+00:00", "+00:00"))

    def test_sign_with_custom_timestamp(self, service):
        """Test signing with a custom timestamp."""
        data = b"test data"
        custom_time = datetime(2026, 1, 21, 12, 0, 0, tzinfo=timezone.utc)

        _, timestamp, _, _ = service.sign_with_timestamp(data, custom_time)

        assert "2026-01-21T12:00:00" in timestamp

    def test_sign_unconfigured_raises_error(self):
        """Test that signing without configuration raises error."""
        service = SigningService(None)

        with pytest.raises(NotConfiguredError):
            service.sign_with_timestamp(b"test")


class TestSigningServiceVerification:
    """Tests for signature verification."""

    @pytest.fixture
    def service(self):
        """Create a signing service with a test key."""
        account = Account.create()
        return SigningService(account.key.hex()[2:])

    def test_verify_valid_signature(self, service):
        """Test verification of a valid signature."""
        data = b"test data"
        data_hash, timestamp, signature, address = service.sign_with_timestamp(data)

        is_valid = SigningService.verify_signature(
            data_hash, timestamp, signature, address
        )

        assert is_valid is True

    def test_verify_wrong_address_fails(self, service):
        """Test verification fails with wrong address."""
        data = b"test data"
        data_hash, timestamp, signature, _ = service.sign_with_timestamp(data)

        wrong_address = "0x" + "0" * 40

        is_valid = SigningService.verify_signature(
            data_hash, timestamp, signature, wrong_address
        )

        assert is_valid is False

    def test_verify_tampered_hash_fails(self, service):
        """Test verification fails if hash is tampered."""
        data = b"test data"
        data_hash, timestamp, signature, address = service.sign_with_timestamp(data)

        tampered_hash = "0" * 64

        is_valid = SigningService.verify_signature(
            tampered_hash, timestamp, signature, address
        )

        assert is_valid is False

    def test_verify_tampered_timestamp_fails(self, service):
        """Test verification fails if timestamp is tampered."""
        data = b"test data"
        data_hash, timestamp, signature, address = service.sign_with_timestamp(data)

        tampered_timestamp = "2099-12-31T23:59:59+00:00"

        is_valid = SigningService.verify_signature(
            data_hash, tampered_timestamp, signature, address
        )

        assert is_valid is False

    def test_verify_invalid_signature_fails(self, service):
        """Test verification fails with invalid signature."""
        data = b"test data"
        data_hash, timestamp, _, address = service.sign_with_timestamp(data)

        invalid_signature = "0x" + "0" * 130

        is_valid = SigningService.verify_signature(
            data_hash, timestamp, invalid_signature, address
        )

        assert is_valid is False

    def test_verify_signature_without_0x_prefix(self, service):
        """Test verification works with signature without 0x prefix."""
        data = b"test data"
        data_hash, timestamp, signature, address = service.sign_with_timestamp(data)

        # Remove 0x prefix if present
        sig_without_prefix = signature[2:] if signature.startswith("0x") else signature

        is_valid = SigningService.verify_signature(
            data_hash, timestamp, sig_without_prefix, address
        )

        assert is_valid is True


class TestGetSigningService:
    """Tests for get_signing_service singleton."""

    def test_get_signing_service_returns_service(self, monkeypatch):
        """Test that get_signing_service returns a SigningService instance."""
        # Reset singleton
        import app.services.signing as signing_module
        signing_module._signing_service = None

        service = get_signing_service()

        assert isinstance(service, SigningService)

    def test_get_signing_service_returns_same_instance(self, monkeypatch):
        """Test that get_signing_service returns singleton."""
        import app.services.signing as signing_module
        signing_module._signing_service = None

        service1 = get_signing_service()
        service2 = get_signing_service()

        assert service1 is service2
