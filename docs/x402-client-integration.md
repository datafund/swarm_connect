# x402 Client Integration Guide

This document describes how to integrate x402 payment support into CLI tools and MCP servers that interact with the Swarm Connect gateway.

## Protocol Version

**This gateway implements x402 protocol version 1 (v1).**

Key v1 characteristics:
- Uses string network identifiers: `"base-sepolia"`, `"base"` (not CAIP-2 format like `eip155:84532`)
- Response includes `"x402Version": 1`
- Payment scheme: `"exact"` with EIP-3009 `transferWithAuthorization`
- Asset: USDC on Base chain

Clients should check the `x402Version` field in 402 responses to confirm compatibility.

## Overview

When the gateway has `X402_ENABLED=true`, protected endpoints require payment via the x402 protocol. Clients must:

1. Detect HTTP 402 responses
2. Parse payment requirements
3. Sign a payment authorization
4. Retry with `X-PAYMENT` header
5. Treat any **2xx** as success. `POST /api/v1/stamps/` answers **201 Created**, and uploads answer 200. A client that checks only for 200 treats a successful purchase as a failure and may pay a second time.

Runnable versions of the samples in this guide are in [`docs/samples/`](samples/):

| Sample | What it does |
|--------|--------------|
| [`x402_client.py`](samples/x402_client.py) | Python: buy a stamp, upload, download. Free tier, or `--paid` with the `x402` SDK |
| [`x402_client.mjs`](samples/x402_client.mjs) | The same in Node.js (18+). `--paid` uses the `x402` npm package |
| [`x402_curl.sh`](samples/x402_curl.sh) | curl + jq: show the 402, then the free-tier round trip |

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

When payment is required, the gateway returns a 402 whose body is the x402
payment request. **Where the body sits depends on the gateway version:**

- The x402 v1 spec puts `x402Version`, `accepts`, `error` and `freeTier` at the **top level** (shown below). Standard x402 clients read it there.
- Current gateway releases nest the same object under `detail`: `{"detail": {"x402Version": 1, "accepts": [...], ...}}`.

Read the top level first and fall back to `detail`. That works with both:

```python
body = response.json()
accepts = body["accepts"] if "accepts" in body else body["detail"]["accepts"]
```

The spec shape:

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
        "version": "2"
      }
    }
  ],
  "error": "Payment required. Use X-PAYMENT header for paid access or X-Payment-Mode: free for free tier.",
  "freeTier": {
    "available": true,
    "requestsRemaining": 5,
    "requestsLimit": 5,
    "windowSeconds": 60,
    "instruction": "Add header 'X-Payment-Mode: free' to use free tier"
  }
}
```

### Key Fields

| Field | Description |
|-------|-------------|
| `maxAmountRequired` | Price in smallest units (USDC has 6 decimals, so 10000 = $0.01) |
| `network` | Blockchain network (`base-sepolia` for testnet, `base` for mainnet) |
| `payTo` | Gateway's receiving wallet address |
| `asset` | USDC contract address on the network |

The price is at least the gateway's minimum (`X402_MIN_PRICE_USD`, $0.01 by
default), so sign exactly `maxAmountRequired`, never less.

## Payment Flow

### Using the x402 Python SDK

The SDK is `x402` 1.0.0 (`pip install "x402==1.0.0"`, which is the version the
gateway pins). There is no `X402Client` class. The pieces a client needs are:

- `x402.clients.requests.x402_requests(account)`: a `requests.Session` that answers 402s automatically. It parses the 402 at the **top level**, so it only works with gateways that return the spec shape. Against a gateway that nests the body under `detail`, it raises `PaymentError`.
- `x402.clients.base.x402Client(account).create_payment_header(requirements, 1)`: signs one payment. Use this when you handle the 402 yourself, which works with both shapes.

Automatic, for spec-shape gateways:

```python
from eth_account import Account
from x402.clients.requests import x402_requests

session = x402_requests(Account.from_key(PRIVATE_KEY), max_value=100_000)  # refuse > $0.10
response = session.post(
    "https://gateway.example.com/api/v1/stamps/",
    json={"size": "small", "duration_hours": 25},
)
assert response.status_code == 201
```

### Handling the 402 yourself

This works with both 402 shapes. It is the core of
[`samples/x402_client.py`](samples/x402_client.py):

```python
import requests
from x402.clients.base import x402Client
from x402.types import PaymentRequirements

