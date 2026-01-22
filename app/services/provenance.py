# app/services/provenance.py
"""
Document processing service for provenance signing.

Validates JSON documents and adds notary signatures with timestamps.
"""
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.services.signing import (
    SigningService,
    SigningServiceError,
    NotConfiguredError,
    get_signing_service
)

logger = logging.getLogger(__name__)


class ProvenanceError(Exception):
    """Base exception for provenance service errors."""
    pass


class DocumentValidationError(ProvenanceError):
    """Raised when document validation fails."""
    pass


class NotaryNotEnabledError(ProvenanceError):
    """Raised when notary signing is not enabled."""
    pass


@dataclass
class NotarySignature:
    """Represents a notary signature on a document."""
    type: str  # "notary"
    signer: str  # Ethereum address
    timestamp: str  # ISO 8601
    data_hash: str  # SHA-256 of data field
    signature: str  # EIP-191 signature
    hashed_fields: List[str]  # Fields that were hashed to create data_hash
    signed_message_format: str  # Format of the EIP-191 signed message


@dataclass
class SignedDocument:
    """A document with notary signature added."""
    original_data: Any  # The original "data" field content
    signatures: List[Dict[str, Any]]  # Array of signatures including the new one
    raw_json: str  # The complete signed document as JSON string


class ProvenanceService:
    """
    Service for document validation and notary signing.

    Expected input document format:
    {
        "data": { ... },           // Required: content to be signed
        "signatures": [ ... ]      // Optional: existing signatures
    }

    Output document format:
    {
        "data": { ... },
        "signatures": [
            ...,                   // Existing signatures preserved
            {
                "type": "notary",
                "signer": "0x...",
                "timestamp": "2026-01-21T14:00:00Z",
                "data_hash": "abc123...",
                "signature": "def456...",
                "hashed_fields": ["data"],
                "signed_message_format": "{data_hash}|{timestamp}"
            }
        ]
    }
    """

    def __init__(self, signing_service: Optional[SigningService] = None):
        """
        Initialize the provenance service.

        Args:
            signing_service: Optional SigningService instance. If None, uses singleton.
        """
        self._signing_service = signing_service or get_signing_service()

    @property
    def is_available(self) -> bool:
        """Check if provenance signing is available."""
        return settings.NOTARY_ENABLED and self._signing_service.is_configured

    @property
    def notary_address(self) -> Optional[str]:
        """Get the notary public address."""
        return self._signing_service.public_address if self._signing_service.is_configured else None

    def validate_document(self, raw_content: bytes) -> Tuple[Dict[str, Any], Any]:
        """
        Validate a document for signing.

        Args:
            raw_content: Raw bytes of the document

        Returns:
            Tuple of (parsed_document, data_field_value)

        Raises:
            DocumentValidationError: If document is invalid
        """
        # Parse JSON
        try:
            content_str = raw_content.decode('utf-8')
            document = json.loads(content_str)
        except UnicodeDecodeError as e:
            raise DocumentValidationError(f"Document must be valid UTF-8: {e}")
        except json.JSONDecodeError as e:
            raise DocumentValidationError(f"Document must be valid JSON: {e}")

        # Check for required "data" field
        if not isinstance(document, dict):
            raise DocumentValidationError("Document must be a JSON object")

        if "data" not in document:
            raise DocumentValidationError("Document must have a 'data' field")

        # Validate signatures field if present
        if "signatures" in document:
            if not isinstance(document["signatures"], list):
                raise DocumentValidationError("'signatures' field must be an array")

        # Log guidance for SWIP compliance (non-blocking)
        swip_fields = ["content_hash", "provenance_standard", "encryption", "stamp_id"]
        missing_swip = [f for f in swip_fields if f not in document]
        if missing_swip:
            logger.debug(
                f"Document missing optional SWIP fields: {missing_swip}. "
                "Consider using SWIP-37 compliant structure for future compatibility."
            )

        return document, document["data"]

    def sign_document(
        self,
        raw_content: bytes,
        timestamp: Optional[datetime] = None
    ) -> SignedDocument:
        """
        Sign a document with the notary key.

        Args:
            raw_content: Raw bytes of the JSON document
            timestamp: Optional timestamp. If None, uses current UTC time.

        Returns:
            SignedDocument with notary signature added

        Raises:
            NotaryNotEnabledError: If notary signing is not enabled
            DocumentValidationError: If document is invalid
            SigningServiceError: If signing fails
        """
        if not settings.NOTARY_ENABLED:
            raise NotaryNotEnabledError("Notary signing is not enabled. Set NOTARY_ENABLED=true.")

        if not self._signing_service.is_configured:
            raise NotConfiguredError("Notary signing is not configured. Set NOTARY_PRIVATE_KEY.")

        # Validate document
        document, data_value = self.validate_document(raw_content)

        # Serialize the data field for signing (canonical JSON)
        data_bytes = json.dumps(data_value, sort_keys=True, separators=(',', ':')).encode('utf-8')

        # Sign the data
        data_hash, timestamp_iso, signature, signer = self._signing_service.sign_with_timestamp(
            data_bytes,
            timestamp
        )

        # Create signature entry
        notary_signature = {
            "type": "notary",
            "signer": signer,
            "timestamp": timestamp_iso,
            "data_hash": data_hash,
            "signature": signature,
            "hashed_fields": ["data"],  # Fields that were hashed to create data_hash
            "signed_message_format": "{data_hash}|{timestamp}"  # Actual EIP-191 signed message
        }

        # Get existing signatures or create empty array
        existing_signatures = document.get("signatures", [])

        # Create output document - preserve all original fields, update signatures
        output_document = document.copy()
        output_document["signatures"] = existing_signatures + [notary_signature]

        # Serialize to JSON
        output_json = json.dumps(output_document, indent=2)

        logger.info(f"Signed document with hash {data_hash[:16]}... at {timestamp_iso}")

        return SignedDocument(
            original_data=data_value,
            signatures=output_document["signatures"],
            raw_json=output_json
        )

    def verify_notary_signature(
        self,
        raw_content: bytes,
        expected_signer: Optional[str] = None
    ) -> Tuple[bool, Optional[str]]:
        """
        Verify a notary signature on a document.

        Args:
            raw_content: Raw bytes of the signed document
            expected_signer: Optional expected signer address. If None, uses current notary.

        Returns:
            Tuple of (is_valid, error_message)
        """
        try:
            document, data_value = self.validate_document(raw_content)
        except DocumentValidationError as e:
            return False, str(e)

        signatures = document.get("signatures", [])
        if not signatures:
            return False, "No signatures found in document"

        # Find notary signature
        notary_sigs = [s for s in signatures if s.get("type") == "notary"]
        if not notary_sigs:
            return False, "No notary signature found in document"

        # Verify the most recent notary signature
        sig = notary_sigs[-1]

        # Get expected signer
        signer_to_check = expected_signer or self.notary_address
        if not signer_to_check:
            return False, "No expected signer address available"

        # Check signer matches
        if sig.get("signer", "").lower() != signer_to_check.lower():
            return False, f"Signer mismatch: expected {signer_to_check}, got {sig.get('signer')}"

        # Re-hash the data field
        data_bytes = json.dumps(data_value, sort_keys=True, separators=(',', ':')).encode('utf-8')
        computed_hash = SigningService.hash_data(data_bytes)

        # Check hash matches
        if computed_hash != sig.get("data_hash"):
            return False, f"Data hash mismatch: document may have been modified"

        # Verify signature
        is_valid = SigningService.verify_signature(
            sig.get("data_hash", ""),
            sig.get("timestamp", ""),
            sig.get("signature", ""),
            signer_to_check
        )

        if not is_valid:
            return False, "Signature verification failed"

        return True, None


# Singleton instance (lazy initialization)
_provenance_service: Optional[ProvenanceService] = None


def get_provenance_service() -> ProvenanceService:
    """
    Get the provenance service singleton.

    Returns:
        ProvenanceService instance
    """
    global _provenance_service
    if _provenance_service is None:
        _provenance_service = ProvenanceService()
    return _provenance_service
