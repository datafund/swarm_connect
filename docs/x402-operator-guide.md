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
X402_NETWORK=base-sepolia                            # base-sepolia or base (or eip155:84532 / eip155:8453)
X402_PROTOCOL_VERSIONS=1                             # 1 (default), 1,2 or 2: the x402 versions accepted (see below)

# === Pricing ===
X402_BZZ_USD_RATE=0.50               # Manual BZZ/USD rate
X402_MARKUP_PERCENT=50               # 50% markup on BZZ cost
X402_MIN_PRICE_USD=0.01              # Minimum $0.01 per request
X402_BANDWIDTH_USD_PER_GB=0.10       # Paid uploads and chunk credit, per GB of bandwidth

# === Gnosis Wallet Thresholds (warnings) ===
X402_XBZZ_WARN_THRESHOLD=10          # Warn if xBZZ wallet < 10
X402_XDAI_WARN_THRESHOLD=0.5         # Warn if xDAI wallet < 0.5
X402_CHEQUEBOOK_WARN_THRESHOLD=5     # Warn if chequebook < 5 xBZZ

# === Base Wallet Monitoring ===
X402_BASE_ETH_WARN_THRESHOLD=0.005   # Warn if Base ETH < 0.005 (~50 txs)
X402_BASE_ETH_CRITICAL_THRESHOLD=0.001  # Block if Base ETH < 0.001 (~10 txs)

# === Limits ===
X402_MAX_STAMP_BZZ=5                 # Max 5 BZZ per stamp purchase
# X402_RATE_LIMIT_PER_IP is not enforced. Paid requests are bounded by the global limiter
# (RATE_LIMIT_PER_MINUTE + RATE_LIMIT_BURST) once it runs alongside x402 (#401).

# === Free Tier ===
X402_FREE_TIER_ENABLED=true          # Enable rate-limited free tier (default: true)
X402_FREE_TIER_RATE_LIMIT=3          # Free tier requests per minute per IP (default: 3)

# === Access Control ===
X402_BLACKLIST_IPS=                  # Comma-separated: 192.168.1.100,10.0.0.50
# X402_WHITELIST_IPS has no effect (no payment allowlist; see Access Control)

