# Notary Signing Guide

This guide covers the provenance signing feature for all audiences: users, client developers, and gateway operators.

## Table of Contents

- [Overview](#overview)
- [For Users](#for-users)
- [For Client Developers](#for-client-developers)
- [For Gateway Operators](#for-gateway-operators)

---

## Overview

The notary signing feature allows the gateway to add a cryptographic signature to uploaded documents, proving that the data existed at a specific point in time.

**Key benefits:**
- **Timestamp proof**: The gateway's authoritative timestamp proves when data was uploaded
- **Data integrity**: The signature covers a hash of your data, detecting any modifications
- **Verifiable**: Anyone can verify the signature using standard Ethereum tools

---

## For Users

### What is Provenance Signing?

When you upload a document with `sign=notary`, the gateway:
1. Validates your document has a `data` field
2. Creates a SHA-256 hash of the data
3. Adds its cryptographic signature with a timestamp
4. Stores the signed document in Swarm

This proves your data existed at the specified time, signed by the gateway.

### Quick Start

**1. Check if notary is available:**
```bash
curl https://gateway.example.com/api/v1/notary/info
```

Response when enabled:
```json
{
  "enabled": true,
  "available": true,
  "address": "0x1234...abcd",
  "message": "Notary signing is available. Use sign=notary on upload."
}
```

**2. Prepare your document:**

Your document must be JSON with a `data` field:
```json
{
  "data": {
    "title": "My Important Document",
    "content": "This is the content to be notarized",
    "created_at": "2026-01-21T12:00:00Z"
  }
}
```

**3. Upload with notary signature:**
```bash
curl -X POST "https://gateway.example.com/api/v1/data/?stamp_id=YOUR_STAMP&sign=notary" \
  -F "file=@document.json"
```

### Understanding the Signed Document

The returned document includes the notary signature:
```json
{
  "data": {
    "title": "My Important Document",
    "content": "This is the content to be notarized",
    "created_at": "2026-01-21T12:00:00Z"
  },
  "signatures": [
    {
      "type": "notary",
      "signer": "0x1Be31A94361a391bBaFB2a4CCd704F57dc04d4bb",
      "timestamp": "2026-01-21T14:30:00+00:00",
      "data_hash": "abc123...def456",
      "signature": "0x...",
      "signed_fields": ["data"]
    }
  ]
}
```

**Signature fields explained:**
- `type`: Always "notary" for gateway signatures
- `signer`: The gateway's Ethereum address (matches `/notary/info`)
- `timestamp`: ISO 8601 timestamp when the document was signed
- `data_hash`: SHA-256 hash of the canonical JSON `data` field
- `signature`: EIP-191 signature of `"data_hash|timestamp"`
- `signed_fields`: Which fields were included in the hash (always `["data"]`)

---

## For Client Developers

### API Reference

#### Check Notary Status

```
GET /api/v1/notary/info
```

Returns notary availability and the public address for verification.

**Response:**
```json
{
  "enabled": true,
  "available": true,
  "address": "0x1Be31A94361a391bBaFB2a4CCd704F57dc04d4bb",
  "message": "Notary signing is available. Use sign=notary on upload."
}
```

#### Upload with Signing

```
POST /api/v1/data/?stamp_id={id}&sign=notary
Content-Type: multipart/form-data

file: <your JSON document>
```

**Request requirements:**
- Document must be valid JSON
- Must have a `data` field at the root level
- `signatures` field is optional (existing signatures are preserved)

**Error responses:**
- `400 NOTARY_NOT_ENABLED`: Set `NOTARY_ENABLED=true` on the gateway
- `400 NOTARY_NOT_CONFIGURED`: Gateway missing `NOTARY_PRIVATE_KEY`
- `400 INVALID_DOCUMENT_FORMAT`: Document missing `data` field or invalid JSON
- `400 INVALID_SIGN_OPTION`: Use `sign=notary`

### Document Structure Specification

**Input document:**
```json
{
  "data": { ... },           // Required: any JSON value
  "signatures": [ ... ]      // Optional: existing signatures
}
```

**Output document:**
```json
{
  "data": { ... },           // Unchanged
  "signatures": [
    // ... existing signatures preserved ...
    {
      "type": "notary",
      "signer": "0x...",
      "timestamp": "2026-01-21T14:30:00+00:00",
      "data_hash": "...",
      "signature": "...",
      "signed_fields": ["data"]
    }
  ]
}
```

### Verifying Signatures

To verify a notary signature in your client:

1. Get the notary's public address from `/api/v1/notary/info`
2. Extract the notary signature from the document
3. Reconstruct the signed message
4. Verify using EIP-191 signature recovery

**Python verification example:**
```python
import json
import hashlib
from eth_account.messages import encode_defunct
from eth_account import Account

def verify_notary_signature(document: dict, expected_address: str) -> bool:
    """
    Verify a notary signature from the gateway.

    Args:
        document: The signed document with signatures array
        expected_address: The notary's Ethereum address from /notary/info

    Returns:
        True if signature is valid, False otherwise
    """
    # Find the notary signature
    signatures = document.get("signatures", [])
    notary_sig = next(
        (s for s in signatures if s.get("type") == "notary"),
        None
    )
    if not notary_sig:
        return False

    # Verify signer matches expected address
    if notary_sig["signer"].lower() != expected_address.lower():
        return False

    # Reconstruct the data hash
    # IMPORTANT: Use canonical JSON (sorted keys, no whitespace)
    data_json = json.dumps(document["data"], sort_keys=True, separators=(',', ':'))
    computed_hash = hashlib.sha256(data_json.encode('utf-8')).hexdigest()

    # Check hash matches
    if computed_hash != notary_sig["data_hash"]:
        return False

    # Reconstruct the signed message
    message = f"{notary_sig['data_hash']}|{notary_sig['timestamp']}"

    # Verify EIP-191 signature
    signable = encode_defunct(text=message)
    signature = notary_sig["signature"]
    if not signature.startswith('0x'):
        signature = f'0x{signature}'

    try:
        recovered = Account.recover_message(signable, signature=signature)
        return recovered.lower() == expected_address.lower()
    except Exception:
        return False

# Usage example
document = {
    "data": {"message": "Hello World"},
    "signatures": [
        {
            "type": "notary",
            "signer": "0x1Be31A94361a391bBaFB2a4CCd704F57dc04d4bb",
            "timestamp": "2026-01-21T14:30:00+00:00",
            "data_hash": "abc123...",
            "signature": "0x...",
            "signed_fields": ["data"]
        }
    ]
}

is_valid = verify_notary_signature(document, "0x1Be31A94361a391bBaFB2a4CCd704F57dc04d4bb")
print(f"Signature valid: {is_valid}")
```

**JavaScript verification example:**
```javascript
const { ethers } = require('ethers');
const crypto = require('crypto');

async function verifyNotarySignature(document, expectedAddress) {
    // Find notary signature
    const signatures = document.signatures || [];
    const notarySig = signatures.find(s => s.type === 'notary');
    if (!notarySig) return false;

    // Check signer
    if (notarySig.signer.toLowerCase() !== expectedAddress.toLowerCase()) {
        return false;
    }

    // Reconstruct data hash (canonical JSON)
    const dataJson = JSON.stringify(document.data, Object.keys(document.data).sort());
    const computedHash = crypto.createHash('sha256').update(dataJson).digest('hex');

    // Verify hash
    if (computedHash !== notarySig.data_hash) {
        return false;
    }

    // Reconstruct signed message
    const message = `${notarySig.data_hash}|${notarySig.timestamp}`;

    // Verify signature
    try {
        const recovered = ethers.verifyMessage(message, notarySig.signature);
        return recovered.toLowerCase() === expectedAddress.toLowerCase();
    } catch {
        return false;
    }
}
```

### Error Handling

Always handle these error codes:

| Code | HTTP Status | Meaning |
|------|-------------|---------|
| `NOTARY_NOT_ENABLED` | 400 | Gateway has `NOTARY_ENABLED=false` |
| `NOTARY_NOT_CONFIGURED` | 400 | Gateway missing `NOTARY_PRIVATE_KEY` |
| `INVALID_DOCUMENT_FORMAT` | 400 | Document invalid JSON or missing `data` field |
| `INVALID_SIGN_OPTION` | 400 | `sign` parameter not "notary" |

---

## For Gateway Operators

### Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `NOTARY_ENABLED` | No | `false` | Master switch for notary feature |
| `NOTARY_PRIVATE_KEY` | Yes (if enabled) | - | Hex-encoded Ethereum private key |

**Example `.env` configuration:**
```bash
# Enable notary signing
NOTARY_ENABLED=true

# Private key (64 hex characters, no 0x prefix)
NOTARY_PRIVATE_KEY=1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef
```

### Generating a Notary Key

Generate a new Ethereum key pair:

```bash
python -c "from eth_account import Account; a = Account.create(); print(f'Private: {a.key.hex()[2:]}\nAddress: {a.address}')"
```

**Output:**
```
Private: 1a2b3c...
Address: 0x1234...
```

Use the private key (without `0x` prefix) as `NOTARY_PRIVATE_KEY`.

**IMPORTANT**: Store the private key securely. Anyone with this key can sign documents as your gateway's notary.

### Security Considerations

1. **Private key storage**
   - Use environment variables or a secrets manager
   - Never commit to version control
   - Restrict file permissions if using `.env` file

2. **Key separation**
   - The notary key is SEPARATE from your x402 payment address
   - Use a dedicated key for notary signing

3. **Key rotation**
   - Plan for periodic key rotation
   - Document your key rotation procedure
   - Consider a grace period where both old and new keys are valid

4. **Access control**
   - Limit who can access the gateway configuration
   - Monitor for unauthorized configuration changes

### Monitoring

The notary service logs signing events:
```
INFO:     Signed document with hash abc123... at 2026-01-21T14:30:00+00:00
```

Monitor these logs for:
- Unusual signing volume
- Errors in signing operations
- Configuration issues

### Troubleshooting

**Notary not available (enabled but not configured):**
```json
{
  "enabled": true,
  "available": false,
  "message": "Notary is enabled but not configured (missing NOTARY_PRIVATE_KEY)."
}
```
Solution: Set `NOTARY_PRIVATE_KEY` in your environment.

**Invalid private key:**
```
ERROR:    Failed to initialize SigningService: Invalid private key
```
Solution: Ensure the key is valid hex (64 characters, no `0x` prefix).

**Key too short:**
Keys shorter than 32 bytes are automatically zero-padded, but it's best practice to use a full 64-character hex key.

---

## Quick Reference

### Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/notary/info` | GET | Check notary availability and get public address |
| `/api/v1/notary/status` | GET | Simplified status for health checks |
| `/api/v1/data/?sign=notary` | POST | Upload with notary signature |

### Signature Format

The signed message format is:
```
{data_hash}|{timestamp}
```

Where:
- `data_hash` = SHA-256 hash of canonical JSON of the `data` field
- `timestamp` = ISO 8601 timestamp (e.g., `2026-01-21T14:30:00+00:00`)

The signature is an EIP-191 personal_sign of this message.
