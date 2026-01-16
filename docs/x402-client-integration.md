# x402 Client Integration Guide

This document describes how to integrate x402 payment support into CLI tools and MCP servers that interact with the Swarm Connect gateway.

## Overview

When the gateway has `X402_ENABLED=true`, protected endpoints require payment via the x402 protocol. Clients must:

1. Detect HTTP 402 responses
2. Parse payment requirements
3. Sign a payment authorization
4. Retry with `X-PAYMENT` header

## Protected Endpoints

These endpoints require payment (when x402 is enabled and free tier exhausted):

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/stamps/` | Purchase postage stamps |
| POST | `/api/v1/data/` | Upload data to Swarm |
| POST | `/api/v1/data/manifest` | Upload TAR as manifest |

**Free endpoints** (never require payment):
- `GET /api/v1/stamps/` - List stamps
- `GET /api/v1/stamps/{id}` - Get stamp details
- `GET /api/v1/data/{ref}` - Download data

## HTTP 402 Response Format

When payment is required, the gateway returns:

```http
HTTP/1.1 402 Payment Required
Content-Type: application/json

{
  "x402Version": 1,
  "accepts": [
    {
      "scheme": "exact",
      "network": "base-sepolia",
      "maxAmountRequired": "10000",
      "resource": "http://gateway.example.com/api/v1/stamps/",
      "description": "Stamp purchase",
      "payTo": "0x1234567890abcdef1234567890abcdef12345678",
      "maxTimeoutSeconds": 300,
      "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
      "extra": {
        "name": "USDC",
        "decimals": 6
      }
    }
  ],
  "error": "X-PAYMENT header is required"
}
```

### Key Fields

| Field | Description |
|-------|-------------|
| `maxAmountRequired` | Price in smallest units (USDC has 6 decimals, so 10000 = $0.01) |
| `network` | Blockchain network (`base-sepolia` for testnet, `base` for mainnet) |
| `payTo` | Gateway's receiving wallet address |
| `asset` | USDC contract address on the network |

## Payment Flow

### Using the x402 Python SDK

```python
from x402.client import X402Client

# Initialize client with wallet
client = X402Client(
    private_key="0xYOUR_PRIVATE_KEY",
    network="base-sepolia",
)

# Make request - client handles 402 automatically
response = client.post(
    "https://gateway.example.com/api/v1/stamps/",
    json={"amount": 1000000, "depth": 17}
)

# Response is the final result after payment
print(response.json())
```

### Manual Implementation

If implementing without the SDK:

```python
import requests
import json
from eth_account import Account
from eth_account.messages import encode_typed_data

def make_paid_request(url, method="POST", data=None, private_key=None):
    """Make a request with x402 payment handling."""

    # Step 1: Initial request
    response = requests.request(method, url, json=data)

    # Step 2: Check if payment required
    if response.status_code != 402:
        return response

    # Step 3: Parse payment requirements
    req_data = response.json()
    payment_req = req_data["accepts"][0]

    # Step 4: Create payment authorization (EIP-3009 transferWithAuthorization)
    account = Account.from_key(private_key)

    authorization = {
        "from": account.address,
        "to": payment_req["payTo"],
        "value": payment_req["maxAmountRequired"],
        "validAfter": 0,
        "validBefore": int(time.time()) + 300,  # 5 min validity
        "nonce": os.urandom(32).hex(),
    }

    # Step 5: Sign the authorization
    # (Actual signing requires EIP-712 typed data - see x402 SDK)
    signature = sign_eip3009_authorization(authorization, private_key)

    # Step 6: Create payment payload
    payment_payload = {
        "x402Version": 1,
        "scheme": "exact",
        "network": payment_req["network"],
        "payload": {
            "signature": signature,
            "authorization": authorization,
        }
    }

    # Step 7: Encode and retry
    import base64
    payment_header = base64.b64encode(
        json.dumps(payment_payload).encode()
    ).decode()

    return requests.request(
        method, url, json=data,
        headers={"X-PAYMENT": payment_header}
    )
```

## CLI Integration Requirements

A CLI tool should:

### 1. Store Wallet Configuration

```bash
# Config file: ~/.swarm-connect/config.yaml
x402:
  enabled: true
  private_key_env: "SWARM_CONNECT_PRIVATE_KEY"  # Read from env var
  network: "base-sepolia"
  auto_pay: true  # Automatically pay without prompting
  max_auto_pay_usd: 1.00  # Max amount to auto-pay