# === Audit ===
X402_AUDIT_LOG_PATH=logs/x402_audit.jsonl
```

### x402 protocol versions (`X402_PROTOCOL_VERSIONS`)

Which x402 versions a payment is accepted in. `/health` reports the result under `x402.versions`.

| Value | Accepts | Refuses |
|---|---|---|
| `1` (default) | x402 v1 payments in `X-PAYMENT` | `PAYMENT-SIGNATURE` with 402 `X402_VERSION_UNSUPPORTED` |
| `1,2` | v1 in `X-PAYMENT` and v2 in `PAYMENT-SIGNATURE`, each settled in its own version | both headers in one request (400 `PAYMENT_HEADER_CONFLICT`) |
| `2` | v2 in `PAYMENT-SIGNATURE` only | `X-PAYMENT` with 402 `X402_VERSION_UNSUPPORTED` |

Any other value (empty, `3`, `1,1`, `v2`) stops the gateway at startup with a settings error that names `X402_PROTOCOL_VERSIONS`. Whitespace and order do not matter (`2, 1` is `1,2`).

With `1` the gateway behaves exactly as before: same 402 bodies, same facilitator requests, same receipts, same state files.

With 2 enabled:

- A v2 payment is checked before the facilitator is called. Its `accepted` offer must equal the gateway's own offer for that request, field by field (scheme `exact`, the CAIP-2 network such as `eip155:84532`, asset, amount, payTo, maxTimeoutSeconds, extra). The signed authorization must pay that offer (`to` is payTo, `value` is the amount). The gateway sends its own offer and its own resource description to the facilitator, never the client's copy. Refusals are 400 `PAYMENT_HEADER_TOO_LARGE`, `PAYMENT_HEADER_MALFORMED` or `PAYMENT_HEADER_INVALID`, and 402 `X402_VERSION_UNSUPPORTED`, `PAYMENT_SCHEME_UNSUPPORTED`, `PAYMENT_METHOD_UNSUPPORTED` (Permit2) or `PAYMENT_OFFER_MISMATCH` (see [error-codes.md](error-codes.md)). None of them charges anything.
- Every 402 that offers a payment also carries the `PAYMENT-REQUIRED` header (#608): base64 of the x402 v2 `PaymentRequired`, with the same offers as the body in v2 form (CAIP-2 network, `amount`), the body's `error` and the `resource`. v2 clients such as the SDK read the header and pay with v2. With `1,2` the JSON body stays the v1 body, so v1 clients such as the current CLI keep paying with v1 against the same gateway. With `2` alone the body is the JSON copy of the v2 `PaymentRequired` (`x402Version` 2, `error`, `resource`, `accepts`), plus the usual `code`, `message` and other fields, at the top level and under `detail`. A 402 that offers no payment (a chunk-credit refusal, say) has no header.
- If the header would be over 8192 characters (a very long description, for example), or the offer cannot be written in v2, the 402 is sent without it and with the v1 body (with `2` alone too, so v1 clients can still pay it), a warning is logged, and `gateway_x402_payment_required_header_failures_total{reason="too_large"|"invalid"}` counts it. It is never a 500. v2 clients cannot pay that 402; v1 clients still can with `1,2`. The series exist from startup when 2 is enabled, and should stay at 0.
- `/verify` and `/settle` are sent with `x402Version: 2`. The facilitator must settle v2 `exact` on the configured network, which the startup check below makes sure of.
- A v2 settlement answers with the `PAYMENT-RESPONSE` header, a v1 settlement with `X-PAYMENT-RESPONSE`. Never both.
- A v2 settle answer is a definite refusal only when it is a 4xx with `success: false`. A 5xx, or any answer the gateway cannot read, is an unknown outcome (502, the result withheld), as a timeout is.
- A v2 verify answer counts only when it is a 2xx, or a 4xx with `isValid: false`. Anything else (a 5xx even if it says `isValid: true`) is answered 502, nothing charged.
- The v2 receipt (`PAYMENT-RESPONSE`) carries `success`, `transaction`, `network`, `payer`, `errorReason` and `amount` only; the facilitator's `extra`, `extensions` and `extensionResponses` are not passed on.
- One signed authorization counts once across both versions. Re-wrapping it in the other version's header is refused like any replay. An `Idempotency-Key` result stored for one version is not served to a request paying in the other: it gets 409 `IDEMPOTENCY_KEY_VERSION_MISMATCH`, unpaid.
- `gateway_x402_settlements_total{version="2"}` exists from startup for every result, so the "x402 settlement problems" alert sees the first v2 problem after a restart.

Switching `PAYMENT-REQUIRED` on switches v2-capable clients to v2, and switching back to `1` moves them back to v1: the header is what they follow. `2` alone shuts out v1 clients (the current CLI). The deploy configuration for this setting is #609; until then set it on the host.

#### Startup check: the facilitator must settle v2

With x402 enabled and 2 in `X402_PROTOCOL_VERSIONS`, the gateway calls the facilitator's `GET /supported` once at startup, with the configured facilitator credentials (CDP or the bearer token). It starts only if the answer is a 2xx and lists a kind with `x402Version` 2, scheme `exact` and the configured network as a CAIP-2 id (`eip155:84532` for `base-sepolia`, `eip155:8453` for `base`). A v2 kind listed under the v1 name (`base-sepolia`) does not count. The deploy check (`python -m app.x402.check_config`) runs the same call, so a deploy with such a facilitator fails before the running gateway is replaced. With `1`, or with x402 disabled, `/supported` is never called.

The check fails closed. Every failure stops startup with a message that begins `Refusing to start with x402 enabled: X402_PROTOCOL_VERSIONS includes 2, but` and ends with `fix the facilitator, or set X402_PROTOCOL_VERSIONS=1`. Messages name the facilitator host and what it lists, never a credential:

| Message continues with | Cause | What to do |
|---|---|---|
| `the facilitator on <host> does not list x402Version 2, scheme exact, network eip155:84532 in /supported (it lists v2 exact for [...]; for eip155:84532, in either name form: [...])` | The facilitator does not settle v2 `exact` on this network. The two lists show what it does settle. | Use a facilitator that settles v2 on the network, or set `X402_PROTOCOL_VERSIONS=1`. |
| `the facilitator's /supported on <host> could not be reached (ConnectError)` (or `ReadTimeout`, `ConnectTimeout`, ...) | No answer within 5 seconds, or no connection. | Check the facilitator URL and the network path from the gateway to it. |
| `the request to the facilitator's /supported on <host> could not be made (RuntimeError; ...)` (or `InvalidURL`, ...) | The request could not be sent at all: the CDP credential hook failed (a malformed key, say), or the facilitator URL is not a valid URL. The error text itself is not shown, since it may contain a credential. | Check the facilitator URL and the credentials. |
| `the facilitator's /supported on <host> answered HTTP 401, not a list of payment kinds (a 401 or 403 is an authentication error)` (or any other non-2xx status) | Any answer that is not 2xx, whatever its body: an authentication error (CDP answers 401 without valid credentials), a 404 for a wrong path, a 5xx from a facilitator in trouble. Kinds in a non-2xx body are never trusted. | For 401 or 403, check the credentials; otherwise the facilitator URL and the facilitator's state. |
| `the facilitator's /supported on <host> answered HTTP 200 with something that is not a list of payment kinds (ValidationError)` (or `JSONDecodeError`, `TypeError`) | A 2xx answer of another shape, such as an HTML page. | Check the facilitator URL. |

