# x402 Local Testing Guide

This guide explains how to test the x402 payment flow locally with real testnet transactions.

## Overview

```
┌─────────────┐     HTTP + X-PAYMENT      ┌─────────────┐
│   Client    │ ───────────────────────►  │   Gateway   │
│  (your app) │                           │ (this repo) │
└─────────────┘                           └──────┬──────┘
       │                                         │
       │                                         │ Verify payment
       │                                         ▼
       │                               ┌─────────────────┐
       │                               │   Facilitator   │
       │                               │  (x402.org)     │
       └───────────────────────────────┴─────────────────┘
                    │
                    │ USDC Transfer (on-chain)
                    ▼
              ┌───────────┐
              │   Base    │
              │  Sepolia  │
              │ (testnet) │
              └───────────┘
```

## What You Need

| Component | Purpose | Where to get it |
|-----------|---------|-----------------|
| **Test wallet** | Sign payments | Any Ethereum wallet (MetaMask, etc.) |
| **Testnet ETH** | Pay gas fees | Base Sepolia faucet |
| **Testnet USDC** | Pay for gateway services | USDC faucet or bridge |
| **Gateway wallet** | Receive payments | Create new wallet for gateway |

## Step-by-Step Setup

### 1. Create Test Wallets

You need TWO wallets:
- **Client wallet**: Makes payments (needs testnet USDC)
- **Gateway wallet**: Receives payments (configured in gateway)

```bash
# Generate a new wallet (or use existing)
# Save the private key securely!

# Option A: Use an existing MetaMask wallet
# Export private key from MetaMask settings

# Option B: Generate new wallet with Python
python -c "from eth_account import Account; a = Account.create(); print(f'Address: {a.address}\nPrivate key: {a.key.hex()}')"
```

### 2. Get Testnet ETH (for gas)

Base Sepolia faucets:
- https://www.alchemy.com/faucets/base-sepolia
- https://faucet.quicknode.com/base/sepolia
- https://docs.base.org/docs/tools/network-faucets/

Request testnet ETH for BOTH wallets (client and gateway).

### 3. Get Testnet USDC

**Option A: Circle Faucet (Recommended)**
1. Go to https://faucet.circle.com/
2. Select "Base Sepolia"
3. Enter your CLIENT wallet address
4. Receive testnet USDC

**Option B: Bridge from another testnet**
If you have Goerli/Sepolia USDC, bridge to Base Sepolia.

**USDC Contract on Base Sepolia:**
```
0x036CbD53842c5426634e7929541eC2318f3dCF7e
```

### 4. Configure the Gateway

Create or edit `.env`:

```bash
# Gateway settings
SWARM_BEE_API_URL=http://localhost:1633

# x402 settings
X402_ENABLED=true
X402_FREE_TIER_ENABLED=false  # Disable for payment testing
X402_PAY_TO_ADDRESS=0xYOUR_GATEWAY_WALLET_ADDRESS
X402_NETWORK=base-sepolia
X402_FACILITATOR_URL=https://x402.org/facilitator

# Pricing (adjust as needed)
X402_BZZ_USD_RATE=0.50
X402_MARKUP_PERCENT=50
X402_MIN_PRICE_USD=0.01
```

### 5. Start the Gateway

```bash
# Activate virtual environment
source venv/bin/activate

# Start gateway
python run.py

# Or with explicit env vars
X402_ENABLED=true X402_PAY_TO_ADDRESS=0x... python run.py
```

### 6. Test Without Payment (verify 402)

```bash
# Should return HTTP 402 with payment requirements
curl -X POST http://localhost:8000/api/v1/stamps/ | jq

# Expected response:
# {
#   "x402Version": 1,
#   "accepts": [{
#     "scheme": "exact",
#     "network": "base-sepolia",
#     "maxAmountRequired": "10000",  # 0.01 USDC
#     "resource": "http://localhost:8000/api/v1/stamps/",
#     "payTo": "0xYOUR_GATEWAY_WALLET",
#     ...
#   }],
#   "error": "X-PAYMENT header is required"
# }
```

### 7. Test With x402 Client

**Install x402 client:**
```bash
pip install x402
```

**Python test script:**
```python
# test_payment.py
import os
from x402.client import X402Client

# Your client wallet private key (with testnet USDC)
PRIVATE_KEY = os.environ.get("TEST_WALLET_PRIVATE_KEY")

# Create client
client = X402Client(
    private_key=PRIVATE_KEY,
    network="base-sepolia",
)

# Make paid request
response = client.post(
    "http://localhost:8000/api/v1/stamps/",
    json={"amount": 1000000, "depth": 17}
)

print(f"Status: {response.status_code}")
print(f"Body: {response.json()}")
```

**Run it:**
```bash
TEST_WALLET_PRIVATE_KEY=0x... python test_payment.py
```

### 8. Run Live Tests

```bash
# Set environment
export TEST_WALLET_PRIVATE_KEY=0x...
export TEST_GATEWAY_URL=http://localhost:8000
export RUN_LIVE_TESTS=1

# Run live tests
pytest tests/test_x402_live.py -v -s
```

## How x402 Payment Flow Works

1. **Client requests protected endpoint** (no payment header)
2. **Gateway returns HTTP 402** with payment requirements:
   - Price in USDC (smallest units, 6 decimals)
   - Network (base-sepolia)
   - Recipient address (gateway wallet)
   - Facilitator URL
3. **Client creates payment authorization** (EIP-3009 signature)
4. **Client retries request** with `X-PAYMENT` header containing signed auth
5. **Gateway verifies** payment with facilitator
6. **Facilitator executes** USDC transfer on-chain
7. **Gateway processes** request and returns response
8. **Response includes** `X-PAYMENT-RESPONSE` header with settlement proof

## Facilitator

**You don't need to register a facilitator.** The public x402.org facilitator handles testnet payments automatically.

- **Testnet facilitator**: `https://x402.org/facilitator`
- **Mainnet facilitator**: `https://x402.org/facilitator` (same URL, detects network)

The facilitator:
- Verifies payment signatures are valid
- Executes the USDC transfer on-chain
- Returns settlement proof to gateway

## Blockchain Connection

**You don't need your own RPC node.** The x402 client and facilitator handle blockchain interactions.

If you want to monitor transactions:
- **Base Sepolia Explorer**: https://sepolia.basescan.org/
- **Check your wallet**: Search your address to see USDC transfers

## Troubleshooting

### "Insufficient USDC balance"
- Get more testnet USDC from faucet
- Check you're on Base Sepolia (not mainnet)

### "Invalid signature"
- Ensure private key matches wallet with USDC
- Check network is "base-sepolia"

### "Facilitator unreachable"
- Check internet connection
- Verify `X402_FACILITATOR_URL` is correct

### "402 but no accepts array"
- Gateway may not be configured correctly
- Check `X402_PAY_TO_ADDRESS` is set

### Gateway returns 200 instead of 402
- Free tier is enabled
- Set `X402_FREE_TIER_ENABLED=false` for payment testing

## Cost Summary

| Item | Cost |
|------|------|
| Testnet ETH | Free (faucet) |
| Testnet USDC | Free (faucet) |
| x402 facilitator | Free |
| Gateway operation | Your electricity |

**Total cost for testing: $0**

## Next Steps

After successful local testing:
1. Deploy gateway to staging environment
2. Test with remote gateway URL
3. Monitor audit logs: `logs/x402_audit.jsonl`
4. Consider mainnet deployment (real USDC)