```

### 2. Handle 402 Responses

```python
def upload_data(file_path, stamp_id):
    response = api.post("/api/v1/data/", files={"file": open(file_path)})

    if response.status_code == 402:
        payment_req = response.json()
        price_usd = int(payment_req["accepts"][0]["maxAmountRequired"]) / 1_000_000

        if config.auto_pay and price_usd <= config.max_auto_pay_usd:
            # Auto-pay
            response = make_paid_request(...)
        else:
            # Prompt user
            if click.confirm(f"Payment required: ${price_usd:.4f} USDC. Pay?"):
                response = make_paid_request(...)
            else:
                raise click.Abort("Payment declined")

    return response
```

### 3. Provide Wallet Commands

```bash
# Setup wallet
swarm-connect wallet setup

# Check balance
swarm-connect wallet balance

# Show configured address
swarm-connect wallet address
```

### 4. Show Payment Status

```bash
$ swarm-connect upload myfile.txt --stamp-id abc123
Uploading myfile.txt...
Payment required: $0.05 USDC
Paying... ✓
Upload complete: bzzr://xyz789...
```

## MCP Server Integration

For MCP (Model Context Protocol) servers, x402 support enables AI agents to make paid requests.

### MCP Tool Definition

```json
{
  "name": "swarm_upload",
  "description": "Upload data to Swarm (may require x402 payment)",
  "inputSchema": {
    "type": "object",
    "properties": {
      "data": {"type": "string", "description": "Data to upload"},
      "stamp_id": {"type": "string", "description": "Stamp ID to use"},
      "allow_payment": {"type": "boolean", "default": false}
    }
  }
}
```

### MCP Handler

```python
async def handle_swarm_upload(data: str, stamp_id: str, allow_payment: bool = False):
    response = await gateway_client.post(
        "/api/v1/data/",
        json={"data": data},
        params={"stamp_id": stamp_id}
    )

    if response.status_code == 402:
        if not allow_payment:
            return {
                "error": "payment_required",
                "price_usd": parse_price(response),
                "message": "Set allow_payment=true to authorize payment"
            }

        # Make paid request
        response = await make_paid_request(...)

    return {"reference": response.json()["reference"]}
```

### Budget Controls

MCP servers should implement budget controls:

```python
class X402Budget:
    def __init__(self, max_per_request: float, max_per_session: float):
        self.max_per_request = max_per_request
        self.max_per_session = max_per_session
        self.session_spent = 0.0

    def can_spend(self, amount_usd: float) -> bool:
        if amount_usd > self.max_per_request:
            return False
        if self.session_spent + amount_usd > self.max_per_session:
            return False
        return True

    def record_spend(self, amount_usd: float):
        self.session_spent += amount_usd
```

## Free Tier Handling

When `X402_FREE_TIER_ENABLED=true` on the gateway:

- Requests without payment may succeed (within rate limits)
- Response includes `X-Payment-Mode: free-tier` header
- Rate limit headers: `X-RateLimit-Limit`, `X-RateLimit-Remaining`

CLI should detect free tier:

```python
if response.headers.get("X-Payment-Mode") == "free-tier":
    remaining = response.headers.get("X-RateLimit-Remaining", "?")
    print(f"Free tier request ({remaining} remaining)")
```

## Dependencies

Required packages for x402 client:

```
x402>=0.1.0
eth-account>=0.8.0
web3>=6.0.0
```

Or just:
```
x402  # Includes all dependencies
```

## Testing

### Mock Mode

For testing without real payments:

```python
class MockX402Client:
    """Mock client that simulates x402 flow without blockchain."""

    def post(self, url, **kwargs):
        # Simulate successful payment
        response = requests.post(url, **kwargs)
        if response.status_code == 402:
            # Pretend we paid
            response = requests.post(
                url, **kwargs,
                headers={"X-PAYMENT": "mock-payment-header"}
            )
        return response
```

### Testnet Mode

Use Base Sepolia for integration testing:
- Free testnet USDC from [Circle Faucet](https://faucet.circle.com/)
- No real money involved

## Error Handling

| Error | Cause | Resolution |
|-------|-------|------------|
| 402 Payment Required | No payment or free tier exhausted | Make x402 payment |
| 429 Too Many Requests | Free tier rate limit | Wait or make payment |
| "Insufficient balance" | Wallet lacks USDC | Fund wallet |
| "Invalid signature" | Wrong private key or network | Check configuration |
| "Payment verification failed" | Facilitator rejected | Check payment amount |

## References

- [x402 Protocol Specification](https://x402.org/spec)
- [x402 Python SDK](https://pypi.org/project/x402/)
- [EIP-3009: Transfer With Authorization](https://eips.ethereum.org/EIPS/eip-3009)
- [Gateway Operator Guide](./x402-operator-guide.md)
- [Testing Guide](./x402-testing-guide.md)
