# x402 Gateway Operator Guide

This guide explains how to set up and operate the x402 payment integration for the Swarm Connect gateway.

## Overview

x402 enables pay-per-request access to the gateway without requiring user accounts. Users pay in USDC on Base chain, and the gateway uses those funds to purchase stamps and upload data to Swarm.

**Current Status**: Testnet only (Base Sepolia)

## Architecture

```
┌──────────────┐     USDC      ┌──────────────┐     xBZZ      ┌──────────────┐
│    Client    │ ────────────> │   Gateway    │ ────────────> │  Swarm Bee   │
│  (Base chain)│               │  (2 wallets) │               │ (Gnosis chain)│
└──────────────┘               └──────────────┘               └──────────────┘
```

The gateway operates with two separate wallets:
1. **Base wallet**: Receives USDC payments from x402
2. **Gnosis wallet**: Holds xBZZ for stamp purchases and xDAI for gas

There is no automatic bridging - you manually manage both treasuries.

## Prerequisites

### Testnet Setup

1. **Base Sepolia Wallet**
   - Create a new wallet or use existing
   - This receives USDC payments
   - Save the address for `X402_PAY_TO_ADDRESS`

2. **Get Testnet USDC**
   - Visit [Circle USDC Faucet](https://faucet.circle.com/)
   - Select "Base Sepolia" network
   - Request USDC (1 USDC every 2 hours)
   - This is for testing client payments

3. **Gnosis Wallet** (for Swarm)
   - Your existing Bee node wallet
   - Needs xBZZ for stamps and xDAI for gas
   - The gateway reads balances from your Bee node API

## Configuration

### Environment Variables

Add these to your `.env` file:

```bash
# === x402 Core ===
X402_ENABLED=true                                    # Enable x402 payments
X402_FACILITATOR_URL=https://x402.org/facilitator   # Testnet facilitator
X402_PAY_TO_ADDRESS=0xYourBaseSepoliaAddress        # Your USDC receiving wallet
X402_NETWORK=base-sepolia                            # Network identifier

# === Pricing ===
X402_BZZ_USD_RATE=0.50               # Manual BZZ/USD rate
X402_MARKUP_PERCENT=50               # 50% markup on BZZ cost
X402_MIN_PRICE_USD=0.01              # Minimum $0.01 per request

# === Thresholds (warnings) ===
X402_XBZZ_WARN_THRESHOLD=10          # Warn if xBZZ wallet < 10
X402_XDAI_WARN_THRESHOLD=0.5         # Warn if xDAI wallet < 0.5
X402_CHEQUEBOOK_WARN_THRESHOLD=5     # Warn if chequebook < 5 xBZZ

# === Limits ===
X402_MAX_STAMP_BZZ=5                 # Max 5 BZZ per stamp purchase
X402_RATE_LIMIT_PER_IP=10            # 10 requests/minute per IP

# === Access Control ===
X402_BLACKLIST_IPS=                  # Comma-separated: 192.168.1.100,10.0.0.50
X402_WHITELIST_IPS=127.0.0.1         # Free access for these IPs

# === Audit ===
X402_AUDIT_LOG_PATH=logs/x402_audit.jsonl
```

### Disabling x402

To run the gateway without x402 (original behavior):

```bash
X402_ENABLED=false
```

All endpoints will work as before with no payment required.

## Pricing Model

The gateway calculates prices as follows:

```
Final Price = (BZZ Cost × BZZ_USD_RATE × (1 + MARKUP_PERCENT/100))
```

Example for a 24-hour stamp at depth 17:
- BZZ cost: 0.1 BZZ (from chainstate)
- Exchange rate: $0.50/BZZ
- Markup: 50%
- Final price: 0.1 × $0.50 × 1.5 = $0.075

The minimum price (`X402_MIN_PRICE_USD`) ensures you always cover costs.

## Access Control

### Whitelist (Free Access)

IPs in the whitelist bypass x402 payment entirely:

```bash
X402_WHITELIST_IPS=192.168.1.100,10.0.0.1
```

Use for:
- Internal services
- Trusted partners
- Development/testing

### Blacklist (Blocked)

IPs in the blacklist receive 403 Forbidden:

```bash
X402_BLACKLIST_IPS=203.0.113.50
```

Use for:
- Known bad actors
- Abuse prevention

## Audit Logs

All x402 transactions are logged to `logs/x402_audit.jsonl` (JSON lines format).

Each log entry includes:
- Timestamp
- Event type (payment_received, stamp_purchased, error, etc.)
- Client IP
- Wallet address (if available)
- Event-specific data

### Reading Audit Logs

```bash
# View recent events
tail -f logs/x402_audit.jsonl | jq .

# Count successful payments
grep '"event_type":"payment_received"' logs/x402_audit.jsonl | wc -l

# Sum revenue (requires jq)
cat logs/x402_audit.jsonl | jq -s '[.[] | select(.event_type=="payment_received") | .data.amount_usd] | add'
```

## Monitoring

### Pre-flight Checks

The gateway performs pre-flight checks before accepting payments:

1. **xBZZ Balance**: Can we afford to buy stamps?
2. **xDAI Balance**: Do we have gas for transactions?
3. **Chequebook Balance**: Can we pay for bandwidth?

If any check fails below the configured threshold:
- **Below threshold but above minimum**: Log warning, accept payment
- **Below minimum**: Reject with "Service temporarily unavailable"

### Check Gateway Status

```bash
# Health check
curl http://localhost:8000/

# Check wallet balances (existing endpoint)
curl http://localhost:8000/api/v1/wallet

# View available stamps
curl http://localhost:8000/api/v1/stamps/
```

## Treasury Management

Since there's no automatic bridging, you need to manually manage:

### Base Wallet (USDC receipts)
- Monitor USDC balance on Base Sepolia
- Periodically withdraw to exchange or bridge to Gnosis

### Gnosis Wallet (xBZZ spending)
- Monitor xBZZ and xDAI via Bee node API
- Top up xBZZ when below threshold
- Top up xDAI for gas

### Recommended Flow
1. Accumulate USDC on Base
2. Periodically swap USDC → xDAI on Base or bridge to Gnosis
3. Swap xDAI → xBZZ on Gnosis as needed
4. Fund Bee node wallet

## Troubleshooting

### "Service temporarily unavailable"

Gateway wallet is below minimum thresholds:
1. Check wallet balances: `curl http://localhost:8000/api/v1/wallet`
2. Top up xBZZ or xDAI on Gnosis

### "Payment verification failed"

Facilitator rejected the payment:
1. Check client has sufficient USDC
2. Verify `X402_PAY_TO_ADDRESS` is correct
3. Check facilitator status at x402.org

### Payments not settling

1. Check facilitator URL is correct
2. Verify network configuration matches client
3. Check audit logs for error details

### High error rate

1. Review audit logs for patterns
2. Check if rate limiting is too aggressive
3. Verify Bee node is healthy

## Network Details

### Base Sepolia (Testnet)
- Chain ID: 84532
- RPC: https://sepolia.base.org
- USDC: `0x036CbD53842c5426634e7929541eC2318f3dCF7e`
- Explorer: https://sepolia.basescan.org

### Base Mainnet (Future)
- Chain ID: 8453
- USDC: `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`
- Explorer: https://basescan.org

## Resources

- [x402 Official Documentation](https://x402.gitbook.io/x402)
- [x402 Python SDK](https://pypi.org/project/x402/)
- [Circle USDC Faucet](https://faucet.circle.com/)
- [Base Sepolia Faucet](https://www.coinbase.com/faucets/base-sepolia-faucet)
- [Parent Issue](https://github.com/datafund/provenance-fellowship/issues/23)