**What failing closed costs.** A facilitator outage does not stop a running gateway. But if the gateway restarts (a deploy, a crash, a host reboot) while the facilitator's `/supported` is unreachable, the gateway stays down, free tier and downloads included, until the facilitator answers again. The container restart policy keeps retrying, and the check passes as soon as `/supported` answers. To bring the gateway up during a facilitator outage, set `X402_PROTOCOL_VERSIONS=1` and recreate the container. It then starts without calling `/supported`, advertises and accepts v1 only, and v2 clients move to v1. Set it back to `1,2` once the facilitator answers.

Whatever the setting, an `X-PAYMENT` whose payload claims an `x402Version` other than 1 is refused with 400 `PAYMENT_HEADER_INVALID` before the facilitator (#639).

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

**Uploads are priced as bandwidth, not storage.** A paid `POST /api/v1/data/`
(or `/data/manifest`) writes with a stamp the caller supplies, so storage is
already paid for. The price is:

```
Priced Bytes = ceil(Content-Length × (data + parity) / data)
Upload Price = max(Priced Bytes / 10^9 × X402_BANDWIDTH_USD_PER_GB × (1 + MARKUP_PERCENT/100), X402_MIN_PRICE_USD)
```

`(data + parity) / data` is the erasure-coding overhead of the upload's
redundancy level (#429). Bee splits the chunks into groups and adds parity
chunks to each group, and it pushes those chunks to the network too. The
level is the request's `redundancy` query parameter. Without one, the level
is the default, 2, because the gateway always asks Bee for a level. A value
the endpoint refuses is priced at the default, and the request is then
refused without a charge.

| Level | Name | Data chunks per full group | Parity chunks | Factor |
|-------|------|---------------------------|---------------|--------|
| 0 | none | - | 0 | 1.000 |
| 1 | medium | 119 | 9 | 1.076 |
| 2 | strong (default) | 107 | 21 | 1.196 |
| 3 | insane | 97 | 31 | 1.320 |
| 4 | paranoid | 39 | 89 | 3.282 |

The counts are from Bee v2.8.1, `pkg/file/redundancy/level.go` (`mediumEt`,
`strongEt`, `insaneEt`, `paranoidEt`), and are set in
`REDUNDANCY_ERASURE_GROUP` in `app/services/swarm_api.py`. If you upgrade Bee,
check that these tables have not changed.

Limitations of the factor:

- **Small files are underpriced.** The factor is the overhead of a full
  group, which is the smallest overhead Bee adds. A file of a few chunks, and
  the last partial group of a larger file, get more parity per data chunk. For
  example, one data chunk at level 2 gets 4 parity chunks. Bee also stores the
  root chunk as 2, 4, 8 or 16 extra copies (levels 1-4). The gateway does not
  price these extra bytes. Uploads that small cost the `X402_MIN_PRICE_USD`
  floor unless you raise `X402_BANDWIDTH_USD_PER_GB` by several orders of
  magnitude.
- **The gateway cannot count chunks exactly.** It sees only `Content-Length`,
  which includes the multipart framing. A `/data/manifest` TAR holds several
  files and manifest nodes, each with its own chunk tree.
- **Encrypted uploads are out of scope.** Bee uses different tables for
  encrypted chunks (`encMediumEt` and the others). The gateway does not offer
  encrypted uploads, so it does not use them.

`GET /api/v1/pricing` takes the same `redundancy` parameter and quotes the
same amount the 402 asks for.

A request without `Content-Length` is priced at `MAX_UPLOAD_SIZE_MB`, multiplied by the same factor. With the
defaults ($0.10/GB, 50% markup, 10 MB cap) every upload costs the $0.01
minimum, at every redundancy level. There is no setting for the upload price alone:
`X402_BANDWIDTH_USD_PER_GB` also prices chunk-credit top-ups, and
`X402_MIN_PRICE_USD` also floors stamp purchases. Before #365 an upload cost
the price of a new 24-hour stamp sized to it.

### Keeping the BZZ/USD rate honest (#364)

Every price is computed from `X402_BZZ_USD_RATE`. It is **not** updated automatically. Set it explicitly per environment (GitHub variable `X402_BZZ_USD_RATE`, also `X402_MARKUP_PERCENT` and `X402_MIN_PRICE_USD`) and review it regularly. To be warned when it drifts, set `X402_BZZ_PRICE_FEED_URL`, for example `https://api.coingecko.com/api/v3/simple/price?ids=swarm-bzz&vs_currencies=usd`. The gateway then exports `gateway_bzz_usd_rate_configured` and `gateway_bzz_usd_rate_market`, logs a warning, and the Grafana alert "BZZ pricing rate off market" fires when the configured rate is more than 2× away from the market (above: overcharging; below 0.5×: sales can lose money).

## Access Control

### No payment allowlist

There is no allowlist that bypasses payment. `X402_WHITELIST_IPS` has no effect, and the gateway logs a warning at startup if it is set: an address-based bypass would give paid operations away to anyone who can appear from a listed address. Trusted internal callers should pay, or use the free tier.

### Blacklist (Blocked)

Addresses in the blocklist receive 403 `ACCESS_BLOCKED` on every route, before any other work. IPs and CIDR ranges are accepted; IPv6 clients are best blocked by their /64. The list is read at startup, so a change takes a restart. For an immediate block, use the proxy (see `docs/mainnet-runbook.md`).

```bash
X402_BLACKLIST_IPS=203.0.113.50,2001:db8:1234:5678::/64
```

Use for:
- Known bad actors
- Abuse prevention

## Free Tier

The gateway supports a rate-limited free tier that allows clients to use protected endpoints without payment. When enabled, clients receive information about both payment options and free tier availability in the 402 response.

### Configuration

```bash
# === Free Tier ===
X402_FREE_TIER_ENABLED=true           # Enable free tier (default: true)
X402_FREE_TIER_RATE_LIMIT=3           # Requests per minute for free tier (default: 3)
```

### How It Works

1. **Client makes request** to a protected endpoint without any payment headers
2. **Gateway returns HTTP 402** with:
   - Payment requirements (price, network, address)
   - Free tier info (if enabled): remaining requests, limit, instructions
3. **Client chooses**:
   - **Paid access**: Add `X-PAYMENT` header with signed payment
   - **Free tier**: Add `X-Payment-Mode: free` header

### 402 Response Format

When a client hits a protected endpoint without proper headers:

```json
{
  "x402Version": 1,
  "error": "Payment required. Use X-PAYMENT header for paid access or X-Payment-Mode: free for free tier.",
  "accepts": [
    {
      "scheme": "exact",
      "network": "base-sepolia",
      "maxAmountRequired": "10000",
      "resource": "http://localhost:8000/api/v1/data/",
      "description": "Upload bandwidth (1024 bytes, stored with the stamp you supplied)",
      "mimeType": "application/json",
      "payTo": "0xYourAddress...",
      "maxTimeoutSeconds": 300,
      "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
    }
  ],
  "freeTier": {
    "available": true,
    "requestsRemaining": 5,
    "requestsLimit": 5,
    "windowSeconds": 60,
    "instruction": "Add header 'X-Payment-Mode: free' to use free tier"
  }
}
```

### Client Headers

| Header | Value | Purpose |
|--------|-------|---------|
| `X-PAYMENT` | Base64-encoded payment payload | Use paid tier |
| `X-Payment-Mode` | `free` (or `free-tier`) | Use free tier |

### Free Tier Response Headers

When using free tier, responses include rate limit headers:

```
X-RateLimit-Limit: 3
X-RateLimit-Remaining: 2
X-RateLimit-Reset: 60
X-Payment-Mode: free-tier
```

### Rate Limit Exceeded (429)

When free tier rate limit is exceeded:

```json
{
  "error": "Rate limit exceeded",
  "detail": "Rate limit exceeded (free tier): 4/3 requests per minute",
  "message": "Free tier rate limit exceeded. Use x402 payment for higher limits.",
  "payment_info": {
    "price_usd": 0.01,
    "network": "base-sepolia",
    "pay_to": "0xYourAddress..."
  }
}
```

### Disabling Free Tier

To require payment for all requests:

```bash
X402_FREE_TIER_ENABLED=false
```

When disabled, 402 responses will not include the `freeTier` block, and requests with `X-Payment-Mode: free` will receive a 402 requiring payment.

## Audit Logs

All x402 transactions are logged to `logs/x402_audit.jsonl` (JSON lines format).

Each log entry includes:
- Timestamp
- Event type (payment_received, stamp_purchased, error, etc.)
- Client IP
- Wallet address (if available)
- Event-specific data

The payment records (`payment_settled`, `payment_failed`, `payment_delivered`) include `data.x402_version`, the x402 protocol version of the payment. It is `1` for every payment unless `X402_PROTOCOL_VERSIONS` enables x402 v2; then a `PAYMENT-SIGNATURE` payment is `2`. The metrics have a matching `version` label. On `gateway_x402_payments_total` it is `1`, `2` or `none`, where `none` means the request carried no payment header. On `gateway_x402_settlements_total` it is `1` or `2` and never `none`, because only a payment is settled.

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

### Health Endpoint

The `/health` endpoint provides comprehensive x402 status when enabled:

```bash
curl http://localhost:8000/health | jq .
```

**Example Response:**
```json
{
  "status": "ok",
  "message": "Welcome to Swarm API Aggregator",
  "x402": {
    "enabled": true,
    "versions": [1],
    "base_wallet": {
      "address": "0xYourAddress...",
      "balance_eth": 0.01,
      "threshold_eth": 0.005,
      "critical_eth": 0.001,
      "ok": true,
      "is_critical": false
    },
    "bee_gnosis_wallet": {
      "wallet_address": "0xBeeNodeWallet...",
      "chequebook_address": "0xChequebook...",
      "can_accept": true,
      "xbzz_ok": true,
      "xdai_ok": true,
      "chequebook_ok": true,
      "balances": {...}
    },
    "warnings": [],
    "errors": []
  }
}
```

**Status Values:**
| Status | Meaning | HTTP Code |
|--------|---------|-----------|
| `ok` | Bee node reachable and payments accepted | 200 |
| `degraded` | Something needs attention. Paid requests may be refused. Causes: the Bee node API cannot be reached (`bee_node.api_reachable: false`), the node reports `network_availability: Unavailable`, the xBZZ or xDAI balance cannot be read (`x402.bee_gnosis_wallet.can_accept: false`, paid requests get `503 PAYMENTS_UNAVAILABLE`), payment state cannot be written, there is no snapshot yet (just after startup), or the snapshot is too old to trust | 200 |
| `critical` | A blocking error in `x402.errors`. Nothing sets one today; the SDK's `health()` reports false only for this value | 200 |

An unreachable Bee node is `degraded` and not `critical` on purpose (#108, #589): the SDK
treats `critical` as "gateway down", and a Bee outage is usually short. Monitors that
must tell "gateway alive" from "Bee reachable" should read `bee_node.healthy`. It is also
false when the health snapshot is too old to trust (its refreshes keep failing): the Bee
state is then unknown, `bee_node.api_reachable` is null, and a warning says so.

A low balance that could still be read is a warning only and does not change `status`.

### Pre-flight Checks

The gateway performs pre-flight checks before accepting payments:

**Gnosis Wallet (for Swarm operations):**
1. **xBZZ Balance**: Can we afford to buy stamps?
2. **xDAI Balance**: Do we have gas for transactions?
3. **Chequebook Balance**: Can we pay for bandwidth?

**Base Wallet (for x402 payments):**
4. **ETH Balance**: Does facilitator have gas for USDC transfers?

**Threshold Behavior:**
- **Below warning threshold**: Log warning, accept payments. `status` does not change.
- **Balance cannot be read** (xBZZ or xDAI; #589): `can_accept` is false, `status` is
  `degraded`, and every paid request (and every request that would be offered a 402)
  gets `503 PAYMENTS_UNAVAILABLE` before payment, so that request is not charged. A
  retry of an attempt that may have been charged, answered from stored state (a stored
  result or a 409), is not refused by this check, though its pricing can still fail while
  Bee is down. Free-tier
  requests still run. An unreadable chequebook balance alone is a warning only.
- **Snapshot too old to trust**: the checks come from the `/health` snapshot (#588). When
  its refreshes keep failing and it is older than `HEALTH_SNAPSHOT_MAX_TRUSTED_AGE_SECONDS`
  (default 60, never less than TTL + refresh timeout), paid requests are refused the same
  way and `status` is `degraded`.

The check reads the cached snapshot, so it adds no Bee call per request. It can lag by
up to `HEALTH_SNAPSHOT_TTL_SECONDS` (15 s): just after Bee recovers, paid requests can
still be refused for that long. Just after Bee fails, a paid request can still pass and
fail after settlement, which leads to the usual refund path. If Bee fails by not
answering at all, refreshes take until their timeout to fail, so this window can last
until the snapshot is too old to trust: up to `HEALTH_SNAPSHOT_MAX_TRUSTED_AGE_SECONDS`
(60 s), not only one TTL.

**Edge case:** the gate keys on Bee's `/wallet` endpoint. If `/wallet` fails while the rest
of Bee still works (uploads succeed), every paid route is refused, including paid uploads
that do not spend xBZZ. `/health` shows it as `can_accept: false` with a "Failed to fetch
xBZZ balance" warning while `bee_node.api_reachable` is true. Check the Bee node's wallet
API (`curl <bee>/wallet` from the gateway host); free-tier uploads keep working meanwhile.

### Check Gateway Status

```bash
# Health check with x402 status
curl http://localhost:8000/health | jq .

# Check Gnosis wallet balances
curl http://localhost:8000/api/v1/wallet

# View available stamps
curl http://localhost:8000/api/v1/stamps/
```

## Treasury Management

Since there's no automatic bridging, you need to manually manage:

### Base Wallet (USDC receipts + ETH for gas)
- Monitor USDC balance on Base Sepolia
- **Monitor ETH balance** - needed for facilitator to execute USDC transfers
- Top up ETH when `/health` shows `degraded` or `critical`
- Periodically withdraw USDC to exchange or bridge to Gnosis

**ETH Thresholds (Base Sepolia):**
| Level | Threshold | Runway | Action |
|-------|-----------|--------|--------|
| Warning | 0.005 ETH | ~50 transactions | Top up soon |
| Critical | 0.001 ETH | ~10 transactions | Gateway blocks requests |

### Gnosis Wallet (xBZZ spending)
- Monitor xBZZ and xDAI via Bee node API or `/health` endpoint
- Top up xBZZ when below threshold
- Top up xDAI for gas

### Recommended Flow
1. Accumulate USDC on Base
2. Periodically swap USDC → xDAI on Base or bridge to Gnosis
3. Swap xDAI → xBZZ on Gnosis as needed
4. Fund Bee node wallet

## Troubleshooting

### Health endpoint shows `degraded` or `critical` status

The health endpoint always returns HTTP 200 so the gateway stays reachable — check the
`status` field in the JSON response, then `bee_node.api_reachable`, `bee_node.healthy`,
`snapshot.warnings` and `x402.bee_gnosis_wallet.can_accept`. If `can_accept` is false,
paid requests are getting `503 PAYMENTS_UNAVAILABLE` until the Bee wallet can be read again.

1. Check health endpoint: `curl http://localhost:8000/health | jq .`
2. Look at `x402.base_wallet.is_critical` and `x402.bee_gnosis_wallet.can_accept`
3. If Base ETH is critical: Top up ETH on Base Sepolia
4. If Gnosis balances are low: Top up xBZZ or xDAI on Gnosis

### "Service temporarily unavailable" (legacy)

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