USDC_UNITS = 1_000_000  # "10000" means $0.01


def payment_requirements(resp):
    body = resp.json()
    return body["accepts"] if "accepts" in body else body["detail"]["accepts"]


def paid_request(method, url, account, max_usd=0.10, **kwargs):
    resp = requests.request(method, url, **kwargs)
    if resp.status_code != 402:
        resp.raise_for_status()          # any 2xx is success (purchase is 201)
        return resp

    requirement = payment_requirements(resp)[0]
    price = int(requirement["maxAmountRequired"]) / USDC_UNITS
    if price > max_usd:
        raise RuntimeError(f"price ${price} above budget ${max_usd}")

    header = x402Client(account).create_payment_header(PaymentRequirements(**requirement), 1)
    resp = requests.request(method, url, headers={"X-PAYMENT": header}, **kwargs)
    resp.raise_for_status()
    return resp
```

`create_payment_header` fills in the EIP-3009 authorization (payer, `payTo`,
`maxAmountRequired`, validity window, random nonce) and signs it with the USDC
EIP-712 domain from `extra`. Each call makes a new nonce. Never reuse a header.

### Node.js

With the `x402` npm package (1.x speaks x402 v1) and `viem`:

```js
import { createPaymentHeader } from "x402/client";
import { createSigner } from "x402/types";

const body = await res.json();                       // the 402
const requirement = (body.accepts ?? body.detail.accepts)[0];
const signer = await createSigner(requirement.network, process.env.X402_PRIVATE_KEY);
const header = await createPaymentHeader(signer, 1, requirement);
res = await fetch(url, { ...init, headers: { ...init.headers, "X-PAYMENT": header } });
if (!res.ok) throw new Error(`HTTP ${res.status}`);  // 2xx is success
```

See [`samples/x402_client.mjs`](samples/x402_client.mjs) for the full flow.

### Request bodies

- **Stamp purchase** (`POST /api/v1/stamps/`): send `size` (`small`, `medium` or `large`) or `depth`, and `duration_hours` (at least 24). The gateway computes the amount. The legacy `amount` field is in PLUR per chunk, and small values produce a batch that expires almost at once, so avoid it.
- **Upload** (`POST /api/v1/data/?stamp_id=...`): a `multipart/form-data` body with the content in a field named `file`. A JSON body such as `{"data": "..."}` is rejected with 422.

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
    with open(file_path, "rb") as f:          # binary, multipart field "file"
        content = f.read()
    files = {"file": (os.path.basename(file_path), content)}
    params = {"stamp_id": stamp_id}
    response = api.post("/api/v1/data/", params=params, files=files)

    if response.status_code == 402:
        requirement = payment_requirements(response)[0]   # top level or detail
        price_usd = int(requirement["maxAmountRequired"]) / 1_000_000

        if not (config.auto_pay and price_usd <= config.max_auto_pay_usd):
            if not click.confirm(f"Payment required: ${price_usd:.4f} USDC. Pay?"):
                raise click.Abort()
        header = x402Client(account).create_payment_header(PaymentRequirements(**requirement), 1)
        response = api.post("/api/v1/data/", params=params, files=files,
                            headers={"X-PAYMENT": header})

    response.raise_for_status()                # any 2xx is success
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
    # Uploads are multipart with the content in the "file" field (not JSON).
    files = {"file": ("data.txt", data.encode(), "text/plain")}
    params = {"stamp_id": stamp_id, "content_type": "text/plain"}
    response = await gateway_client.post("/api/v1/data/", params=params, files=files)

    if response.status_code == 402:
        requirement = payment_requirements(response)[0]   # top level or detail
        if not allow_payment:
            return {
                "error": "payment_required",
                "price_usd": int(requirement["maxAmountRequired"]) / 1_000_000,
                "message": "Set allow_payment=true to authorize payment"
            }
        header = x402Client(account).create_payment_header(PaymentRequirements(**requirement), 1)
        response = await gateway_client.post("/api/v1/data/", params=params, files=files,
                                             headers={"X-PAYMENT": header})

    response.raise_for_status()                # any 2xx is success
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

When `X402_FREE_TIER_ENABLED=true` on the gateway, clients can choose between paid and free access. A request with neither `X-PAYMENT` nor `X-Payment-Mode: free` gets HTTP 402 with both the payment requirements and the free tier availability. A client that already knows it wants the free tier can send `X-Payment-Mode: free` on the first request.

### Client Decision Flow

```
1. Make request to protected endpoint
   ↓
