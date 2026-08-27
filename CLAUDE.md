# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a FastAPI-based service that provides a simplified API layer for accessing Swarm (EthSwarm Bee) features. The application acts as an aggregator that connects to Swarm Bee nodes and exposes specific functionality through REST endpoints.

## Development Commands

### Setup and Installation
```bash
# Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Setup environment
cp .env.example .env
# Edit .env to configure SWARM_BEE_API_URL
```

### Running the Application
```bash
# Development server (with auto-reload)
python run.py

# Custom port (if 8000 is taken)
PORT=8001 python run.py

# HTTPS development (requires SSL certificates)
SSL_KEYFILE=./localhost+2-key.pem SSL_CERTFILE=./localhost+2.pem python run.py
```

### Testing
```bash
# Run all tests
source venv/bin/activate && python -m pytest tests/ -v

# Run specific test file
python -m pytest tests/test_manifest_upload.py -v
```

## Architecture Overview

### Application Structure
- **FastAPI Application**: Main app defined in `app/main.py` with modular router inclusion
- **Configuration Management**: Centralized in `app/core/config.py` using Pydantic Settings with .env support
- **API Layer**: Organized under `app/api/` with separate endpoints and models
- **Service Layer**: External API integration handled in `app/services/`
- **Models**: Pydantic models for request/response validation in `app/api/models/`
- **Middleware**: Rate limiting in `app/middleware/rate_limit.py` (sliding window per-IP), JSON body limits in `app/middleware/body_limit.py` (depth and size)
- **x402 Module**: Optional payment gateway in `app/x402/` (see x402 section below)

### Key Components

**Core Configuration (`app/core/config.py`)**:
- Uses `pydantic-settings` for environment variable management
- Validates SWARM_BEE_API_URL as proper URL format
- Cached settings object with `@lru_cache()` for performance

**Swarm Integration (`app/services/swarm_api.py`)**:
- Handles HTTP requests to Swarm Bee API (`/batches` endpoint)
- Includes error handling for network issues and malformed responses
- Supports both direct list responses and `{"batches": [...]}` wrapper formats
- `calculate_propagation_signals()`: Computes propagation timing for stamps purchased through this gateway

**Stamp Purchase Tracker (`app/services/stamp_tracker.py`)**:
- In-memory tracker for stamps purchased through this gateway
- Records purchase timestamps to calculate propagation timing signals
- Auto-prunes entries older than 10 minutes to prevent unbounded growth
- Functions: `record_purchase()`, `get_purchase_time()`, `clear_tracker()`

