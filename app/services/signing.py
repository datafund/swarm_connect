# app/services/signing.py
"""
Core signing infrastructure for notary/provenance features.

Implements EIP-191 personal message signing for document notarization.
The gateway signs documents with an authoritative timestamp to provide
proof that data existed at a specific point in time.
"""
import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional, Tuple

from eth_account import Account
from eth_account.messages import encode_defunct

from app.core.config import settings

logger = logging.getLogger(__name__)


class SigningServiceError(Exception):
    """Base exception for signing service errors."""
    pass


class NotConfiguredError(SigningServiceError):
    """Raised when signing is attempted without proper configuration."""
    pass


class SigningService:
    """
    Service for EIP-191 message signing.

    Uses the gateway's notary private key to sign data with timestamps,
    providing cryptographic proof of document existence at a specific time.

    The signing process:
    1. Hash the data (SHA-256)
    2. Create message: "data_hash|timestamp"
    3. Sign with EIP-191 personal_sign

    Clients can verify signatures using the public address from /notary/info.
    """

    def __init__(self, private_key: Optional[str] = None):
        """
        Initialize the signing service.

        Args:
            private_key: Hex-encoded private key (without 0x prefix).
                        If None, uses settings.NOTARY_PRIVATE_KEY.
        """
        self._private_key = private_key or settings.NOTARY_PRIVATE_KEY
        self._account: Optional[Account] = None

        if self._private_key:
            try:
                # Normalize the key: remove 0x prefix if present
                key_hex = self._private_key[2:] if self._private_key.startswith('0x') else self._private_key

                # Pad to 64 hex chars (32 bytes) if necessary
                key_hex = key_hex.zfill(64)

                # Add 0x prefix for eth_account
                key_with_prefix = f'0x{key_hex}'
                self._account = Account.from_key(key_with_prefix)
                logger.info(f"SigningService initialized with address: {self._account.address}")
            except Exception as e:
                logger.error(f"Failed to initialize SigningService: {e}")
                raise SigningServiceError(f"Invalid private key: {e}")

    @property
    def is_configured(self) -> bool:
        """Check if the signing service is properly configured."""
        return self._account is not None

    @property
    def public_address(self) -> Optional[str]:
        """Get the public address derived from the private key."""
        return self._account.address if self._account else None

    def _hash_data(self, data: bytes) -> str:
        """
        Create SHA-256 hash of data.

        Args:
            data: Raw bytes to hash

        Returns:
            Hex-encoded hash string (without 0x prefix)
        """
        return hashlib.sha256(data).hexdigest()

    def _create_signing_message(self, data_hash: str, timestamp: str) -> str:
        """
        Create the message to be signed.

        Format: "data_hash|timestamp"
        This binds the data hash to the timestamp, preventing timestamp manipulation.

        Args:
            data_hash: SHA-256 hash of the data
            timestamp: ISO 8601 timestamp string

        Returns:
            Message string to be signed
        """
        return f"{data_hash}|{timestamp}"

    def sign_with_timestamp(
        self,
        data: bytes,
        timestamp: Optional[datetime] = None
    ) -> Tuple[str, str, str, str]:
        """
        Sign data with an authoritative timestamp.

        Args:
            data: Raw bytes to sign
            timestamp: Optional timestamp. If None, uses current UTC time.

        Returns:
            Tuple of (data_hash, timestamp_iso, signature, signer_address)

        Raises:
            NotConfiguredError: If signing service is not configured
            SigningServiceError: If signing fails
        """
        if not self.is_configured:
            raise NotConfiguredError("Signing service not configured. Set NOTARY_PRIVATE_KEY.")

        # Generate timestamp if not provided
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)

        timestamp_iso = timestamp.isoformat()

        # Hash the data
        data_hash = self._hash_data(data)

        # Create signing message
        message = self._create_signing_message(data_hash, timestamp_iso)

        try:
            # EIP-191 personal_sign
            signable = encode_defunct(text=message)
            signed = self._account.sign_message(signable)
            signature = signed.signature.hex()

            logger.debug(f"Signed data hash {data_hash[:16]}... at {timestamp_iso}")

            return data_hash, timestamp_iso, signature, self._account.address

        except Exception as e:
            logger.error(f"Signing failed: {e}")
            raise SigningServiceError(f"Failed to sign data: {e}")

    @staticmethod
    def verify_signature(
        data_hash: str,
        timestamp: str,
        signature: str,
        expected_address: str
    ) -> bool:
        """
        Verify an EIP-191 signature.

        This is a static method for client-side verification.

        Args:
            data_hash: SHA-256 hash of the original data
            timestamp: ISO 8601 timestamp string
            signature: Hex-encoded signature
            expected_address: Expected signer address

        Returns:
            True if signature is valid and from expected address
        """
        try:
            # Reconstruct the message
            message = f"{data_hash}|{timestamp}"

            # Verify signature
            signable = encode_defunct(text=message)

            # Ensure signature has 0x prefix
            sig_with_prefix = signature if signature.startswith('0x') else f'0x{signature}'

            recovered = Account.recover_message(signable, signature=sig_with_prefix)

            return recovered.lower() == expected_address.lower()

        except Exception as e:
            logger.warning(f"Signature verification failed: {e}")
            return False

    @staticmethod
    def hash_data(data: bytes) -> str:
        """
        Public method to hash data (for verification purposes).

        Args:
            data: Raw bytes to hash

        Returns:
            Hex-encoded SHA-256 hash
        """
        return hashlib.sha256(data).hexdigest()


# Singleton instance (lazy initialization)
_signing_service: Optional[SigningService] = None


def get_signing_service() -> SigningService:
    """
    Get the signing service singleton.

    Returns:
        SigningService instance

    Note:
        The service may not be configured (is_configured=False)
        if NOTARY_PRIVATE_KEY is not set. Check is_configured before use.
    """
    global _signing_service
    if _signing_service is None:
        _signing_service = SigningService()
    return _signing_service