2. Receive HTTP 402 with:
   - Payment requirements (accepts[])
   - Free tier info (freeTier{})
   ↓
3. Client chooses:
   a) Paid: Retry with X-PAYMENT header
   b) Free: Retry with X-Payment-Mode: free header
```

### Using Free Tier

To use free tier, add the `X-Payment-Mode: free` header:

```python
# Check 402 response for free tier availability
if response.status_code == 402:
    data = response.json()
    data = data if "accepts" in data else data["detail"]   # top level or detail
    free_tier = data.get("freeTier", {})

    if free_tier.get("available") and free_tier.get("requestsRemaining", 0) > 0:
        # Use free tier
        response = requests.post(
            url,
            headers={"X-Payment-Mode": "free"},
            **kwargs
        )
    else:
        # Must pay - no free tier or exhausted
        response = paid_request("POST", url, account, **kwargs)
```

### Free Tier Response

Successful free tier responses (2xx: 201 for a stamp purchase, 200 for an upload) include rate limit headers:

```
HTTP/1.1 201 Created
X-Payment-Mode: free-tier
X-RateLimit-Limit: 5
X-RateLimit-Remaining: 4
X-RateLimit-Reset: 60
```

CLI should display free tier status:

```python
if response.headers.get("X-Payment-Mode") == "free-tier":
    remaining = response.headers.get("X-RateLimit-Remaining", "?")
    limit = response.headers.get("X-RateLimit-Limit", "?")
    print(f"Free tier request ({remaining}/{limit} remaining)")
```

### Rate Limit Exceeded (429)

When free tier rate limit is exhausted (the body is under `detail`):

```json
{
  "detail": {
    "error": "Rate limit exceeded",
    "detail": "Rate limit exceeded (free tier): 6/5 requests per minute",
    "message": "Free tier rate limit exceeded. Use x402 payment for higher limits.",
    "payment_info": {
      "price_usd": 0.01,
      "network": "base-sepolia",
      "pay_to": "0x..."
    }
  }
}
```

### Recommended Client Logic

```python
def smart_request(url, prefer_free=True, **kwargs):
    """Make request with intelligent payment/free tier handling."""
    response = requests.post(url, **kwargs)

    if response.status_code != 402:
        return response

    data = response.json()
    data = data if "accepts" in data else data["detail"]   # top level or detail
    free_tier = data.get("freeTier", {})

    # Try free tier first if available and preferred
    if prefer_free and free_tier.get("available") and free_tier.get("requestsRemaining", 0) > 0:
        response = requests.post(url, headers={"X-Payment-Mode": "free"}, **kwargs)
        if response.ok:              # 2xx: a purchase answers 201, not 200
            return response
        if response.status_code != 429:
            response.raise_for_status()  # a real error: paying would not help
        # Free tier just ran out: fall through to payment

    # Make paid request
    return paid_request("POST", url, account, **kwargs)
```

## Dependencies

Required packages for x402 client:

Python:

```
x402==1.0.0     # pulls in eth-account; the gateway speaks x402 v1
requests
```

Node.js (18+):

```
npm install x402@1 viem
```

## Testing

### Running the samples

Point the samples at a local gateway (see the [Testing Guide](./x402-testing-guide.md)):

```bash
python docs/samples/x402_client.py --gateway http://localhost:8000            # free tier
X402_PRIVATE_KEY=0x... python docs/samples/x402_client.py --gateway http://localhost:8000 --paid
node docs/samples/x402_client.mjs http://localhost:8000 [--paid]
docs/samples/x402_curl.sh http://localhost:8000
```

Each one buys a stamp, waits for it to become usable, uploads a small file,
downloads it and compares the bytes. It exits non-zero on any failure, so it
can be used as a smoke test. `--paid` spends real (testnet or mainnet) USDC
through the gateway's facilitator. `--max-usd` (Python) and `MAX_USD` (Node)
cap what it will sign.

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