**Gnosis Chain Client (`app/services/gnosis_chain.py`)** — Flow B (#225/#227):
- The gateway's first on-chain WRITE capability: signs/sends ERC20 `approve` + PostageStamp `createBatch` on Gnosis so a batch can be owned by an arbitrary address (Bee's HTTP API always makes the node the owner). Returns the created `batchId` (`keccak256(abi.encode(signer, nonce))`).
- `GnosisChainClient.create_batch(owner, initial_balance_per_chunk, depth, ...)` is async (web3 is sync → runs in `asyncio.to_thread`). Skips `approve` when allowance already covers cost. Uses web3.py (already pulled in by x402; no new heavy dep). Build txs with an explicit nonce + EIP-1559 fees (never set `gasPrice` alongside maxFee).
- Config: `GNOSIS_RPC_URL`, `GNOSIS_PRIVATE_KEY` (SENSITIVE — env/secret, never logged), `GNOSIS_CHAIN_ID` (100 mainnet / 11155111 testnet), optional `POSTAGE_STAMP_CONTRACT_ADDRESS` / `BZZ_TOKEN_ADDRESS` (default per chain id). Drives `POST /api/v1/stamps/for-owner` (#228).
- `get_balances()` (15s cache) + `preflight(required_plur)` (#231) read the signer wallet's xBZZ + xDAI; preflight returns `is_critical` when xDAI is below `GNOSIS_XDAI_CRITICAL_THRESHOLD` (no gas) or xBZZ can't cover the batch — used to refuse `503` before spending, and to drive the `gateway_gnosis_signer_xbzz/xdai_balance` metrics gauges.

**Stamps API (`app/api/endpoints/stamps.py`)**:
- Provides `/api/v1/stamps/{stamp_id}` endpoint
- Fetches all stamps from Swarm and filters by ID
- Calculates expiration time: `current_time + batchTTL`
- Records purchase timestamps for propagation tracking on `POST /stamps/`
- Comprehensive error handling with appropriate HTTP status codes

**Data Models (`app/api/models/stamp.py`)**:
- `StampDetails` model with optional fields to handle missing data from upstream API
- Field aliases for API compatibility (`amount` aliased as `value`, etc.)
- Calculated `expectedExpiration` field in `YYYY-MM-DD-HH-MM` UTC format
- Calculated `utilizationPercent` field showing stamp usage as percentage (0-100%)
- Propagation timing fields: `secondsSincePurchase`, `estimatedReadyAt`, `propagationStatus`
- Access control field: `accessMode` (`"owned"`, `"shared"`, or `null`)

### Environment Configuration

Required environment variables:
- `SWARM_BEE_API_URL`: URL to Swarm Bee node API (e.g., `https://api.gateway.ethswarm.org`)

Optional environment variables:
- `HOST`: Server host (default: `127.0.0.1`)
- `PORT`: Server port (default: `8000`)
- `RELOAD`: Enable auto-reload (default: `true`)
- `SSL_KEYFILE`/`SSL_CERTFILE`: For HTTPS development

Stamp propagation:
- `STAMP_PROPAGATION_SECONDS`: Expected propagation delay after purchase in seconds (default: `120`)

Security settings:
- `MAX_UPLOAD_SIZE_MB`: Maximum file upload size in megabytes (default: `10`)
- `MAX_JSON_BODY_BYTES`: Maximum JSON request body size in bytes (default: `1048576` / 1 MB)
- `MAX_JSON_DEPTH`: Maximum JSON nesting depth (default: `20`)
- `RATE_LIMIT_ENABLED`: Enable per-IP rate limiting (default: `true`)
- `RATE_LIMIT_PER_MINUTE`: Requests per minute per IP (default: `60`)
- `RATE_LIMIT_BURST`: Extra burst capacity above per-minute limit (default: `10`)

Monitoring:
- `METRICS_ENABLED`: Expose `/metrics` Prometheus endpoint (default: `true`)
- `METRICS_BALANCE_POLL_SECONDS`: Wallet balance polling interval (default: `60`)
- `GATEWAY_ENVIRONMENT`: Environment label for metrics (default: `development`)

Notary signing (optional):
- `NOTARY_ENABLED`: Enable notary signing feature (default: `false`)
- `NOTARY_PRIVATE_KEY`: Hex-encoded Ethereum private key for signing (64 characters, no 0x prefix). Generate with `python scripts/generate_notary_key.py`

CORS (browser access):
- `CORS_ALLOWED_ORIGINS`: Allowed origins, `*` for all or comma-separated list (default: `*`)
- `CORS_ALLOW_CREDENTIALS`: Allow credentials in CORS requests (default: `false`)

### API Endpoints

#### Core Endpoints
- `GET /` (and `/health`): Health check. Always includes a `bee_node` section (from Bee `/topology` + `/status` + `/health` + `/addresses` + `/chainstate`, fetched concurrently, 15s cached): identity/build `overlay`, `version`, `api_version`, `bee_status`; connectivity `mode`, `connected_peers`, `population`, `depth`, `reachability`, `network_availability` (Available/Unavailable/Unknown — Bee sets this from outbound-dial results; Unavailable = OS network/host-unreachable on dials); reserve/radius `storage_radius`, `committed_depth`, `reserve_size`, `reserve_size_within_radius`, `pullsync_rate`, `batch_commitment`; chain sync `last_synced_block`, `chain_tip`, `chain_sync_lag_blocks`; plus `warming_up`, `healthy`, `warnings`. Any endpoint that fails yields `null` for its fields rather than losing the whole section. Overall `status` → `degraded` when `network_availability` is `Unavailable` (node can't reach the storer network → uploads may 201 without propagating) — advisory warnings (low peer count `< LOW_PEER_WARN_THRESHOLD`, chain lag `> CHAIN_LAG_WARN_BLOCKS`, non-ok Bee status) never flip `healthy` or `status`. x402 wallet section added when `X402_ENABLED`.

#### Stamp Management
- `POST /api/v1/stamps/`: Purchase new postage stamps (records purchase time for propagation tracking)
- `GET /api/v1/stamps/`: List stamps (default: local only). Supports `?global=true` for all stamps, `?wallet=0x...` for wallet-filtered view (x402)
- `GET /api/v1/stamps/{stamp_id}`: Retrieve specific stamp batch details including propagation timing
- `GET /api/v1/stamps/{stamp_id}/check`: Check stamp health for uploads (errors, warnings, can_upload status, propagation status)
- `PATCH /api/v1/stamps/{stamp_id}/extend`: Extend existing stamps with additional funds
- `POST /api/v1/stamps/for-owner` (Flow B #228/#230): create a postage batch owned by an arbitrary address via `GnosisChainClient.create_batch` (PostageStamp.createBatch on Gnosis), so the owner can sign its own stamps off-node. Body: `owner` (0x, never assumed = payer), `size`/`depth`, `duration_hours`, `immutable`. Returns `batchID` (64-hex, no 0x) + `txHash` + propagation info; records the batch in the ownership registry (`source="created_for_owner"`, informational — on-chain ownership is source of truth). **Spends the gateway's Gnosis funds**, so: OFF by default (`STAMP_PURCHASE_FOR_OTHERS_ENABLED`, router 404s when off); owner **allow-list** (`STAMP_FOR_OTHERS_REQUIRE_WHITELIST` + `_OWNER_WHITELIST`); hard caps `STAMP_FOR_OTHERS_MAX_DEPTH` / `_MAX_BZZ` / `_MAX_DURATION_HOURS` — ALL enforced before any on-chain spend. Plus a signer-wallet **preflight** (#231): refuses `503 SIGNER_INSUFFICIENT_FUNDS` if the gateway can't fund the batch (gas/xBZZ), checked after the caps and before createBatch. **x402 (#229):** mounted WITH the x402 dependency, so when `X402_ENABLED` the caller pays via the `/stamps/` protected prefix (priced from the actual depth/duration by reading the body in `_calculate_price_for_request`); free-tier creation is OFF by default (`STAMP_FOR_OTHERS_FREE_TIER_ENABLED`, else `402 FREE_TIER_DISABLED`). Payer (x402) ≠ owner (`body.owner`). Emits `gateway_for_owner_batches_total{status}` + `_bzz_spent_total` and audits each creation. See `docs/buy-batch-for-owner-guide.md`.

**Stamp list query parameters**:
- `global` (bool): If true, return all stamps including non-local (old behavior)
- `wallet` (string): Filter to stamps accessible by this wallet address (requires x402 enabled)
- `exclusive` (bool): When used with `wallet`, return only stamps purchased by this wallet (excludes shared/free and untracked)

**Propagation timing fields** (included in all stamp responses):
- `secondsSincePurchase`: Seconds elapsed since purchase through this gateway (null for external stamps)
- `estimatedReadyAt`: ISO 8601 timestamp when stamp should be usable (null for external stamps)
- `propagationStatus`: `"ready"` / `"propagating"` / `"unknown"` (null if undetermined)

**Access mode field** (included in all stamp responses):
- `accessMode`: `"owned"` (exclusive to a wallet via x402), `"shared"` (free tier), or `null` (not tracked)

#### Data Operations
- `POST /api/v1/data/?stamp_id={id}&content_type={type}&redundancy={level}`: Upload raw data to Swarm (redundancy 0-4, default 2)
- `POST /api/v1/data/manifest?stamp_id={id}&redundancy={level}`: Upload TAR archive as collection/manifest (15x faster for batch uploads)
- `GET /api/v1/data/{reference}`: Download raw data from Swarm (returns bytes directly)
- `GET /api/v1/data/{reference}/json`: Download data with JSON metadata (base64-encoded)

#### Debug (read-only Bee diagnostics, signature-gated)
- `GET /api/v1/debug/bee/{path}`: read-only proxy to allow-listed Bee endpoints (`topology`, `addresses`, `peers`, `status`, `chainstate`, `reservestate`, `redistributionstate`, `node`, `health`, `readiness`, `stamps`, `batches`, `chequebook`, `wallet`) for diagnosing the gateway's Bee node when you only have gateway access. Disabled (404) unless `DEBUG_ALLOWED_ADDRESSES` (comma-separated 0x addresses) is set. Auth = EIP-191 signature from an allow-listed address over `swarm-connect-debug:<unix_ts>` via headers `X-Debug-Timestamp` + `X-Debug-Signature` (freshness window `DEBUG_SIG_MAX_AGE_SECONDS`, default 300s). No stored secret; never proxies writes.

#### Chunk Forwarding (pre-stamped, Flow A)
- `POST /api/v1/chunks/`: Forward a single **client-supplied pre-stamped** chunk to Bee `POST /chunks`. Raw chunk in the body, marshaled stamp in the `Swarm-Postage-Stamp` header (sent instead of `Swarm-Postage-Batch-Id`); optional `?deferred=true` (default false). The client owns the postage batch and signs locally; the gateway is a thin forwarder and does **not** verify the stamp (Bee does). Always mounted; the handler returns 404 when `CHUNK_UPLOAD_ENABLED=false`.
- `POST /api/v1/chunks/credit?mb={n}`: x402-paid prepaid **bandwidth credit** top-up. Priced via the `bandwidth` operation in `pricing.py` at `X402_BANDWIDTH_USD_PER_GB` (min `BANDWIDTH_CREDIT_MIN_TOPUP_MB`). Credit is bound to the verified x402 payer wallet; returns a bearer token.
  - **Billing model**: chunk uploads carry no per-request payment. When `X402_ENABLED`, the client tops up once, then presents the bearer token via the `X-Bandwidth-Credit-Token` header on each `POST /chunks/`; the chunk's byte length is debited from the prepaid balance (atomic check-and-debit; refunded if the Bee forward fails). A **free** path is also available: send `X-Payment-Mode: free` to draw from a per-IP daily byte quota (`app/services/bandwidth_free_tier.py`, in-memory, resets per UTC day), returning `429` when exhausted (also refunded on Bee failure). Only `/chunks/credit` is in `PROTECTED_ENDPOINTS`.
  - Ledger: `app/services/bandwidth_credit.py` (`BandwidthCreditManager`), address-keyed balances + `token -> address` index, persisted to `BANDWIDTH_CREDIT_STATE_FILE`.
  - Config: `CHUNK_UPLOAD_ENABLED` (default false), `CHUNK_UPLOAD_MAX_BYTES_PER_REQUEST` (4104), `X402_BANDWIDTH_USD_PER_GB`, `BANDWIDTH_CREDIT_MIN_TOPUP_MB`, `BANDWIDTH_CREDIT_STATE_FILE`, `CHUNK_UPLOAD_FREE_TIER_ENABLED`, `CHUNK_UPLOAD_FREE_TIER_MB_PER_DAY` (free tier is independent of the x402 `X402_FREE_TIER_*` settings).

#### Stamp Pool (Low-Latency Provisioning)
- `GET /api/v1/pool/status`: Get pool status and reserve levels
- `POST /api/v1/pool/acquire`: Acquire stamp from pool instantly (<5 seconds vs >1 minute)
- `GET /api/v1/pool/available`: List available stamps in pool
- `POST /api/v1/pool/check`: Schedule manual pool maintenance. **Operator-only — it spends BZZ.** Requires an EIP-191 signature over `swarm-connect-pool-check:<unix_ts>` from an address in `POOL_ADMIN_ADDRESSES` (headers `X-Debug-Timestamp` + `X-Debug-Signature`), a deliberately separate allow-list from `DEBUG_ALLOWED_ADDRESSES` so a diagnostics signature cannot authorise spending. 404 when the list is empty (the default). Returns `202` and schedules the work — poll `GET /api/v1/pool/status` for the outcome, since a purchase takes ~16s and awaiting it held the caller's connection (#292).

#### Notary Signing (Provenance)
- `GET /api/v1/notary/info`: Check notary availability and get public address for verification
- `GET /api/v1/notary/status`: Simplified notary status for health checks
- `POST /api/v1/data/?sign=notary`: Upload with notary signature (adds `sign` parameter to data upload)

### Dependencies and Tech Stack

- **FastAPI**: Web framework with automatic OpenAPI documentation
- **Uvicorn**: ASGI server with performance extras
- **httpx**: Async HTTP client for Swarm API integration (AsyncClient with connection pooling)
- **Pydantic**: Data validation and settings management
- **python-dotenv**: Environment file loading

### Development Notes

- Tests are implemented using pytest with mocking (see `tests/` directory)
- CORS middleware enabled by default for browser-based SDK usage
- Authentication/authorization placeholder code exists but not implemented
- SSL/HTTPS support built into development server
- Logging configured at INFO level with structured error handling

## x402 Payment Integration

### Overview

The gateway supports x402 payment protocol for pay-per-request access without user accounts. When enabled, clients pay in USDC on Base chain to access stamp purchase and data upload endpoints.

**Current Status**: Available on `dev` branch (testnet only)

**Parent Issue**: [datafund/provenance-fellowship#23](https://github.com/datafund/provenance-fellowship/issues/23)

### Key Architecture Decisions

- **Two-wallet system**: USDC on Base, xBZZ on Gnosis (no bridging)
- **SDK**: Official `x402` Python package (v1 - v2 under development)
- **Facilitator**: x402.org public facilitator for testnet
- **Scope**: Uploads gated, downloads free

### x402 Module Structure

```
app/x402/
├── __init__.py      # Module init
├── middleware.py    # FastAPI middleware for payment verification
├── preflight.py     # Gateway balance checks
├── pricing.py       # Price calculation (BZZ → USD)
├── access.py        # IP whitelist/blacklist
├── audit.py         # Transaction audit logging
└── ratelimit.py     # Per-IP rate limiting
```

### x402 Test Coverage (196 tests)

```
tests/
├── test_x402_preflight.py    # 21 tests - Balance checks
├── test_x402_pricing.py      # 25 tests - Price calculations
├── test_x402_middleware.py   # 39 tests - HTTP middleware + free tier
├── test_x402_access.py       # 36 tests - IP access control
├── test_x402_audit.py        # 29 tests - Audit logging
├── test_x402_ratelimit.py    # 25 tests - Rate limiting
└── test_x402_integration.py  # 21 tests - Full flow tests
```

### Key Configuration

```bash
X402_ENABLED=false           # Master switch (default: off)
X402_FACILITATOR_URL=...     # Payment facilitator
X402_PAY_TO_ADDRESS=0x...    # USDC receiving wallet (Base)
X402_NETWORK=base-sepolia    # Network identifier

# Free tier settings (for users without x402 capability)
X402_FREE_TIER_ENABLED=true  # Allow non-paying users (default: on)
X402_FREE_TIER_RATE_LIMIT=3  # Requests/minute for free tier (default: 3)
```

### Access Modes (when X402_ENABLED=true)

| User Type | Access | Rate Limit | Headers |
|-----------|--------|------------|---------|
| **Paying users** | Full access | 10/min | `X-PAYMENT-RESPONSE` |
| **Free tier** | Limited access | 3/min | `X-Payment-Mode: free-tier` |
| **Whitelisted IPs** | Full access | No limit | - |
| **Blacklisted IPs** | Blocked | - | 403 |

### Protected Endpoints (when X402_ENABLED=true)

- `POST /api/v1/stamps/` - Requires payment OR free tier
- `POST /api/v1/data/` - Requires payment OR free tier
- `POST /api/v1/data/manifest` - Requires payment OR free tier
- `GET /api/v1/data/{ref}` - FREE (no payment required)

### Free Tier Behavior

When `X402_FREE_TIER_ENABLED=true` (default):
- Users without x402 payment can still access protected endpoints
- Stricter rate limit applied (3 requests/minute by default)
- Response includes `X-Payment-Mode: free-tier` header
- When rate limit exceeded, returns 429 with payment upgrade info

When `X402_FREE_TIER_ENABLED=false`:
- Users without payment get HTTP 402 immediately
- Must provide valid x402 payment to access protected endpoints

### Development Notes

- x402 code is on `dev` branch - test on staging before merging to `main`
- Python SDK is v1 only (v2 under development)
- All x402 transactions logged to `logs/x402_audit.jsonl`

### x402 Documentation

| Document | Purpose |
|----------|---------|
| `docs/x402-operator-guide.md` | Gateway operator setup and configuration |
| `docs/x402-testing-guide.md` | Local testing with testnet wallets |
| `docs/x402-client-integration.md` | CLI and MCP client integration guide |

### Testing x402

```bash
# With x402 disabled (default behavior)
X402_ENABLED=false python run.py

# With x402 enabled (requires facilitator)
X402_ENABLED=true X402_PAY_TO_ADDRESS=0x... python run.py

# Run x402 unit tests (mocked)
python -m pytest tests/test_x402_*.py -v

# Run live tests (requires testnet setup)
RUN_LIVE_TESTS=1 pytest tests/test_x402_live.py -v
```

### Future Work: CLI/MCP Integration

The x402 **server** side is complete. **Client** integration is needed for:

1. **CLI tool** - Add x402 payment support to command-line interface
2. **MCP server** - Enable AI agents to make paid requests

See `docs/x402-client-integration.md` for implementation requirements.

## Swarm Bee API Documentation

### Using Context7 for Latest Documentation

**IMPORTANT**: Always use the Context7 MCP server to get the latest Ethereum Swarm Bee API documentation instead of making assumptions about API endpoints or using deprecated documentation.

#### How to Access Bee Documentation:
1. **Use Context7 MCP Server**: The Context7 server provides access to up-to-date API documentation
2. **Search for Bee Documentation**: Use `mcp__context7__resolve-library-id` with search terms like "ethersphere/bee", "swarm bee", or "ethereum swarm"
3. **Get Current API Reference**: Use `mcp__context7__get-library-docs` to fetch the latest API documentation

#### Common Pitfalls to Avoid:
- **Don't assume API endpoints exist** without checking current documentation
- **Don't use deprecated ports** like 1635 (debug API was deprecated)
- **Don't hardcode API structures** that may have changed between versions
- **Always verify endpoint availability** using Context7 before implementing

#### Example Context7 Usage:
```
# Find Bee documentation
mcp__context7__resolve-library-id with "ethersphere/bee"

# Get latest API docs
mcp__context7__get-library-docs with the resolved library ID
```

**Note**: If ethersphere/bee is not available in Context7, implement functionality based on observed API behavior and document any assumptions clearly.

## Monitoring

### Prometheus Metrics

The gateway exposes a `/metrics` endpoint (Prometheus text format) when `METRICS_ENABLED=true`.

**Auto-instrumented** (from `prometheus-fastapi-instrumentator`):
- `http_requests_total{method, handler, status}` — request count
- `http_request_duration_seconds{method, handler}` — latency histogram
- `http_requests_in_progress` — active requests gauge

**Custom counters** (incremented in endpoint handlers):
- `gateway_uploads_total{status}`, `gateway_upload_bytes_total`
- `gateway_downloads_total{status}`
- `gateway_stamp_purchases_total{size, status}`
- `gateway_pool_acquires_total{size, status}`
- `gateway_notary_signatures_total{status}`
- `gateway_x402_payments_total{mode}` (paid/free/rejected)
- `gateway_rate_limit_hits_total`
- `gateway_chunk_uploads_total{status, mode}` (mode = paid/free/none), `gateway_chunk_upload_bytes_total`
- `gateway_bandwidth_topups_total{status}`, `gateway_bandwidth_topup_bytes_total`

**Custom gauges** (polled every `METRICS_BALANCE_POLL_SECONDS`):
- `gateway_wallet_bzz_balance`, `gateway_wallet_xdai_balance`
- `gateway_chequebook_available_balance`, `gateway_base_eth_balance`
- `gateway_stamp_pool_available{size}`, `gateway_stamps_total`
- `gateway_stamp_min_ttl_seconds`, `gateway_uptime_seconds`
- `gateway_bandwidth_credit_accounts`, `gateway_bandwidth_credit_bytes_total` (when `CHUNK_UPLOAD_ENABLED`)

**Info**: `gateway_info{version, environment, x402_enabled, pool_enabled, notary_enabled, chunk_upload_enabled}`

**Bee chain-backend metrics** (scraped from the bundled Bee nodes, not produced by the gateway):
- `bee_eth_backend_total_rpc_calls` / `bee_eth_backend_total_rpc_errors` — Gnosis RPC volume and failures
- `bee_eth_backend_cache_block_number_load_errors` — failures refreshing the cached chain tip, the specific cause of a broken `/chainstate`
- `bee_eth_backend_calls_*` — per JSON-RPC method (`filter_logs`, `send_transaction`, `eth_call`, `balance`, …), each its own counter rather than a label
- `bee_eth_backend_average_block_time_seconds` — block time as the node observes it

> Alloy keeps only `bee_eth_backend_*` and `up` from Bee's `/metrics`. Bee exposes 874 series per node; forwarding all of them would multiply Grafana Cloud ingest for no benefit. The API port is not published, so this is reachable only on the compose network.

> New metrics are scraped by Grafana Alloy and remote-written to Grafana Cloud automatically once deployed (no extra wiring). Adding them as **panels** on `monitoring/provisioning/dashboards/gateway-overview.json` is a separate, deliberate step (tracked in its own issue).

### Production Monitoring Stack

```
Gateway containers ──/metrics──> Alloy ──remote write──> Grafana Cloud
  (port 8000)                     │                      (dashboards + alerts)
Bee nodes ────────/metrics────────┘
  (port 1633, bee_eth_backend_* only)
```

**How it works:**
- Grafana Alloy runs as a Docker container alongside the gateways (`docker-compose.yml`)
- Alloy scrapes `/metrics` from both gateway containers every 15s via Docker network
- Alloy pushes metrics to Grafana Cloud Prometheus (remote write)
- Grafana Cloud stores metrics (14-day retention) and hosts dashboards
- Environment labels: `development` (dev branch) and `main` (main branch)

**Credentials** (stored in GitHub secrets, injected at deploy):
- `GRAFANA_CLOUD_PROM_USERNAME` — Prometheus instance ID
- `GRAFANA_CLOUD_API_TOKEN` — API token with `metrics:write` scope

**Dashboard:** `datafund.grafana.net/d/gateway-overview`

**Key files:**
- `monitoring/alloy/config.alloy` — Alloy scrape + remote write config
- `monitoring/provisioning/dashboards/gateway-overview.json` — dashboard JSON (pushed to Grafana Cloud via API)

### Local Monitoring Stack

A local Prometheus + Grafana setup is in `monitoring/` for development:

```bash
# Start the gateway
SWARM_BEE_API_URL=http://localhost:1633 python run.py

# Start Prometheus + Grafana (in another terminal)
docker compose -f monitoring/docker-compose.monitoring.yml up -d

# Access:
# - Gateway metrics: http://localhost:8000/metrics
# - Prometheus:      http://localhost:9090
# - Grafana:         http://localhost:3000 (admin/admin)

# Push dashboard to local Grafana
curl -X POST http://admin:admin@localhost:3000/api/dashboards/db \
  -H "Content-Type: application/json" \
  -d @monitoring/provisioning/dashboards/gateway-overview.json

# Stop
docker compose -f monitoring/docker-compose.monitoring.yml down
```

### Monitoring Checklist for New Features

When adding a new feature, consider:
- **Does it need a counter?** New operation type (API call, transaction) → add a Counter in `app/services/metrics.py`
- **Does it need a gauge?** New stateful resource (balance, pool, queue) → add a Gauge
- **Does it need an alert?** New failure mode → document the alert rule
- **Config?** → Update `.env.example` and `deploy.yml`

## Documentation Maintenance

### Architecture Documentation
When making changes to the codebase, ensure the architecture documentation stays current:

1. **README.md Architecture Section**: Update the "Architecture" section whenever you:
   - Add new endpoints or features
   - Modify the system architecture or data flow
   - Change core components or their interactions
   - Add new layers or services
   - Update the feature list or capabilities

2. **Key Areas to Update**:
   - System Overview diagram: Reflect new components or connections
   - Core Features: Add new functionality descriptions
   - Component Architecture: Document new modules or significant changes
   - Data Flow: Update if request/response handling changes
   - Key Value Propositions: Add new benefits or capabilities

3. **Maintenance Guidelines**:
   - Keep diagrams in sync with actual code structure
   - Update feature descriptions to match current capabilities
   - Ensure component descriptions reflect actual file organization
   - Validate that environment variables and configuration are current
   - Update dependency lists when adding new packages

**Important**: The architecture documentation serves as the primary reference for understanding the system. Always verify that changes to the codebase are reflected in both the README.md Architecture section and this CLAUDE.md file.

## Git Workflow

### Branching Strategy

This project uses a three-tier branching model:

```
feature branches → dev → main
        ↓           ↓       ↓
    local dev   staging  production
```

| Branch | Purpose | Deployment |
|--------|---------|------------|
| `main` | Production-ready code | `provenance-gateway.datafund.io` |
| `dev` | Integration/staging branch | `provenance-gateway.dev.datafund.io` |
| `feature/*`, `fix/*` | Feature development | Local only |

### Workflow

1. **Create a feature branch** from `dev`:
   ```bash
   git checkout dev
   git pull origin dev
   git checkout -b feature/my-feature
   ```

2. **Develop and test locally** on the feature branch

3. **Run the full test suite locally — MANDATORY before any PR to `dev` or `main`**:
   ```bash
   source venv/bin/activate && python -m pytest tests/ -v
   ```
   - There is **no CI test gate** — `deploy.yml` only deploys, it does not run pytest. The full suite passing locally is the only thing standing between a regression and staging/production.
   - **Do not open or merge a PR to `dev` or `main` with failing or broken tests.** If a test is failing, either fix it or, if it is a known pre-existing failure, explicitly call it out in the PR description with a tracking issue — never let it pass silently.
   - This is a **development-process rule**, enforced by discipline, not automation. (Historically, the x402 async migration broke ~51 tests for months precisely because nothing ran them — see the test-repair issue.)

4. **Create PR to merge into `dev`**:
   - All code must go through PR review
   - CI/CD automatically deploys to staging (`provenance-gateway.dev.datafund.io`)

5. **Test on staging environment** before promoting to production

6. **Create PR to merge `dev` into `main`**:
   - Only after staging validation **and** a clean local `pytest tests/` run
   - CI/CD automatically deploys to production (`provenance-gateway.datafund.io`)

### Branch Protection Rules

- **Never push directly to `main`** - always use PRs
- **Never push directly to `dev`** - always use PRs from feature branches
- **Always run `python -m pytest tests/` locally and confirm it is green before opening or merging a PR into `dev` or `main`** - no CI runs the tests, so this is a manual gate
- Feature branches can be pushed directly

### Deployment Environments

| Environment | URL | Branch | Purpose |
|-------------|-----|--------|---------|
| **Production** | `provenance-gateway.datafund.io` | `main` | Live users |
| **Staging** | `provenance-gateway.dev.datafund.io` | `dev` | Testing before production |
| **Local** | `localhost:8000` | any | Development |

**Repository**: This repository pushes to `git@github.com:datafund/swarm_connect.git` (origin).

**CRITICAL - Always use datafund repo**:
- When creating GitHub issues: `gh issue create --repo datafund/swarm_connect`
- When creating PRs: `gh pr create --repo datafund/swarm_connect`
- NEVER use `crtahlin/swarm_connect` - that is the upstream fork, not the main repo
- Use `git remote -v` to verify remotes if unsure

## Deployment Workflow

### Auto-Deployment Triggers

Both `dev` and `main` branches have auto-deployment configured via GitHub Actions:

| Branch | Trigger | Target | Deployment Time |
|--------|---------|--------|-----------------|
| `dev` | Push/merge | `provenance-gateway.dev.datafund.io` | ~20-30 seconds |
| `main` | Push/merge | `provenance-gateway.datafund.io` | ~20-30 seconds |

### Environment Variables in deploy.yml

**CRITICAL**: When adding new environment variables that need to be available at runtime:

1. **Add to GitHub Environment Variables** (Settings → Environments → staging/production)
2. **Update `.github/workflows/deploy.yml`** to write the variable to the env file

The workflow writes variables to `/opt/swarm_connect_dev.env` for the `dev` branch. If a variable is set in GitHub but not written by the workflow, **the application won't see it**.

Example from `deploy.yml`:
```yaml
- name: write env file for dev
  if: github.ref == 'refs/heads/dev'
  run: |
    cat > /opt/swarm_connect_dev.env << 'EOF'
    X402_ENABLED=${{ vars.X402_ENABLED || 'false' }}
    STAMP_POOL_ENABLED=${{ vars.STAMP_POOL_ENABLED || 'false' }}
    # ... all other variables
    EOF
```

**When adding new features with env vars:**
1. Add defaults to `app/core/config.py`
2. Document in `.env.example`
3. Add to `deploy.yml` for staging/production
4. Set values in GitHub environment variables

### Verifying Deployment

After merging to `dev` or `main`:
```bash
# Check workflow status
gh run list --repo datafund/swarm_connect --limit 3

# Wait for completion, then test
curl -s https://provenance-gateway.dev.datafund.io/health | python3 -m json.tool
```

## Deployment Troubleshooting

If the remote gateway (provenance-gateway.datafund.io) returns 503 or appears broken after a merge:

1. **Test locally with Docker** to verify the build works:
   ```bash
   # SWARM_BEE_API_URL and SWARM_BEE_API_URL_DEV are required — compose refuses
   # to start without them and names the one it needs. Point them at any Bee
   # node you can reach (a local node, or http://bee:1633 with the `bee` profile).
   SWARM_BEE_API_URL=http://localhost:1633 \
   SWARM_BEE_API_URL_DEV=http://localhost:1633 \
   docker-compose up --build
   ```

2. **Check for Python version compatibility issues**:
   - Docker uses Python 3.9
   - Avoid `int | None` syntax (use `Optional[int]` instead)
   - Avoid other Python 3.10+ features

3. **Common issues**:
   - `TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'` → Use `Optional[T]` instead of `T | None`
   - Import errors → Check all dependencies are in requirements.txt

4. **Quick fix workflow**:
   - Fix the issue locally
   - Create a fix branch, commit, push, and merge PR
   - Wait ~60 seconds for auto-deployment
   - Test the gateway again

## Commit Message Guidelines

- Do NOT include Claude/AI mentions, co-author tags, or "Generated with Claude" footers in commit messages
- Do NOT include "Generated with Claude Code" or similar footers in PR descriptions
- Keep commit messages and PR descriptions clean and professional - just describe the changes

## PLUR Domain Scoping

When calling `plur_learn`, always set:
- `domain`: `provenance.gateway`
- `scope`: `project:swarm-connect`

This ensures engrams are tagged for retrieval in the right context across the global store.

## PLUR Memory

You have persistent memory via PLUR. Corrections, preferences, and conventions persist across sessions as engrams.

> **PLUR is its own MCP server.** The tools below come from the `plur` MCP server registered by `plur init` — `plur_session_start`, `plur_learn`, `plur_recall_hybrid`, `plur_feedback`, `plur_session_end`. If you do not see these exact tool names, **PLUR is not connected**: stop and run `plur doctor` to diagnose. Do **not** substitute tools from other MCP servers (e.g. `datacore_*`) — those belong to a different system and will not persist anything for PLUR.

### Session Workflow

1. **Start**: Call `plur_session_start` with task description — injects relevant engrams
2. **Learn**: When corrected or discovering something new, call `plur_learn` immediately
3. **Recall**: Before answering factual questions, call `plur_recall_hybrid` — check memory first
4. **Feedback**: Rate injected engrams with `plur_feedback` (positive/negative) — trains relevance
5. **End**: Call `plur_session_end` with summary + engram_suggestions

Do not ask permission to use these tools — they are your memory system.

### When to check memory

Before reaching for web search, file reads, or guessing — apply this priority:
1. Is the answer already in engrams? → `plur_recall_hybrid`
2. Is the answer in the local filesystem? → Read/Grep/Glob
3. Is the answer derivable from context already loaded? → Just answer
4. Only if 1-3 fail → Use external tools

| Domain | When to recall |
|--------|----------------|
| Decisions | Past design choices, architecture rationale |
| Corrections | API quirks, bugs, wrong assumptions |
| Preferences | Formatting, tone, workflow, tool choices |
| Conventions | Tag formats, file routing, naming rules |
| Infrastructure | Server IPs, SSH configs, deployment targets |

### When corrected

When the user corrects you ("no, use X not Y", "that's wrong"):
1. Call `plur_learn` immediately — before continuing the task
2. Call `plur_feedback` with negative signal on the wrong engram if one was injected
3. Then continue with the corrected approach

### Verification

When recalling facts that will drive actions:
1. State the recalled fact explicitly before acting on it
2. Include the engram ID or search that produced it
3. If no engram matches, say so and verify from the filesystem
4. Never interpolate between two engrams to produce a "probably correct" composite
