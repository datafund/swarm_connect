# Chunk Forwarding Guide (pre-stamped uploads + prepaid bandwidth)

**Flow A** lets a client that **controls its own postage stamp** (e.g. an AI agent) upload **pre-stamped chunks** through the gateway. The client stamps each chunk locally; the gateway is a thin forwarder to the Bee node's `POST /chunks`. The client's postage batch pays for **storage**; the gateway bills only for **bandwidth**.

> The client must own a postage batch it can sign with. It can buy one itself on Gnosis, or (future, Flow B / #225) have the gateway create a batch owned by the client's address.

---

## Operator guide

### Enabling

The feature is **off by default**. The router is always mounted but returns `404` until enabled.

| Variable | Default | Purpose |
|----------|---------|---------|
| `CHUNK_UPLOAD_ENABLED` | `false` | Master switch for the whole feature. |
| `CHUNK_UPLOAD_MAX_BYTES_PER_REQUEST` | `4104` | Hard cap per chunk body (8-byte span + 4096 payload). |
| `CHUNK_UPLOAD_FREE_TIER_ENABLED` | `true` | Allow free uploads within a daily quota. |
| `CHUNK_UPLOAD_FREE_TIER_MB_PER_DAY` | `100` | Free bytes per IP per UTC day. |
| `X402_BANDWIDTH_USD_PER_GB` | `0.10` | Price per GB used to convert a top-up payment into credit. |
| `BANDWIDTH_CREDIT_MIN_TOPUP_MB` | `100` | Minimum top-up so one payment clears the price floor. |
| `BANDWIDTH_CREDIT_STATE_FILE` | `data/bandwidth_credit.json` | Prepaid-credit ledger persistence. |

Billing is governed by the global `X402_ENABLED`:

- **Pure forwarder (unmetered)** — `CHUNK_UPLOAD_ENABLED=true`, `X402_ENABLED=false`. Anyone with a valid pre-stamped chunk can upload; no payment, no credit, no quota.
- **Paid + free** — `CHUNK_UPLOAD_ENABLED=true`, `X402_ENABLED=true`, `CHUNK_UPLOAD_FREE_TIER_ENABLED=true`. Clients either top up bandwidth credit or use the free daily quota.
- **Paid only** — as above with `CHUNK_UPLOAD_FREE_TIER_ENABLED=false`. Free requests get `402`.
- **Disabled** — `CHUNK_UPLOAD_ENABLED=false`. The endpoints return `404`.

These compose independently of the stamp/data x402 free tier (`X402_FREE_TIER_*`).

### How billing works

- A chunk upload carries **no per-request payment**. Only the top-up does.
- `POST /api/v1/chunks/credit?mb=N` is x402-protected: the client pays once (priced from `mb` at `X402_BANDWIDTH_USD_PER_GB`), credit is bound to the **verified x402 payer wallet**, and a **bearer token** is returned.
- Each `POST /api/v1/chunks/` presents that token (`X-Bandwidth-Credit-Token`); the chunk's byte length is debited (atomic; refunded if the Bee forward fails).
- The free tier debits a per-IP daily byte quota instead.

### Monitoring

Exposed on `/metrics` (scraped by Alloy → Grafana Cloud automatically once deployed):

- `gateway_chunk_uploads_total{status, mode}`, `gateway_chunk_upload_bytes_total`
- `gateway_bandwidth_topups_total{status}`, `gateway_bandwidth_topup_bytes_total`
- `gateway_bandwidth_credit_accounts`, `gateway_bandwidth_credit_bytes_total`
- `gateway_info{...,chunk_upload_enabled}`

Dashboard panels are tracked separately (#234).

---

## Client guide

### 1. Have a postage batch you own

You need a postage batch whose **owner key you control**, so you can sign stamps Bee will accept. (Buy one on Gnosis yourself, or use Flow B once available.)

### 2. Stamp chunks locally

Split the payload into Swarm chunks (≤ 4096 bytes of data, with the 8-byte span), compute each chunk address (BMT), and produce a **marshaled postage stamp** per chunk — 113 bytes hex: `batchID[0:32] + index[32:40] + timestamp[40:48] + signature[48:113]`, signed by the batch owner key. A Swarm SDK (e.g. `bee-js`) does this for you.

### 3. (If billing is on) top up bandwidth credit

```bash
# Pay once via x402 for, say, 100 MB of bandwidth. Returns a bearer token.
curl -X POST "$GATEWAY/api/v1/chunks/credit?mb=100" \
     -H "X-PAYMENT: <x402 payment payload>"
# -> { "address": "0x..", "token": "<credit-token>", "credited_bytes": 100000000, "balance_bytes": ... }
```

### 4. Upload chunks

```bash
# Paid: spend prepaid credit
curl -X POST "$GATEWAY/api/v1/chunks/" \
     -H "Content-Type: application/octet-stream" \
     -H "Swarm-Postage-Stamp: <226-hex marshaled stamp>" \
     -H "X-Bandwidth-Credit-Token: <credit-token>" \
     --data-binary @chunk.bin
# -> { "reference": "...", "bytes_charged": 4104, "credit_balance_bytes": ... }

# Free tier (within daily quota)
curl -X POST "$GATEWAY/api/v1/chunks/" \
     -H "Swarm-Postage-Stamp: <stamp>" \
     -H "X-Payment-Mode: free" \
     --data-binary @chunk.bin

# Deferred (faster response, async network sync)
curl -X POST "$GATEWAY/api/v1/chunks/?deferred=true" ...
```

Default is **non-deferred** (direct upload). The gateway does not verify your stamp — an invalid stamp fails at the Bee node (`502` from the gateway).

### Error reference

| Status | `code` | Meaning |
|--------|--------|---------|
| 404 | — | Feature disabled (`CHUNK_UPLOAD_ENABLED=false`). |
| 400 | `MISSING_STAMP` / `INVALID_STAMP` / `EMPTY_CHUNK` | Bad/absent stamp header or empty body. |
| 413 | `CHUNK_TOO_LARGE` | Body exceeds `CHUNK_UPLOAD_MAX_BYTES_PER_REQUEST`. |
| 402 | `CREDIT_REQUIRED` / `INVALID_CREDIT_TOKEN` / `INSUFFICIENT_CREDIT` | Missing/unknown token, or balance too low — top up. |
| 402 | `FREE_TIER_DISABLED` | Free mode requested but disabled. |
| 429 | `FREE_QUOTA_EXCEEDED` | Daily free quota exhausted — top up for more. |
| 502 | — | Bee rejected the chunk (often an invalid stamp) or is unavailable; any debit is refunded. |
