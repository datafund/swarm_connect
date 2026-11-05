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

# Install dependencies: the runtime set plus the test-only packages (#430).
# requirements.txt holds what the gateway runs; requirements-dev.txt holds
# pytest, pytest-asyncio and the other test-only packages, which the image
# does not ship.
pip install -r requirements.txt -r requirements-dev.txt
# CI installs the pinned sets instead (the image installs only requirements.lock);
# to reproduce CI exactly (Python 3.12):
#   pip install --no-deps --require-hashes -r requirements.lock -r requirements-dev.lock && pip check
# CI also builds the image on every PR, uncached, and checks it answers GET /
# (.github/workflows/image.yml, #486; see TEST_STRATEGY.md for the commands)
# After a dependency change, refresh an existing venv the same way: since #595
# the gateway imports httpx2, so a venv made before it fails with
# "No module named 'httpx2'" until you rerun the install (or rebuild it from the locks)

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
> `pytest tests/` is hermetic — no live gateway is contacted. The live modules
> (`test_integration_gateway.py`, `test_x402_live.py`) are opt-in via
> `RUN_LIVE_TESTS=1`, target **staging** unless `GATEWAY_URL` says otherwise,
> and will not purchase a postage batch without `ALLOW_LIVE_STAMP_PURCHASE=1`
> (or, for the paid x402 test only, `TEST_WALLET_PRIVATE_KEY`, which pays from that wallet).
> Before #233 they defaulted to **production** and skipped only when it was
> unreachable, so the mandatory pre-PR run sent real traffic there — and could buy
> a stamp with real BZZ. See TEST_STRATEGY.md.

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

**Shared HTTP client (`app/services/http_client.py`)**:
- One `httpx2.AsyncClient` for Bee and Base RPC calls, created in the lifespan; call sites take it from `get_client()` for each request
- httpx2/httpcore2 is Pydantic's maintained fork of httpx/httpcore (#595). **Every** module in `app/` uses httpx2: the two libraries' exception classes are unrelated, so an `except httpx.HTTPError` would silently stop catching the shared client's errors (in `stamps._outcome_unknown`, a timeout after a paid purchase would read as "Bee answered"). `tests/test_httpx2_switch.py` fails on any `httpx`/`httpcore` import or name in `app/` (allow-list, empty today). x402 1.0.0 keeps encode's httpx for its own facilitator client, caught only as `Exception`; `requirements.txt` therefore keeps `httpx`. `httpx2.alias_httpx()` is not used. In tests, `httpx` appears only where a test models x402's client, in the httpcore 1.0.9 reproduction, and in a user-agent case
- Outbound requests carry `User-Agent: python-httpx2/<version>`
- TLS: httpx2 does not use certifi. On Linux (the image) it verifies against OpenSSL's default CA file, the OS bundle from `ca-certificates` (`/etc/ssl/certs/ca-certificates.crt`); `truststore` (the OS store) only on macOS and Windows. `SSL_CERT_FILE`/`SSL_CERT_DIR` override it. `image.yml` checks the bundle and httpx2's loaded CAs inside the built image, offline. It also runs three offline handshakes there: a local TLS server with a fresh self-signed certificate must be refused by default httpx2, accepted by a client that trusts that certificate, and refused for another host name
- Its transport counts `PoolTimeout`s and in-flight requests, and replaces the client when the pool is stuck (#587, see Environment Configuration)
- `exc_text(e)` gives `Type: message`, or just the type when the message is empty (httpx2 timeouts), for error log lines
- Its transport is wrapped by the Bee call gate (`app/services/bee_gate.py`, #588), which caps Bee calls in flight; see Environment Configuration

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
- Access control field: `accessMode` (`"owned"`, `"shared"`, `"pool"`, or `null`)

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

Shared outbound HTTP client (`app/services/http_client.py`, #587):
- `BEE_HTTP_MAX_CONNECTIONS` (default `100`), `BEE_HTTP_MAX_KEEPALIVE_CONNECTIONS` (`20`), `BEE_HTTP_KEEPALIVE_EXPIRY_SECONDS` (`5.0`): pool limits; the defaults are httpx2's own.
- `BEE_HTTP_POOL_RECOVERY_THRESHOLD` (default `3`), `BEE_HTTP_POOL_STUCK_FRACTION` (`0.25`), `BEE_HTTP_POOL_RECOVERY_COOLDOWN_SECONDS` (`60`): self-recovery, kept as a backstop since the switch to httpx2 (#595), whose httpcore2 drops the connections behind the leak (pydantic/httpx2#983); #618 decides from a soak whether it goes. httpcore 1.0.9 keeps a pool slot for good when a request is cancelled (client disconnect, `asyncio.wait_for` timeout) right after the pool gave it a new connection; enough of them and every call fails with `PoolTimeout` until a restart (staging, 2026-11-03). The client is replaced when there were this many `PoolTimeout`s in a row, at least this fraction of `BEE_HTTP_MAX_CONNECTIONS` is stuck, and Bee answers `GET /health` on a new connection; if the pool cannot be read, the probe alone decides. One replacement at a time (lock and generation check), attempts at least the cooldown apart. The old client closes in the background once its in-flight requests finish (at most the longest Bee timeout plus 60 s). `tests/test_bee_pool_recovery.py` reproduces the leak on httpcore 1.0.9 (still installed for x402) and requires httpcore2 to stay healthy; its recovery tests inject connections httpcore2 keeps (connected, never idle, held by no request), so the timeouts and the pool snapshot are real. Its httpcore2 load test runs on httpcore2's own connect path, as the gateway does, and requires no stuck connection afterwards. With the test's shielded-connect recording backend instead, httpcore2 2.16.0 kept stuck `HTTP/1.1 NEW` connections after most five-second runs under anyio cancel scopes, up to 3 of the pool's 4 slots; on its own connect path none (2026-11-05). That the shielded connect opens the window is a hypothesis, investigated in #629. The gateway does run its endpoints under anyio cancel scopes through `BaseHTTPMiddleware` (`RateLimitMiddleware`, `AccessListMiddleware`, `BodyLimitMiddleware`, `X402Middleware`), so #618 must check `gateway_bee_http_pool_connections{state="stuck"}` over the soak before the recovery is removed.
- Proxies: the shared client is built with an explicit transport, so httpx2 does not apply `HTTP_PROXY`, `HTTPS_PROXY` or `ALL_PROXY` from the environment to it (before #587 it did). This is intended: Bee is on the compose network and must be reached directly. It applies to the Base RPC calls on the same client too, so a deployment that needs a proxy for Base RPC cannot set it through those variables. The recovery probe uses `trust_env=False` for the same path.

Flood resilience (#588):
- Bee call gate (`app/services/bee_gate.py`, `app/middleware/bee_gate.py`): `BEE_CALL_GATE_ENABLED` (default `true`; `false` = no gate, no rewrite, no pre-check), `BEE_MAX_CONCURRENT_CALLS`, `BEE_RESERVED_BACKGROUND_CALLS`, `BEE_MAX_CONCURRENT_TRANSFERS` (unset: 64%, 8% and 24% of `BEE_HTTP_MAX_CONNECTIONS`, so 64/8/24 of 100 and 26/3/10 of 40; the resulting cap must be below `BEE_HTTP_MAX_CONNECTIONS`, the reserve below the cap, transfers at most cap minus reserve, or startup fails), `BEE_CALL_SLOT_WAIT_SECONDS` (`2`), `BEE_BACKGROUND_SLOT_WAIT_SECONDS` (`30`), `BEE_BUSY_RETRY_AFTER_SECONDS` (`5`). Only calls to `SWARM_BEE_API_URL` are gated (not Base RPC). Slot classes: `background` (outside any request; may use every slot), `user` and `transfer` (a request's calls; together at most calls minus reserved; transfers = bzz/bytes/chunks also capped), `committed` (a request whose x402 payment settled, marked in `settle_payment`; same pool, long wait). A slot is held until the response body is closed. A refused call raises `BeeBusyError` (an `httpx2.TransportError`, not a `PoolTimeout`; not counted in `gateway_bee_api_errors_total`). The middleware turns the error response (status >= 400) of a request whose OWN call was refused into `503 BEE_BUSY` + `Retry-After` ("nothing was charged"), but never when that may be untrue or hide a payment answer: the request carries `X-PAYMENT`; a state-changing Bee call of the request (any method but GET/HEAD) got a slot and was sent, whatever its outcome (`RequestGateState.change_sent`, set in `GatedTransport`); settlement was started (`mark_payment_attempted()` immediately before both facilitator settle calls, in `settle_payment` and `X402Middleware._finish_paid`) or completed; the response has `X-Payment-Status`, `X-PAYMENT-RESPONSE`, `X-Payment-Transaction` or `X-Payment-Mode: paid|failed`; or its body has a `PAYMENT_`/`PAYMENTS_`/`IDEMPOTENCY_`/`DELIVERY_`/`PURCHASE_` code or x402 fields. 2xx and the responses of other requests pass through. The 503 keeps the replaced response's `X-RateLimit-*` headers and drops its others. A `BeeBusyError` certainly did not reach Bee: `spend_certainly_did_not_happen` is true and `stamps._outcome_unknown` false, so a paid purchase refused after settlement goes to its refund path. Work a request starts but that runs outside it calls `bee_gate.enter_background()` (pool `/check` maintenance, `_async_replenish_one`, `recover_pool`); a paid purchase's own task stays `committed`. A paid route with an `X-PAYMENT` header is refused up front when no request slot is free. The sizes are a judgement, not measured.
- `/health` snapshot (`app/services/health_snapshot.py`): `HEALTH_SNAPSHOT_TTL_SECONDS` (`15`), `HEALTH_SNAPSHOT_REFRESH_TIMEOUT_SECONDS` (`20`), `HEALTH_SNAPSHOT_FIRST_WAIT_SECONDS` (`5`), `HEALTH_SNAPSHOT_MAX_TRUSTED_AGE_SECONDS` (`60`, never less than TTL + refresh timeout). Single-flight refresh run as background work, stale-while-revalidate, failed refresh retried no sooner than one TTL later. A snapshot is **too old to trust** (`health_snapshot.too_old`, #589) when its last refresh failed AND it is older than the max trusted age; an old snapshot with no failed refresh is not (refreshes are started by requests, so an idle gateway has one). `health_snapshot.peek(x402)` returns the snapshot without waiting or calling Bee, starting a refresh only when one exists and is stale (never the first one: startup and `/health` build that). `app.main.build_health_response(snapshot)` holds every `status` rule. Tests get a fresh snapshot per test (conftest).
- Top callers (`app/services/top_callers.py`): `TOP_CALLERS_ENABLED` (`true`), `TOP_CALLERS_WINDOW_SECONDS` (`60`), `TOP_CALLERS_N` (`5`), `TOP_CALLERS_MAX_TRACKED` (`10000`), `TOP_CALLERS_LOG_MIN_REQUESTS` (`300`), `TOP_CALLERS_HASH_KEY` (empty = random per process). Keyed blake2b tag of `get_client_key()`; metrics carry ranks only, the log line carries tags; no address in either.
- Alerts (warning, `monitoring/alerting/alert-rules.json`, tests in `tests/test_flood_alerts.py`): **Bee calls refused by the gate** (`sum by (environment, host) (increase(gateway_bee_call_rejections_total[5m])) > 25` for 10m: a single burst leaves the window before the hold ends) and **Health snapshot stale** (`max by (environment, host) (gateway_health_snapshot_age_seconds) > 120` for 5m; working refreshes stay under about 65 s). Runbook rows in docs/mainnet-runbook.md section 5.
- Replay: `tests/test_flood_resilience.py` (in-process, stub Bee), `scripts/flood_replay.py` + `python -m tests.tools.bee_stub` against a running gateway.

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
- `GET /` (and `/health`): Health check. Same handler and body for both. The Bee node and wallet parts come from a snapshot refreshed at most every `HEALTH_SNAPSHOT_TTL_SECONDS` (#588), so a request makes no upstream call; `snapshot.age_seconds`, `snapshot.max_age_seconds` and advisory `snapshot.warnings` describe it. Always includes a `bee_node` section (from Bee `/topology` + `/status` + `/health` + `/addresses` + `/chainstate`, fetched concurrently, 15s cached): identity/build `overlay`, `version`, `api_version`, `bee_status`; connectivity `mode`, `connected_peers`, `population`, `depth`, `reachability`, `network_availability` (Available/Unavailable/Unknown — Bee sets this from outbound-dial results; Unavailable = OS network/host-unreachable on dials); reserve/radius `storage_radius`, `committed_depth`, `reserve_size`, `reserve_size_within_radius`, `pullsync_rate`, `batch_commitment`; chain sync `last_synced_block`, `chain_tip`, `chain_sync_lag_blocks`; plus `warming_up`, `healthy`, `warnings`. Any endpoint that fails yields `null` for its fields rather than losing the whole section. `bee_node.api_reachable` (#589) is false when none of Bee's `/health`, `/addresses`, `/topology`, `/status` answered (a node still starting up answers the first two, so it is reachable). Overall `status` → `degraded` (never `critical`: the SDK's `health()` is false only on `critical`, and #108 kept Bee connectivity out of it) when the Bee API cannot be reached (`api_reachable` false), when the node summary could not be built (`bee_node` is then `{healthy: false, api_reachable: false, warnings}`, never left out: external probes read `bee_node.healthy`), when there is no snapshot yet, when the snapshot is too old to trust (#589; `bee_node` then keeps the last values but reports `healthy: false`, `api_reachable: null` and a 'Bee node state unknown' warning, so external probes of `bee_node.healthy` (#586) do not see a stale true; the snapshot itself is not changed), or when `network_availability` is `Unavailable` (node can't reach the storer network → uploads may 201 without propagating) — advisory warnings (low peer count `< LOW_PEER_WARN_THRESHOLD`, chain lag `> CHAIN_LAG_WARN_BLOCKS`, non-ok Bee status) never flip `healthy` or `status`. A `state` section is always included: `state.warnings` lists fixed-text warnings about gateway state files (today only ownership registry saves that are refused or fail, #558); they are advisory and never change `status`. x402 wallet section added when `X402_ENABLED`; `can_accept` is false when the xBZZ or xDAI balance could not be read (#589; a low balance that was read, or an unreadable chequebook, is a warning only), and `status` is then `degraded`. Its `warnings` name a failed Bee wallet or chequebook fetch by HTTP status or exception class only ("Failed to fetch xBZZ balance from the Bee node (HTTP 503); see the gateway log for details."), never the exception text, which for an httpx2 error contains the internal Bee API URL; the full text is logged (#549). Stamp purchase and extend errors follow the same rule: a Bee 4xx with an empty body is reported as `HTTP <status>`, and Bee's own message goes through `public_bee_text` in `app/services/bee_errors.py`: scrubbed by `without_addresses` at a 2 KB cap, cut to 200 characters without a partial word (adding "…"), and then passed through a **fail-closed safety gate**. The gate is the guarantee: with the placeholders removed, the text must contain no non-ASCII or control character (so multi-line text is hidden), no `:` that is not followed by a space or the end, no `//` or `::`, no `@`, `%`, `\` or `?` before a non-space character, no two colon-separated hex groups, no four digit groups with any of ` ,._/-` between them (`10 0 0 1`, `ip_10_0_0_1`, `10/0/0/1`), no integer of 7+ digits, `0x` hex of 6+ or 8+ hex characters mixing digits and letters, no Go form `lookup X:`, `lookup X on`, `address X:` or `dial tcp X:` with a real name, no dotted token other than a version of at most three digit groups, and no configured secret in any letter case. Otherwise (and when no safe cut point exists) nothing of Bee's text is shown: the stamp endpoints answer `Bee rejected the stamp purchase|extension: HTTP <status>`, and `/pool/status` leaves out the "Bee said" part. So timestamps, `key:number` fields, Go source positions, dotted words and public links in Bee's text are hidden on purpose; Bee's fixed messages carry no addresses: every literal, constant and `errors.New` message in Bee's `pkg/api`, the mapStructure parameter texts and the HTTP status texts pass unchanged (`tests/fixtures/bee_api_messages.txt`, regenerated by `tests/fixtures/extract_bee_api_messages.py`; e.g. `out of funds`, `syncing in progress`), as do user agents (`bee/2.8.1`). Handlers that pass a Go error through have dynamic text and are not on the stamp or pool paths. The scrubber exists for text from proxies and intermediaries. The scrubber keeps messages readable: `without_addresses` replaces, as `<secret>`, secrets of the configured URLs wherever they appear, in any letter case: passwords of 8+ characters, and path segments, query values and username-only keys holding a run of 8+ letters and digits that mixes two of lowercase, uppercase and digits (so `facilitator`, `Transaction`, `mainnet-v2` and one-case keys such as `abcdefghijklmnop` are not collected). It also replaces URLs; libp2p multiaddr addresses, names and ports (the peer id stays); `user:pass@` credentials (up to 256/1024 characters; a key name before them such as `token=` stays); validated IPv4 (four 1-3 digit groups, each at most 255) and IPv6 addresses (bare ones need 3+ groups, or groups on both sides of `::` with 4+ hex digits), with any port, path, query or fragment, even glued to letters, `_` or non-ASCII text; the name after `lookup` when it is dotted, configured, or followed by ` on `; the names in Go's x509 `certificate is valid for ..., not ...` and `wanted to match ...`, taken whole; dotted `host:port`, and dotted names with a letters-only last label followed by a path, query or fragment, except a path ending in a Go source position (`.../api.go:42`) and public code and docs hosts (github.com, gitlab.com, golang.org, go.dev, ethswarm.org and their subdomains) unless configured; names ending in `.internal` or `.svc`, or with two or more labels before `.local`, `.lan` or `.home`; and the hosts of the gateway's own URL settings (`SWARM_BEE_API_URL`, `GNOSIS_RPC_URL`, `BASE_RPC_URL`, `X402_FACILITATOR_URL`, `X402_BZZ_PRICE_FEED_URL`; a value without a scheme counts too, a blank one is ignored), matched as whole words in any case: a dotted one anywhere, a dotless one (a compose service name such as `bee`, already public in `docker-compose.yml`) only with a `:port`, a `/path` not starting with a digit, a `?query`, after `user:pass@` or `lookup`, or in a multiaddr, and never right after `/`, so "Bee node not ready", the user agent `bee/2.8.1` and `github.com/ethersphere/bee/v2` stay. A host is found after `<`, `@` or `/` too. Percent-encoded text is decoded (printable ASCII, twice at most) and shown decoded when that reveals anything to replace, keeping what the first pass caught; standalone RFC 3339 timestamps are kept by the scrubber. Kept by design: unconfigured dotless hosts (`nethermind:8545`, `localhost:8545`; Bee's own RPC endpoint is a Bee setting, not a gateway one), bare dotted names with no port or path, short IPv6 forms such as `::1` and `2001:db8::`, and `key:number` fields such as `timeout:30`. Known scrubber limits, each refused by the gate: text past the 2 KB pre-cut (the `lookup X…` left at the cut), an address glued to a percent-encoded non-ASCII byte, and an internal host inside a public code or docs link. Not hidden by either: a bare single-label host name nobody configured, with no port or path and outside Go's `lookup X:`, `lookup X on`, `address X: missing port` and `dial tcp X: unknown port` forms (`dial nethermind failed`), which cannot be told from a word; and inet_aton short IPv4 forms such as `10.0.1` or `127.1`, which cannot be told from versions. The gate's patterns run on at most 2 KB and are linear. A timestamp inside a URL or path is scrubbed with it. The module docstring has the full ordered rule list.

#### Stamp Management
- `POST /api/v1/stamps/`: Purchase new postage stamps (records purchase time for propagation tracking). **Bounded** by `X402_MAX_STAMP_BZZ` per request and `STAMP_DAILY_BZZ_PER_CALLER` per caller per day — see below. Labels are sent to Bee as the `?label=` query parameter (a JSON body was ignored by Bee, so labels used to be dropped). Caller labels: at most 100 characters, no Unicode control or format characters (Cc/Cf) (checked before payment), not `recovered`, not starting with `paid-`/`pool-`/`synced-`; `""` means none. **Slow or lost Bee response after payment (#400):** a paid purchase sends Bee a label unique to it: the caller's plus `-<12 hex>`, else `paid-<12 hex>`. Bee's `POST /stamps` runs as its own task with `STAMP_PURCHASE_BEE_TIMEOUT_SECONDS` (900) and is never cut off by the gateway: Bee only attaches the label after the receipt, on the request's context, so closing the connection early leaves a `recovered` batch that cannot be attributed. The request waits for it up to `SWARM_STAMP_PURCHASE_TIMEOUT_SECONDS` (120; also the free tier's Bee timeout), then answers `202 PURCHASE_PENDING`, and the background half awaits the same task and registers Bee's batchID. Only if that request itself ends without an answer (a transport error, or a 502/504 from a proxy), `find_purchased_batch` polls Bee's `/stamps` for up to `STAMP_PURCHASE_LOOKUP_SECONDS` (30) for exactly one batch with that unique label, depth and amount, created at or after the chain block noted before the purchase, and not registered to anyone; a failed poll counts as "not found yet". Bee's `recovered` batches are deliberately **not** matched (nothing ties one to this purchase rather than a concurrent one); those go to the refund path. Found → registered to the payer with `register_stamp(only_if_unowned=True)`, normal 201. Not found → `202`, and the background half keeps looking for `STAMP_PURCHASE_BACKGROUND_LOOKUP_SECONDS` (900). Every 202 writes a `purchase_pending` audit event (tx, label, depth, amount, start block, payer); background tasks and Bee requests are held in `_PENDING_TASKS`, and on shutdown `drain_pending_purchases` waits `SHUTDOWN_PENDING_PURCHASE_GRACE_SECONDS` (25; uvicorn `--timeout-graceful-shutdown 10`, compose `stop_grace_period: 40s`) before cancelling them, a purchase still at Bee being recorded as "in flight at shutdown". At most `STAMP_MAX_CONCURRENT_PAID_PURCHASES` (10) paid purchases wait on Bee at once, checked before settlement (`503 PURCHASE_CAPACITY`, not charged), which also keeps them from exhausting the shared Bee client's 100-connection pool. `resolve()` also finalises a `settled` key whose request ended before storing the 202 (client disconnect), so a retry gets the late 201. Delivered late → registered, `payment_delivered` (`late: true`), stored Idempotency-Key result replaced with the 201. Otherwise (refused by Bee, not found, found but already registered, an error of its own, or cancelled at shutdown; the reason says which) → `payment_failed` `delivery_after_settlement` and the stored key result becomes `500 DELIVERY_FAILED_AFTER_PAYMENT`. `register_stamp` logs an ERROR whenever a batch changes hands between non-pool owners. Ownership is registered as soon as the batch id is known; bookkeeping after it (spend budget, purchase time, metrics) cannot fail the response.
- `GET /api/v1/stamps/`: List stamps (default: local only). Supports `?global=true` for all stamps, `?wallet=0x...` for wallet-filtered view (x402)
- `GET /api/v1/stamps/{stamp_id}`: Retrieve specific stamp batch details including propagation timing
- `GET /api/v1/stamps/{stamp_id}/check`: Check stamp health for uploads (errors, warnings, can_upload status, propagation status)
- `PATCH /api/v1/stamps/{stamp_id}/extend`: Extend existing stamps with additional funds. Subject to the same two bounds. Payment-gated like a purchase (`("PATCH", "/api/v1/stamps/")` is in `PROTECTED_ENDPOINTS`) and priced from the parsed request and the batch's own depth. Only the batch's owner may extend it (same `check_access` rule as uploads: payer-owned needs that payer, `shared` is open, pool inventory and untracked batches are refused), and a legacy `amount` below 24 hours' worth is refused with `EXTENSION_TOO_SMALL` (#350). The `stamp_id` is case-insensitive: the handler lowercases it first, and the price quote matches the batch case-insensitively, so the quote uses the same depth as the top-up (#542). A batch the Bee node does not list gets the route's own 404 from the x402 dependency, raised by `_calculate_price_for_request` from the same `get_all_stamps_processed()` lookup that finds the depth (no extra Bee call; that listing is not cached), so it comes after the #427 validation 422 and before any 402 offer, payment verification or free-tier count (#572). `stamp_not_found()` in `stamps.py` builds that 404 for both places. If Bee cannot be reached for the lookup, the dependency answers 503 "Failed to calculate price" as for any pricing failure, never 404, again before any offer, verification or free-tier count.
- `POST /api/v1/stamps/for-owner` (Flow B #228/#230): create a postage batch owned by an arbitrary address via `GnosisChainClient.create_batch` (PostageStamp.createBatch on Gnosis), so the owner can sign its own stamps off-node. Body: `owner` (0x, never assumed = payer), `size`/`depth`, `duration_hours`, `immutable`. Returns `batchID` (64-hex, no 0x) + `txHash` + propagation info; records the batch in the ownership registry (`source="created_for_owner"`, informational — on-chain ownership is source of truth). **Spends the gateway's Gnosis funds**, so: OFF by default (`STAMP_PURCHASE_FOR_OTHERS_ENABLED`, router 404s when off); owner **allow-list** (`STAMP_FOR_OTHERS_REQUIRE_WHITELIST` + `_OWNER_WHITELIST`); hard caps `STAMP_FOR_OTHERS_MAX_DEPTH` / `_MAX_BZZ` / `_MAX_DURATION_HOURS` — ALL enforced before any on-chain spend. Plus a signer-wallet **preflight** (#231): refuses `503 SIGNER_INSUFFICIENT_FUNDS` if the gateway can't fund the batch (gas/xBZZ), checked after the caps and before createBatch. **x402 (#229):** mounted WITH the x402 dependency, so when `X402_ENABLED` the caller pays via the `/stamps/` protected prefix (priced from the actual depth/duration by reading the body in `_calculate_price_for_request`); free-tier creation is OFF by default (`STAMP_FOR_OTHERS_FREE_TIER_ENABLED`, else `402 FREE_TIER_DISABLED`). Payer (x402) ≠ owner (`body.owner`). Emits `gateway_for_owner_batches_total{status}` + `_bzz_spent_total` and audits each creation. **Concurrency (#368):** `GnosisChainClient` holds a `threading.Lock` (create_batch runs in `asyncio.to_thread`) from the nonce read through the createBatch receipt, acquired with a 30 s timeout (`SIGNER_LOCK_TIMEOUT_SECONDS`). Inside it, if the signer's `pending` nonce is above `latest` (an earlier tx unconfirmed), it refuses before sending anything. Both refusals raise `SignerBusy` (a `GnosisChainError`: nothing spent) → **503 `SIGNER_BUSY`**, not settled. Receipt waiting is bounded at 120 s total for approve + createBatch (`RECEIPT_TIMEOUT_SECONDS`), inside the 300 s x402 authorization and Caddy's 300 s `read_timeout`. It keeps a standing allowance of `APPROVE_BUFFER_BATCHES` (10) × the per-batch cap, topped up below half (logged with tx hash), instead of approving the exact cost. createBatch gas limit = max(estimate × 1.3, 1,000,000), capped at 3M and never below the estimate (`CREATE_BATCH_GAS_MARGIN` / `_FLOOR` / `_CAP`). The tree insert costs more when the batch lands in a later block than the estimate assumed. On bee-factory the bare estimate ran out of gas about every second batch, using up to ~2× the estimate, so margins alone (1.3×, 2×) were not enough. A createBatch receipt timeout raises `TransactionPending` (deliberately not a `GnosisChainError`) and the endpoint answers **202** with `txHash`, `confirmed: false` and a "may still mine" message; being 2xx, the x402 payment settles (fail closed). A pending batch is registered like a created one; if it never mines, the record stays (no cleanup is wired). An approve timeout is a plain `GnosisChainError` (502). See `docs/buy-batch-for-owner-guide.md`.

**Stamp list query parameters**:
- `global` (bool): If true, return all stamps including non-local (old behavior)
- `wallet` (string): Filter to stamps accessible by this wallet address (requires x402 enabled). Case-insensitive (#491): canonicalised with `normalize_address`, as the ownership registry stores owners, so a checksummed, uppercase or unprefixed address finds its own stamps. Where the filter applies (x402 enabled, `global` not true), an invalid address is `422` instead of an empty list; where it is ignored it is not checked. Validated before the handler's `try`, whose `except Exception` would make it a 500.
- `exclusive` (bool): When used with `wallet`, return only stamps purchased by this wallet (excludes shared/free and untracked)

**Propagation timing fields** (included in all stamp responses):
- `secondsSincePurchase`: Seconds elapsed since purchase through this gateway (null for external stamps)
- `estimatedReadyAt`: ISO 8601 timestamp when stamp should be usable (null for external stamps)
- `propagationStatus`: `"ready"` / `"propagating"` / `"unknown"` (null if undetermined)

**Spending limits on the stamp endpoints** (`app/services/spend_budget.py`, #102):
Both `POST /stamps/` and `PATCH /stamps/{id}/extend` spend the gateway's BZZ for whoever asks, and neither had a bound — the pool got a daily allowance and these did not, which made them the cheaper way to spend the operator's money. Measured on staging, an anonymous free-tier request reached the point of the gateway costing a **243,074 BZZ** batch and was refused only because the wallet could not cover it: the balance was the limit.

Two bounds now apply, answering different questions:

- `X402_MAX_STAMP_BZZ` (default 5.0, zero disables) caps a **single request**, so no one call takes a large share of the wallet however it is shaped. This setting existed from the start and was referenced nowhere — a cap that appears in configuration and enforces nothing, which is worse than an absent one because it reads as protection during review.
- `STAMP_DAILY_BZZ_PER_CALLER` (default 0.5, `-1` disables) caps a **caller over a day**, so the first bound cannot simply be applied repeatedly.

The budget counts **BZZ, not batches**, unlike the pool allowance. The pool hands out fixed inventory so counting batches per size bounds the spend; these endpoints take a depth and a duration and cost `amount × 2^depth`, so a count would let a caller stay inside its allowance and still spend arbitrarily by asking for bigger batches.

The key is the **client IP**, not `Origin`. The callers here are CLIs, SDKs and the MCP plugin, which send no `Origin` at all and would collapse into one shared bucket. An IP is not an identity — shared behind NAT, cheap to change — and this is the same bargain `bandwidth_free_tier.py` already makes. It bounds casual and accidental spending, which is what actually happened twice, without pretending to prevent deliberate spending.

Both limits are enforced **before** the wallet balance check, so the refusal does not depend on how much money happens to be left. Charged only after the money is actually spent, so a purchase Bee refuses costs the caller nothing. A settled x402 payment bypasses the daily budget but **not** the per-request ceiling — the gateway fronts the BZZ either way — and the bypass is withheld on a test network for the same reason as the pool's.

`PLUR_PER_BZZ` and `plur_to_bzz` now live once, in `app/services/swarm_api.py`, and everything else imports them. There were five copies of the constant and two of the function — `app/x402/pricing.py`, `app/x402/preflight.py`, `app/services/gnosis_chain.py`, `app/api/endpoints/stamps_for_owner.py` and `swarm_api` itself. They all agreed, but nothing made them agree, and one drifting would have produced wrong money arithmetic in one place and not the others. The x402 modules re-export both, so existing imports and tests are unaffected.

**Stamp ownership enforcement** (`app/services/stamp_ownership.py`, when `X402_ENABLED`):
Every batch a caller can obtain is registered to them — pool acquire, direct purchase, and for-owner all call `register_stamp`. Batches the pool buys for its own inventory are registered as `POOL_OWNER` (`"pool"`) at purchase and on sync, and `check_access` **refuses** them: a caller receives one by acquiring it, which re-registers it to them. A batch absent from the registry is also refused; `STAMP_OWNERSHIP_ALLOW_UNTRACKED=true` restores the old permissive default and exists solely to recover from a lost registry file. Before #312 the pool's inventory was untracked and the untracked default was *allow*, so anyone could store data on batches the gateway had paid for — one production batch reached 50% utilisation without ever being acquired.

**Pool sync and ownership (#477).** `sync_from_bee_node` is the only path that puts a batch back into the pool from the state file (startup and every periodic check both go through `check_and_replenish`). Before importing, it reads the batch's ownership record. A batch registered to any owner other than `"pool"` (a wallet address or `"shared"`) is not imported, is dropped from the pool state, and is logged at ERROR. If such a batch is already in the in-memory pool, it is removed, unless it is `RESERVED` for an acquire in progress. Unregistered batches (pre-#312 inventory) and `"pool"` batches import as before. If reading the record raises, the batch is not imported on that sync, is logged at ERROR, and stays in state for the next sync. This matters because `_save_state` swallows write errors: when the save after a release fails, the state file still lists the sold batch, and sync used to put it back up for sale. Not covered: if the ownership save also failed and the process restarted, the registry still says `"pool"` and the batch is re-imported. **Held, not yet usable:** a batch the pool bought but cannot sell yet is held with `PoolStamp.usable=False`: it counts toward the reserve target (`_HELD`) but is never selected, reserved, released or counted as available; `get_status`/`/pool/status` report it in `pending_stamps` by depth (gauge `gateway_stamp_pool_pending{size}`), and each TTL check that holds one logs "Holding N pool batch(es) until Bee lists them usable". Sync and `_update_stamp_ttls` hold a batch in two cases. (a) Bee lists it with `usable=false`, TTL > 0 and no `unusableReason`: held as long as Bee lists it so. (b) Bee does not list it yet (Bee lists a batch only after its chain listener processes BatchCreated, some blocks behind the tip): `add_stamp_to_pool(usable=False)` records it as pending in `<state>_pending.json` (purchase time from when Bee returned the id, depth, amount, TTL, label), so this survives a restart; it is held for `STAMP_POOL_UNLISTED_GRACE_SECONDS` (900, > 0) after the purchase, then removed from the pool, the state file and the ownership registry. Sync also evaluates pending ids the state file lacks, and writes the state file whenever what it would write differs from what the file lists, so a batch held only from the pending record is in the file before its pending entry is cleared (else a second restart stranded it). A pending entry is cleared once Bee lists the batch and the state file is known to list it (a save just succeeded, or the file already matched the pool), or when the batch leaves the pool; clearing it before that let a failed state write followed by a restart strand the batch. A failed state write sets `_state_dirty`, so the next sync writes it again (no pause: only an unreadable file pauses, #547). A missing pending file loses only the grace period; an unparseable one is copied once to `.corrupt-<ts>` (`keep_corrupt_copy`, #440) and treated as empty, logged as such at ERROR; a malformed entry is dropped with an ERROR; a timestamp without a time zone is read as UTC, and one in the future is clamped to now, so the hold stays bounded by the grace period. Any other batch Bee does not list is removed as before. `_update_stamp_ttls` skips batches added while its listing was being fetched (the listing predates them). While any batch is held, the background loop re-runs `_update_stamp_ttls` (`_recheck_held`) every `STAMP_POOL_HELD_RECHECK_SECONDS` (30, > 0) between full checks, so the batch is offered soon after Bee lists it usable; the loop wakes every `STAMP_POOL_HELD_RECHECK_SECONDS`; while the same batches stay held the time between re-checks doubles up to `STAMP_POOL_CHECK_INTERVAL_SECONDS`, a newly held batch resets it (re-checked at the next wake), a re-check that returned early does not count, and with nothing held it makes no extra Bee calls. The re-check has its own flag (`_rechecking`): it is skipped while a full run is in progress or before a sync has read Bee, and a full run (including a signed `POST /pool/check`) that arrives during a re-check waits for it, at most `_RECHECK_WAIT_SECONDS` (60), then runs; it is never skipped because of a re-check. A batch held longer than the grace period (one Bee keeps listing as not usable) is logged at WARNING at most once an hour. Before this, both dropped such batches as expired or gone, so a paid batch left the pool and state file while its ownership record still said `"pool"`. `_purchase_stamp` always adds a batch Bee accepted: usable when the wait saw it usable (even if the follow-up listing misses it), held otherwise, including when the purchase is cancelled after Bee returned the id.

Owner addresses are canonical (lowercase, `0x`-prefixed, validated) on `register_stamp`, on every `check_access` comparison, and at load, where mixed-case records written before #384 are normalised; an owner that is not `"shared"`, `"pool"` or a valid address makes the registry unreadable. **Batch ids are keyed in lowercase (#558):** every `StampOwnershipManager` method that takes a batch id (`register_stamp`, `check_access`, `get_stamp_info`, `remove_stamp`, `cleanup_expired`) lowercases it, so a caller that forgets to lowercase cannot miss a record. At load, mixed-case keys from older files are lowercased and the file is rewritten through the normal atomic save, only when something changed. Before the first save after a migration, the original is copied atomically (temp file, fsync, rename) to `<file>.pre-558-<UTC timestamp>`. An identical existing copy is reused; a name already taken gets a `-N` suffix. Merged spellings' `mode`, `acquired_at` and `source` survive only there. While that copy fails, **no** save writes the file: not the migration's, and not a later `register_stamp`/`remove_stamp`/`cleanup_expired`. Each refused save logs an ERROR and keeps the change in memory only, as a failed write already does. While saves are refused, `/health` shows a fixed advisory warning in `state.warnings` (no path, no exception text) that does not change `status`, because a restart would lose the in-memory changes; that warning clears when the copy succeeds. A failed write of the registry file (after a successful copy, or at any other time) shows a second fixed warning, which clears on the next successful write, so `state.warnings` is empty only once a save has succeeded as a whole (`StampOwnershipManager.health_warnings()`, recovery in `docs/mainnet-runbook.md` §4). Each later save retries the copy first. A failed copy never leaves a partial `.pre-558-` file. The original stays intact, and the next start migrates it again. Keys that differ only in case are grouped first and decided on the set of distinct owners, so file order never matters: one owner → merged, keeping the record already under the lowercase key (else the first spelling in sorted order); exactly one wallet plus `"shared"` and/or `"pool"` → the wallet wins (WARNING naming every exact spelling and every owner), since `"shared"` would open the batch to everyone and `"pool"` would let it be resold; anything else (two or more wallets, or `"shared"` and `"pool"` with no wallet) → the registry is unreadable and startup stops, so no owner is guessed; the error names every spelling exactly and every owner, and is also logged at ERROR. To recover, keep the real owner's record and delete the other keys. To find the real owner, search the x402 audit log (`X402_AUDIT_LOG_PATH`, JSON lines) for the batch id in any case. A `payment_delivered` event has the payer in `wallet_address`, and `data.path` and `data.resource` (`batchID` or `batch_id`) say what was paid for. With `data.path` `/api/v1/stamps/` (also the late delivery of a pending purchase) or `/api/v1/pool/acquire`, the payer is the owner. With `/api/v1/stamps/for-owner` it names the payer, not the owner. Those batches carry `source: "created_for_owner"` in the registry, and the on-chain batch owner decides (their `stamp_purchased` event also names the payer, or the owner only when there was no payer). A `PATCH .../extend` event names whoever paid for the top-up, not the owner. Free-tier and pre-x402 batches leave no wallet record, and `stamp_tracker` keeps only purchase times in memory. **Owner proof** (#384): the upload routes (`POST /data/`, `/data/manifest`) accept the owner on the free tier without a payment, by headers `X-Owner-Timestamp` + `X-Owner-Signature` = EIP-191 signature over `swarm-connect-owner-upload:<stamp_id lowercase>:<ts>` (`verify_owner_proof` in `app/services/signed_auth.py`, window `DEBUG_SIG_MAX_AGE_SECONDS`). Each proof is single-use (in-memory used set, sound for the single uvicorn process; proofs signed before process start are refused, since a restart empties the set) and bound to one batch. It is reserved when it grants access (a concurrent request with the same proof gets 401 "in use"), confirmed when Bee returns a reference, and released when the request fails before Bee could have stored anything (#470): a later validation, settlement, a Bee connect error or a Bee 4xx. A timeout, 5xx or cancellation during the Bee call keeps it spent (fail closed); `_upload_to_bee` in `data.py` decides, and a `finally` in each upload handler releases an unsettled reservation. Entries expire with the proof's timestamp window, so an abandoned reservation cannot stay stuck. Both routes lowercase `stamp_id` on entry (#473), since the registry is keyed in lowercase and the query accepts either case. It is a bearer credential for one upload (not bound to the content). The proof is only checked when the x402 payer alone does not grant access. The x402 dependency refuses (402) a verified payment whose payer is not a valid address, before the handler runs, since that payer would become an owner. **Pre-deploy check** (an invalid saved owner stops startup): `jq -r '.[].owner' "$STAMP_OWNERSHIP_FILE" | grep -vxE 'shared|pool|(0x)?[0-9a-fA-F]{40}'` must print nothing. So must `jq -r 'keys[] | ascii_downcase' "$STAMP_OWNERSHIP_FILE" | sort | uniq -d`, unless every id it lists has one distinct owner, or exactly one wallet plus `shared`/`pool` (#558). Owners are compared after normalisation (lowercase, `0x`-prefixed), as the load compares them, so `0xAA…` and `0xaa…` (or `aa…` without `0x`) count as one owner. Such an id is merged at load and needs no manual fix. To list one id's owners the same way: `jq -r --arg id <id in lowercase> 'to_entries[] | select(.key | ascii_downcase == $id) | .value.owner | ascii_downcase | ltrimstr("0x")' "$STAMP_OWNERSHIP_FILE" | sort -u`. Bad proof → `401 OWNER_PROOF_INVALID`; valid proof from a non-owner → `403 STAMP_OWNERSHIP_DENIED`.

**Access mode field** (included in all stamp responses):
- `accessMode`: `"owned"` (exclusive to a wallet via x402), `"shared"` (free tier), or `null` (not tracked)

#### Data Operations
- `POST /api/v1/data/?stamp_id={id}&content_type={type}&redundancy={level}`: Upload raw data to Swarm (redundancy 0-4, default 2)
- `POST /api/v1/data/manifest?stamp_id={id}&redundancy={level}`: Upload TAR archive as collection/manifest (15x faster for batch uploads)
- **Paid upload pricing (#365, #429)**: bandwidth, not storage. `_calculate_price_for_request` prices `Content-Length` (or `MAX_UPLOAD_SIZE_MB` without one) multiplied by the erasure-coding overhead of the effective redundancy level: the `redundancy` query parameter, parsed as the endpoint parses it, else `DEFAULT_REDUNDANCY_LEVEL` (2), which the gateway always sends to Bee. The factor is `(data + parity) / data` for a full group, from `REDUNDANCY_ERASURE_GROUP` in `swarm_api.py` (Bee v2.8.1 `pkg/file/redundancy/level.go`): 1.076, 1.196, 1.320, 3.282 for levels 1-4. It underprices small files (more parity per chunk, plus root replicas); see `docs/x402-operator-guide.md`. `GET /api/v1/pricing` takes the same `redundancy` parameter and forwards it, so the quote equals the 402.
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
- `GET /api/v1/pool/status`: Get pool status and reserve levels. Unauthenticated, so its `errors` name a Bee failure by HTTP status or exception class only (plus Bee's own message for a refused purchase, with URLs replaced by `<url>` and addresses and internal host names by `<address>`/`<host>`, scrubbed and cut by `public_bee_text` as described under `/health` above, #549), never the exception text, which for an httpx2 `HTTPStatusError` contains the internal Bee API URL; the full text is in the gateway log (#534).
- `POST /api/v1/pool/acquire`: Acquire stamp from pool instantly (<5 seconds vs >1 minute). **Priced** from what the pool paid — `STAMP_POOL_DEFAULT_DURATION_HOURS + 1` at the requested depth, matching `_purchase_stamp`'s cost basis — plus `X402_POOL_MARKUP_PERCENT` (default 100, i.e. double). Before this the path matched no branch in the pricing dispatcher and fell through to `X402_MIN_PRICE_USD` regardless of size, so a depth-20 batch was quoted the same as the floor. **Payment is optional here**: a caller presenting an `X-PAYMENT` header has it settled and **bypasses the allowance entirely** — the budget bounds what the operator gives away, not what someone has paid for. A caller presenting none falls through to the allowance. **The bypass is withheld on a test network** (`is_testnet_network`, anything not recognised as mainnet), because testnet USDC is free from a faucet and honouring it would replace a bounded giveaway with an unbounded one — capped only by `STAMP_POOL_MAX_PURCHASES_PER_HOUR`, roughly 4 BZZ a day. Production ran `X402_NETWORK=base-sepolia` against the public `x402.org` facilitator when this was written, so it would have applied there. The payment is still settled and the batch still registered to the payer; only the bypass is withheld, and the 429 message then points at direct purchase rather than a payment that would not help. `X402_ALLOW_TESTNET_PAID_BYPASS=true` overrides it so staging can exercise the path. The router uses `settle_payment_if_offered`, not `require_x402_payment`, because the latter answers 402 to any caller sending no payment header, which is every current caller. Subject to a **daily allowance per size**, with a bucket per **configured** origin and one **shared** bucket for everything else — an unlisted origin does not get its own allowance, because the header is caller-supplied and rotating it would otherwise mint unlimited budgets (`POOL_DAILY_ALLOWANCES`, e.g. `https://app.example=50`; unlisted origins and callers sending no `Origin` use `POOL_DEFAULT_DAILY_ALLOWANCE`, `-1` = unlimited). Returns `429` with code `DAILY_STAMP_ALLOWANCE_EXHAUSTED` and a message written to be shown to a person, plus `resets_at` and the direct-purchase alternative. `POOL_ALLOWANCE_PER_IP` adds a per-client sub-limit (keyed by `get_client_key`: IPv4 address or IPv6 /64) inside each origin's bucket (code `DAILY_STAMP_ALLOWANCE_PER_CLIENT_EXHAUSTED`; counted per origin and size). Where paying would bypass the allowance (x402 on, mainnet), either exhaustion answers **402** with `accepts` and the same detail fields instead of 429; if the price cannot be computed it falls back to 429. An empty pool is answered 409 before any allowance check, so no payment is offered for a size that is out of stock. Consumed only once a batch is actually handed over, so an empty pool costs the caller nothing. **Origin is attribution, not authentication** — a browser cannot forge another site's, so this stops other websites spending the operator's postage, but any non-browser client can claim any origin; the budget is the control. Counters persist across restarts, or a crash loop would grant a fresh allowance each time.
- `GET /api/v1/pool/available`: List available stamps in pool
> **Top-up follows the reserve config**: only depths the pool still wants are kept alive. A depth whose target drops to 0 is not replaced when handed out, so paying to extend it maintains inventory the configuration has stopped carrying — batches at a dropped depth are left to expire and sync removes them. They stay usable until then.

> **A reserved batch is not topped up (#475)**: `_topup_stamp` checks, under the pool lock and after its last await before the spend, that the batch is still `AVAILABLE`. A batch reserved for a settling acquire (or already handed out) since the top-up list was built is skipped: no extend call, the hourly slot is given back, it is logged at INFO and is not recorded as an error or refusal. **While the extend call is in flight the batch cannot be acquired (#540)**: in the same locked step `_topup_stamp` adds it to `_topup_in_flight`, and removes it in a `finally` on every exit. `reserve_stamp`, `release_stamp`, `get_available_stamp` and `get_available_stamp_any_size` skip a batch in that set, so an acquire takes another batch; if it was the only one of that size, the acquire answers as for an empty size (larger-size fallback for free callers, 409 for paid ones) rather than waiting on Bee. Status, `/pool/available` and the replenish counts still count it, so a running top-up does not trigger a purchase.

> **An unreadable pool state file is never written over (#547)**: while the pool is paused because `pool_state.json` cannot be read, `_save_state` refuses every write (releases, in-flight purchases, expiry removals, sync cleanup), and a save that finds the file broken between syncs sets the same pause itself. A maintenance run re-checks the pause before each purchase and top-up, and `_purchase_stamp` and `_topup_stamp` check it again right before Bee is asked to spend (after the price lookup, and before every 429 retry), giving back the hourly slot (and, for a purchase, the ceiling hold; a top-up has not taken its hold yet at that point), so nothing is spent once the pause is set, including by an immediate replenishment. An extend or purchase call already sent to Bee when the pause is set still completes. Refused saves log the full batch-id list at ERROR, once and then only when it changes. Recovery: repair, restore or move the file away. The pause lifts on the next sync that either takes the first-run path or completes its Bee read, and that sync always writes the reconciled pool (in-memory batches, batches it imported from the file, and carried unreadable ids), whether or not a save was refused during the pause, so a restart afterwards starts from the full pool. It logs "Pool state file (pool_state.json) readable again; pause lifted, wrote N batch ids" at INFO. The first-run path is taken when the file is missing, holds a valid empty JSON list `[]`, or lists only batches released during the pause (those are subtracted first), and no purchase Bee has not listed yet is pending (a pending one makes the sync read Bee); it does not call Bee and writes the in-memory pool plus carried unreadable ids. A zero-byte or otherwise unparseable file is still unreadable and stays paused. A sync that reads a restored file but then fails at Bee does not: saves stay refused until the pool has been reconciled with the file. Batches released during the pause are subtracted from what sync imports, so a restored backup cannot put a sold batch back up for sale (in-process only; after a restart the ownership registry guard from #477 covers it). Batches bought during the pause are held only in memory too, so a restart before recovery loses them from the pool unless the file is rebuilt from the logged batch list. Separately, ids a sync keeps in state without importing are carried by every later save, and every sync that reads Bee evaluates them again even if the file no longer lists them (a restored older backup), so recovery does not drop them: an id with unreadable record data is kept until a sync imports it or Bee no longer lists it, an id whose ownership lookup failed is kept whatever Bee says until the lookup succeeds, and the first-run path, which does not read Bee, keeps them all. Before this a release dropped them from the file. While saves are held and the last failure is Bee's, the skip entry in `/pool/status` says saves stay held until a sync with Bee completes, without naming the file (the cause is Bee, #533).

> **Spend ceiling**: the pool refuses to make more than `STAMP_POOL_MAX_PURCHASES_PER_HOUR` (default 10) BZZ-spending operations in a rolling hour, across every depth, every kind of spend and every code path — the scheduled check, the immediate replenishment after an acquire, and anything added later. Enforced inside `_purchase_stamp` rather than in the replenish loop so no caller can miss it, and counted when Bee accepts the batch rather than when it becomes usable, since that is when the money is spent. Refusals are reported in `GET /api/v1/pool/status` errors. This exists because a staging node once bought 82 batches against a target of 5 (#271) and the bulk of it was never explained — a ceiling does not need the cause. **Topping up counts against the same limit** as buying (#334): extending a batch calls `extend_postage_stamp`, which spends BZZ exactly as buying does, and it was previously uncounted — the only path in the gateway with no bound at all. One shared limit rather than one per kind of spend, or the combined total would be twice the configured ceiling. The internals are named `_spend_times` / `_record_spend` rather than `_purchase_*` because the old names are how the gap survived review: a reader sees `_record_purchase`, correctly concludes a top-up is not a purchase, and reads past it.

- `POST /api/v1/pool/check`: Schedule manual pool maintenance. **Operator-only — it spends BZZ.** Requires an EIP-191 signature over `swarm-connect-pool-check:<unix_ts>` from an address in `POOL_ADMIN_ADDRESSES` (headers `X-Debug-Timestamp` + `X-Debug-Signature`), a deliberately separate allow-list from `DEBUG_ALLOWED_ADDRESSES` so a diagnostics signature cannot authorise spending. 404 when the list is empty (the default). Returns `202` and schedules the work — poll `GET /api/v1/pool/status` for the outcome, since a purchase takes ~16s and awaiting it held the caller's connection (#292).

#### Notary Signing (Provenance)
- `GET /api/v1/notary/info`: Check notary availability and get public address for verification
- `GET /api/v1/notary/status`: Simplified notary status for health checks
- `POST /api/v1/data/?sign=notary`: Upload with notary signature (adds `sign` parameter to data upload)

### Dependencies and Tech Stack

- **FastAPI**: Web framework with automatic OpenAPI documentation
- **Uvicorn**: ASGI server with performance extras
- **httpx2**: Async HTTP client for Swarm API integration (AsyncClient with connection pooling), Pydantic's maintained fork of httpx (#595); httpx itself stays installed for x402
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

**Current Status**: Deployed on both staging (`dev`) and production (`main`); switched per environment by the `X402_ENABLED` / `X402_NETWORK` GitHub variables read in `deploy.yml` (`X402_ENABLED` defaults to `false` there, so the variable is what turns it on). Network and facilitator defaults: `base-sepolia` and `https://x402.org/facilitator` for dev; **none** for main (#370), so a missing value stops production at startup. Network, facilitator URL, pay-to address and facilitator credentials change together in one deploy at the mainnet cutover (`docs/mainnet-runbook.md` §1). `GET /` reports `x402.enabled` for a running gateway.

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
├── dependency.py    # Router dependency: validate / 402 / free-tier opt-in / verify (require_x402_payment, settle_payment_if_offered)
├── middleware.py    # Settlement after a 2xx + response headers; network/USDC config
├── preflight.py     # Bee (Gnosis) wallet balance checks (can_accept)
├── accept_gate.py   # 503 PAYMENTS_UNAVAILABLE before payment while can_accept is false (#589)
├── payment_headers.py # Which payment headers a request carries: X-PAYMENT (v1), PAYMENT-SIGNATURE (v2) (#600)
├── base_balance.py  # Base ETH balance of the pay-to address (gates protected requests)
├── pricing.py       # Price calculation (BZZ → USD)
├── access.py        # IP whitelist/blacklist (not wired into requests yet, #379)
├── audit.py         # Audit log (JSON lines)
└── ratelimit.py     # Per-IP rate limiting (free tier)
```

### x402 Tests

`tests/test_x402_*.py` (preflight, pricing, middleware, access, audit, ratelimit, integration), plus `test_base_balance.py`. `test_x402_live.py` is opt-in (see TEST_STRATEGY.md).

### Key Configuration

```bash
X402_ENABLED=false           # Master switch (default: off)
X402_FACILITATOR_URL=...     # Payment facilitator (with CDP credentials: empty or exactly https://api.cdp.coinbase.com/platform/v2/x402)
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
| **Free tier** | Limited access | 3/min | request `X-Payment-Mode: free`; response echoes `free-tier` |
| **Whitelisted IPs** | Full access | No limit | - |
| **Blacklisted IPs** | Blocked | - | 403 |

### Protected Endpoints (when X402_ENABLED=true)

- `POST /api/v1/stamps/` - Requires payment OR free tier
- `POST /api/v1/data/` - Requires payment OR free tier
- `POST /api/v1/data/manifest` - Requires payment OR free tier
- `GET /api/v1/data/{ref}` - FREE (no payment required)

**Validation comes before payment (#427).** FastAPI runs router dependencies before it validates the route's own parameters, so `require_x402_payment` validates them itself first (`_validate_before_payment`). It checks the path, query, header, cookie and body parameters of the matched route with FastAPI's own `request_params_to_args` and `request_body_to_args`, on the body FastAPI has already read. A malformed request to a protected route gets `422 VALIDATION_ERROR` before it is priced, before a 402, before it uses a free-tier slot, and before a payment is verified or reserved. This also covers `POST /pool/acquire` with a payment offered, through `settle_payment_if_offered`. `POST /chunks/credit` takes `mb` as a string on purpose (#260/#261), so FastAPI accepts any value; `_check_before_payment` applies the handler's own rule (`valid_topup_mb` in `app/services/bandwidth_credit.py`: a plain integer of at least 1) and the same 422 before payment. Other checks a handler makes itself (labels, spending limits, an unknown batch) still run after the payment step. The check uses FastAPI internals and handles both 0.127 (`scope["route"]`) and 0.141 (the effective route context, `strict_content_type`). If a later FastAPI changes them, the check is skipped with one WARNING per process ("could not validate the request before payment"), and at DEBUG for each later request, and FastAPI validates after the payment step, as before #427. Any other error during the check (for example a client disconnect) skips it for that request only, at DEBUG. After upgrading FastAPI, run `tests/test_x402_validate_before_payment.py`.

**Payments refused while the gateway cannot accept them (#589).** `require_x402_payment`, right after validation and before pricing, refuses with `503 PAYMENTS_UNAVAILABLE` (`Retry-After` = the snapshot TTL, `detail.reason` `balances_unreadable` or `snapshot_too_old`) every request that would pay or be offered a 402, when the cached `/health` snapshot says `can_accept` is false (the xBZZ or xDAI balance could not be read) or is too old to trust (`app/x402/accept_gate.py`). Nothing is priced, verified, reserved or settled. Free-tier requests (`X-Payment-Mode: free` with the free tier on) are not gated, nor is a retry of an attempt that may have been charged (`accept_gate.is_retry_of_possibly_charged`: an `X-PAYMENT` whose authorization is in flight or `AFTER_WORK` in the outcome-unknown registry, or reserved and unexpired in the replay guard, or an `Idempotency-Key` with a stored entry for that authorization's payer on the route; read-only in-memory lookups, no Bee call, no write). Its existing path answers from stored state without a new settlement: the replayed result, a 409, or the 402 "already used". A `BEFORE_WORK` authorization is NOT exempt: `settle_payment` would settle it again, possibly for the first time, so it is refused like a new payment. Backstop: an exempted request that still gets past the replay guard and the idempotency lookup (its state changed meanwhile) is refused with the same 503 before it settles, releasing its reservation and idempotency key. Limitation: the gate not refusing it is all that is guaranteed. The request is still priced before verify, and pricing can call Bee, so while Bee cannot price such a retry can get 503 "Failed to calculate price" (or `BEE_BUSY`) instead of its stored result or 409. It covers every paid route, including a paid `POST /pool/acquire` (through `settle_payment_if_offered`); an unpaid acquire uses the free allowance as before, and while payments are refused its allowance-exhausted refusal is the 429 form, without the 402 offer and with no `Retry-After` (the free allowance stays spent until `resets_at`, so a client honouring it would loop). Where `paid_bypass_is_honoured()`, the message says the paid acquire (`X-PAYMENT`) is temporarily refused and does not point at `POST /api/v1/stamps/`; otherwise (a testnet without the override, where a paid acquire never bypasses the allowance) it keeps the direct-purchase guidance, since the free tier of `POST /api/v1/stamps/` is not gated. `PAYMENTS_` is in the Bee call gate's payment code prefixes (`app/middleware/bee_gate.py`), so a 503 PAYMENTS_UNAVAILABLE is never rewritten to BEE_BUSY. The check reads the snapshot in memory only (`health_snapshot.peek`): no Bee call per request. With no snapshot yet (startup) it lets requests through, as before. Counted in `gateway_x402_payments_total{mode="unavailable"}`. `GET /pricing` is unaffected.

**Payment headers of either x402 version (#600).** Whether a request offers a payment is decided in one place, `app/x402/payment_headers.py` (`offered_payment_versions`, `offers_payment`): `X-PAYMENT` is v1, `PAYMENT-SIGNATURE` is v2, matched by header name in any letter case, on Starlette headers or raw ASGI scope headers. `require_x402_payment`, `settle_payment_if_offered` and the Bee call gate's `_offers_payment` all use it. Only v1 is settled; there is no setting for v2 yet. On every paid route (stamp purchase and extend, buy-batch-for-owner, uploads, manifest, `/chunks/credit`, and `/pool/acquire` through `settle_payment_if_offered`): both headers together get `400 PAYMENT_HEADER_CONFLICT` right after request validation, before the #589 gate and before pricing; `PAYMENT-SIGNATURE` alone gets `402 X402_VERSION_UNSUPPORTED` right after the payment requirements are built, before the free-tier branch, the no-header 402 and verify. Its body is the ordinary spec-shaped v1 Payment Required body (`x402Version` 1, `error`, `accepts`, `freeTier` when available) with that code, so a v1 client can still pay it. So a v2 request never reaches the free tier, the pool allowance, the facilitator or the handler, also with `X-Payment-Mode: free`. Ordering: a v2-only request without `X-Payment-Mode: free` while payments are unavailable gets `503 PAYMENTS_UNAVAILABLE` first, and a pricing failure stays 503; both say nothing was charged. An empty `PAYMENT-SIGNATURE` counts as present (fails closed); an empty `X-PAYMENT` alone is still no payment, as before. Not affected: x402 disabled (both headers ignored, nothing is paid), GET requests, and the chunk upload `POST /api/v1/chunks/` (it reads neither header; it spends credit or the free quota). The Bee call gate treats `PAYMENT-SIGNATURE` as a payment: refused up front with BEE_BUSY when no slot is free, and never rewritten. `accept_gate.is_retry_of_possibly_charged` reads `X-PAYMENT` only, since no v2 attempt can have been charged. Counted in `gateway_x402_payments_total{mode="version_unsupported"|"header_conflict"}`. Phase 2 of #596 (`X402_PROTOCOL_VERSIONS`) replaces the v2-only 402.

### Idempotency-Key (paid requests, #359)

Paid POSTs accept an `Idempotency-Key` header (`app/x402/idempotency.py`). Scoped to (payer, method, path, key). Order in `require_x402_payment`: request validation (#427, see above) → facilitator verify (the verified signature makes `from` the payer; for keyed requests a facilitator-reported payer must equal it) → replay guard reserves the new authorization → idempotency lookup (any refusal or replay releases the reservation; nothing is settled). A repeat returns the stored 2xx response with `Idempotent-Replayed: true` (the dependency raises `IdempotentReplay`, which `X402Middleware` turns into the response) and writes a `payment_idempotent_replay` audit event. The authorization that paid for the original is refused by hash, since verify does not reject a spent nonce. Before either settle point calls the facilitator, a `settling` entry (with the authorization's nonce) is persisted; success promotes it to `settled` (with the transaction), a definite refusal (`success=False`) deletes it, and an exception, timeout or cancellation leaves it, so a retry gets `409 IDEMPOTENCY_KEY_SETTLEMENT_UNKNOWN` naming the nonce to check on-chain. The key is released only when nothing can have been collected. A 2xx too large to store marks the entry `delivered_not_stored` (`409 IDEMPOTENCY_KEY_DELIVERED_NOT_STORED`, "it succeeded"). `/chunks/credit` results are stored with the bearer token removed; a replay fills in `bandwidth_credit_manager.issue_token(payer)` (current token, never minted or rotated). `authorization_key()` canonicalises the nonce (bytes32 hex, `0x`-prefixed, lowercase) and payer (20-byte address) and refuses anything else, so the replay guard and the stored authorization hash cannot be bypassed by respelling. Entries missing `request_hash` or a known `state` make the file unreadable (fail closed). A retry while the first request runs → `409 IDEMPOTENCY_KEY_IN_PROGRESS`; after a paid request failed, raised, was cancelled or the gateway restarted → `409 IDEMPOTENCY_KEY_SETTLED_PENDING` naming the transaction; same key with a different query/body (hash stored; multipart hashed field by field) → `422 IDEMPOTENCY_KEY_REUSED`. Free-tier requests ignore the header.

State: `X402_IDEMPOTENCY_STATE_FILE` (`data/x402_idempotency.json`), 24 h TTL, at most `X402_IDEMPOTENCY_MAX_ENTRIES` (10000; completed entries evicted before settled-pending ones), bodies over 64 KB not stored (logged). The whole file is rewritten atomically on each change, never on the event loop: like the settlement-unknown registry, `_lock` guards memory only, writes snapshot under it and write under `_write_lock` with a sequence number, on the store's own I/O lane (`_idempotency_io`, `app/core/io_lane.py` IOLane; the registry has its own, so a hang on one file fails closed only the requests that need it: keyed requests for this one), coalesced (`schedule_persist` returns the queued write while it has not started; it snapshots when it starts) (`schedule_persist`/`apersist`). On the request path only the "settling" record is awaited (`arecord_settling`, bounded by `X402_SETTLEMENT_STATE_WRITE_TIMEOUT_SECONDS`): not on disk in time means 503 PAYMENT_STATE_UNAVAILABLE before settling, the entry taken back and the registry write-ahead given up (`give_up`); the registry entry is marked asked (`mark_asked`) only after that. Every other change (settled, refused, completed, resolved, pruned) is made in memory at once and written in the background. A failed write, or one that does not finish in time, makes the store `unavailable` (keyed paid requests get 503 `IDEMPOTENCY_UNAVAILABLE`) until a write succeeds; a retry or probe runs in the background at most every 5 s. The store methods keep a synchronous mode (`persist="sync"`, the default) for callers off the loop. An unreadable file is copied to `.corrupt-<ts>`, never overwritten, and keyed paid requests get `503 IDEMPOTENCY_UNAVAILABLE` until it is repaired (unkeyed requests work). `/health` shows it as an `x402.warnings` entry (unreadable, unwritable or unresponsive; status stays ok). In-flight markers are in memory: this assumes a **single gateway process** (no `--workers`).

Unknown settlement outcomes (#553): `X402_SETTLEMENT_UNKNOWN_STATE_FILE` (`data/x402_settlement_unknown.json`, `app/x402/settlement.py` `outcome_unknown`). An authorization whose settlement raised is recorded for 24 h (hashed key, wall-clock expiry, pruned on load, cap 10000 evicting only `before_work` entries, oldest first, never an `after_work` one (its work would run again), an in-flight entry or the new write-ahead; if the cap cannot be met that way, the write-ahead fails closed with 503), written atomically on each change; `atomic_write_json` also fsyncs the directory (best effort). Write-ahead: `begin_settlement` records the authorization (before_work in settle_payment, after_work in the fallback) before the facilitator is asked; a failed write refuses with 503 PAYMENT_STATE_UNAVAILABLE without settling (the fallback withholds the result and releases the authorization). `begin_settlement` reads the prior kind, inserts and writes under one lock, rolls the insert back if it cannot be kept or written, and marks the entry in flight (memory only): until the facilitator call ends with an unknown outcome, or, after a definite answer, until its resolution is written (the resolution thread releases it), so a duplicate in that window is told "in progress", never "outcome unknown". A definite answer removes the entry and writes that at once (`resolve`), so a resolved entry never outlives the process: two writes per paid request, run off the event loop (`abegin_settlement`/`aresolve`: a shielded executor future, so a cancelled caller cannot interrupt the write). If the caller is cancelled (or times out) during the write-ahead, a done-callback on the thread future releases the in-flight mark and removes the entry on the registry pool, generation-checked (the facilitator was never asked); if the process dies first, that entry stays, which only turns a refusal of that authorization into a 409. Records (audit, `record_settlement`, metric) come before the resolution write, so a cancellation then loses none. `_lock` guards memory only and is never held across disk I/O; writes snapshot under `_lock` and write under `_write_lock` with a sequence number, so an older snapshot never overwrites a newer one. No other registry write happens on the request path: the write-ahead entry already has the right kind for an unknown outcome or an interrupted settlement. The write retry while the file is unwritable runs in the background. All registry I/O runs on its own 2-worker pool of daemon threads (`_submit_registry_io`), not the default executor, which long blocking calls (on-chain createBatch) can saturate; daemon threads so a hung write cannot keep the process from exiting (concurrent.futures joins its workers at exit without a timeout). Lifespan shutdown waits for pending registry writes for at most 5 s (`shutdown_registry_io`). New write-aheads are refused with 503 (and the registry marked stalled) when the oldest pending registry write is older than the write timeout, or when `MAX_PENDING_REGISTRY_IO` (64) writes are pending; an ordinary burst queues. While stalled and no write is pending, a probe write runs in the background at most every `WRITE_RETRY_SECONDS`, and an abandoned write-ahead whose write landed clears the stall, so the stall always clears once the disk responds. A failed write-ahead's rollback and release only apply while it still owns the key (generation). The request path waits at most `X402_SETTLEMENT_STATE_WRITE_TIMEOUT_SECONDS` (default 5, validated > 0, <= 60, finite) for a write: a write-ahead that takes longer (hung disk) is answered 503 PAYMENT_STATE_UNAVAILABLE without settling, the registry is marked stalled (`unavailable`, `/health` cause `unresponsive`) until a write completes, the key is released at once, and the late write is handled like a cancelled begin. Un-asked write-aheads (written, facilitator not yet called; memory only, `_unasked`) are never a prior unknown outcome: reads report the write-ahead's own prior, a later write-ahead inherits it, and a begin for a key in flight is refused. The generation is allocated before the write is queued, so a caller can give up on a write-ahead that has not started. Each write-ahead has a generation (memory only); a late removal (abandoned begin) only removes the entry if it is still its generation and no settlement of the key is in flight, and a resolution only removes its own generation's entry. The `prior` kind, not the write-ahead entry, decides whether a refusal is 402 or 409. A parallel duplicate while an entry is in flight gets `409 PAYMENT_SETTLEMENT_IN_PROGRESS` (no Retry-After: resending that X-PAYMENT would meet a payable 402 once the first has settled) (audited `stage: duplicate_in_progress`, not a reconciliation case), checked in the dependency before the after_work and replay-guard checks. A test write at load makes an unwritable file unavailable at startup. A settlement interrupted by CancelledError is recorded too (`settlement_interrupted`): before_work in settle_payment (the middleware then answers 502 PAYMENT_SETTLEMENT_UNAVAILABLE, not "You were not charged", and keeps the authorization reserved), after_work in the fallback. There, an inner CancelledError (the facilitator call cancelled, not the request's task: `Task.cancelling()`, `_task_is_being_cancelled`) gets the structured 502 PAYMENT_SETTLEMENT_UNKNOWN; a cancellation of the task itself is re-raised. The record is written first either way. (Until #617 the image ran Python 3.10, which cannot tell the two apart, so every such CancelledError ended in the server's plain-text 500.) A remembered before_work authorization refused on a fallback route is upgraded to after_work. `before_work` (settle_payment, nothing ran): the same-X-PAYMENT retry is let through; a facilitator refusal of it (verify or settle) is `409 PAYMENT_AUTHORIZATION_OUTCOME_UNKNOWN`, never a 402 with accepts. `after_work` (middleware fallback, the work ran): refused with that 409 in the dependency, before the replay guard and any work. A later successful settlement removes the entry. Unreadable file: copied to `.corrupt-<ts>` (`atomic_io.unreadable_state`), never overwritten, and every X-PAYMENT request gets `503 PAYMENT_STATE_UNAVAILABLE` until it is repaired. A failed write does the same until a later write succeeds (retried in a background thread when `outcome_unknown.unavailable` is checked, at most every `WRITE_RETRY_SECONDS` = 5 s, so `/health` probes neither block on nor hammer the disk). Either state is an `x402.warnings` entry in `/health` (code, file base name, unreadable/unwritable; paths and error text only in the log) with `status: degraded` (an unavailable idempotency store is a warning only). Every 409 PAYMENT_AUTHORIZATION_OUTCOME_UNKNOWN is audited as `payment_failed`, `stage: outcome_unknown_retry`, with the nonce and where it was stopped (`after_work`, `verify`, `settle`, `in_use` = still reserved after an interrupted settlement).

Facilitator reasons in responses (#576): a facilitator's refusal reason reaches the caller only if it is a short machine code (`[a-z0-9_]{1,64}`, for example `insufficient_funds` or `invalid_exact_evm_nonce_already_used`); anything else is replaced by "facilitator error; details in the gateway log" (`public_facilitator_reason` in `app/x402/settlement.py`). This covers the `PAYMENT_SETTLEMENT_FAILED` message and `reason` from `settle_payment` and the middleware fallback, the `reason` of `PAYMENT_AUTHORIZATION_OUTCOME_UNKNOWN`, and the "Payment verification failed: ..." 402. Facilitators also answer with free text, such as a multi-line RPC error naming the RPC URL and quoting the request body (measured on Base Sepolia). The full reason goes to the log, and for settlement refusals and outcome-unknown retries also to the audit record.

### Free Tier Behavior

When `X402_FREE_TIER_ENABLED=true` (default):
- Users without x402 payment can still access protected endpoints
- Stricter rate limit applied (3 requests/minute by default)
- Opt in with request header `X-Payment-Mode: free` (the canonical request value)
- Response includes `X-Payment-Mode: free-tier` header
- When rate limit exceeded, returns 429 with payment upgrade info

When `X402_FREE_TIER_ENABLED=false`:
- Users without payment get HTTP 402 immediately
- Must provide valid x402 payment to access protected endpoints

### Development Notes

- x402 runs on staging and production - test on staging (`dev`) before merging to `main`
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
- `gateway_pool_acquires_total{size, status}` (`status` = `success`/`error`/`settled_not_delivered`; `error` is an empty pool or a reservation lost to another request, both answered 409, never an allowance refusal; `settled_not_delivered` is a paid acquire whose batch could not be handed over after settlement, refund needed)
- `gateway_pool_allowance_refusals_total{size, scope, response}` — free pool acquires refused because a daily allowance was used up (#554). `scope` = `origin` (per-application budget) or `client` (per-client cap); `response` = `402` (paying would bypass the allowance), `429`, or `409` (requested size out of stock and the allowance for the larger stand-in size spent; counted under the larger size). `size` is a preset or `other` for a non-preset pool batch, so the label stays bounded. Not counted: the 503 `POOL_ALLOWANCE_UNAVAILABLE` (records unreadable, no allowance ran out). No origin, IP or wallet is a label. Counted only from the deploy that added it
- `gateway_stamp_spend_refusals_total{operation, limit}` — purchases and extends refused by a spending limit (`limit` = `per_request` or `daily_budget`)
- `gateway_stamp_spend_bzz_total{operation, charged}` — BZZ committed through the stamp endpoints (`charged` = `budget` or `paid`). A paid purchase answered 202 and delivered later is counted when it is delivered (#468); one Bee refuses with a 4xx after the 202 has its spend hold released and is not counted
- `gateway_requests_by_client_total{client_type, outcome}` — which clients are served and whether they are (`client_type` = `mcp`/`cli`/`sdk-js`/`browser`/`curl`/`http-lib`/`bot`/`other`/`none`; `outcome` = `ok`/`client_error`/`rate_limited`/`payment_required`/`server_error`)
- `gateway_notary_signatures_total{status}` (`success` = a signed document that was then stored on Swarm, counted after the upload so the Notarized Share KPI cannot exceed 100%; `error` = signing refused or failed, including an unexpected signing exception, which is also counted once as an upload error. A signed upload that fails counts only as an upload error)
- `gateway_x402_payments_total{mode}` (paid/free/rejected/unavailable/version_unsupported/header_conflict; `unavailable` = refused before payment with `503 PAYMENTS_UNAVAILABLE`, #589; `version_unsupported` = `402 X402_VERSION_UNSUPPORTED` and `header_conflict` = `400 PAYMENT_HEADER_CONFLICT`, #600)
- `gateway_x402_settlements_total{result}`: `settled`, `refused` (nothing delivered or collected), `error` (outcome unknown, including a settlement interrupted by a shutdown or disconnect), `settled_not_delivered` (refund needed), `refused_after_unknown` (a retry of an authorization whose earlier settlement got no answer was refused, at verify or at settle, so the earlier transfer probably went through). All exist from startup; the "x402 settlement problems" alert fires on any `result!="settled"`
- `gateway_rate_limit_hits_total`
- `gateway_bee_api_errors_total{endpoint}` — failed calls from the gateway to its Bee node (`endpoint` = `batches`/`purchase`/`upload`/`chunk_upload`/`download`/`wallet`). All six exist from startup (#586). The "Bee calls failing" alert fires on more than 20 in 5 minutes, for 5 minutes: the process can keep answering `/metrics` (so `up` stays 1) while every Bee call fails
- `gateway_bee_poll_total{status}` — balance polls; `error` when any of the Bee node wallet reads (xBZZ, xDAI, chequebook) failed. Before #586 it counted `success` unless an exception escaped, which the check functions never let happen
- `gateway_state_save_failures_total{file}` — failed saves of a state file (#506): `file` = `pool` (stamp pool state) or `ownership` (ownership registry, including a save refused because the pre-migration copy failed, #558). The save logs at ERROR and the change stays in memory only; a restart loses it. The "State file save failures" alert fires on any increase, and stays active while `gateway_state_unsaved` is 1. A pool save refused during a #547 pause (unreadable file) is not counted here
- `gateway_state_unsaved{file}` — 1 while a state file is behind memory after a failed save, until a save succeeds (#506): `file=ownership` while the registry's last write failed or saves are refused (#558); `file=pool` after a failed write of the pool state (a dedicated flag, not the #547 pause; 0 while the pool is disabled). Read at scrape time; both series exist from startup. Nothing retries a failed ownership save and the pool retries only on its next sync, so this, not the counter's 15-minute window, keeps the alert active while the change is unsaved
- `gateway_chunk_uploads_total{status, mode}` (mode = paid/free/none), `gateway_chunk_upload_bytes_total`
- `gateway_bandwidth_topups_total{status}`, `gateway_bandwidth_topup_bytes_total`
- `gateway_bee_http_pool_timeouts_total` — outbound calls of the shared HTTP client (Bee API, Base RPC) that got no pool connection in time (`PoolTimeout`) (#587)
- `gateway_bee_calls_in_flight{lane}` (scrape time) and `gateway_bee_call_rejections_total{lane}` — Bee call gate (#588); lane `user|transfer|committed|background`, plus `admission` for paid requests refused before payment
- `gateway_health_snapshot_age_seconds` (-1 before the first) and `gateway_health_snapshot_refreshes_total{result=ok|error|timeout}` — the `/health` snapshot (#588)
- `gateway_top_caller_requests{rank}` (1..`TOP_CALLERS_N`), `gateway_top_callers_distinct`, `gateway_top_callers_untracked_total` — heaviest callers in the last complete window; ranks only, never a caller (#588)
- `gateway_bee_http_client_replacements_total{result}` — recovery checks of the shared HTTP client after repeated `PoolTimeout`s (#587): `replaced`; `replaced_unverified` (the pool could not be read, so the Bee probe alone decided); `probe_failed` (Bee did not answer on a new connection either, so the client was kept); `not_stuck` (the pool is busy, not holding leaked slots, so the client was kept)

**Series created at 0 at startup (#518):** `gateway_uploads_total{status=success|error}`, `gateway_downloads_total{status=success|error|too_large}`, `gateway_notary_signatures_total{status=success|error}`, `gateway_pool_acquires_total{size=small|medium|large, status=success|error|settled_not_delivered}`, `gateway_pool_allowance_refusals_total{size=small|medium|large|other, scope=origin|client, response=402|429|409}`, `gateway_x402_settlements_total{result}`, `gateway_state_save_failures_total{file=pool|ownership}`, `gateway_bee_http_pool_timeouts_total`, `gateway_bee_http_client_replacements_total{result}` (#587), `gateway_bee_call_rejections_total{lane}`, `gateway_health_snapshot_refreshes_total{result}`, `gateway_top_callers_untracked_total` (#588), `gateway_x402_payments_total{mode="unavailable"}` (#589), `gateway_x402_payments_total{mode="version_unsupported"|"header_conflict"}` (#600) and `gateway_bee_api_errors_total{endpoint}` (#586; a test reads the endpoints from `swarm_api.py`). A series created on its first event first appears at 1, and `rate()`/`increase()` have no earlier sample, so they count that event as 0; a rare event such as `settled_not_delivered` would be missed by a rate panel. A fresh gateway also shows 0 instead of "No data". A restart does not cause an undercount for a series that already existed: Prometheus treats the drop as a counter reset and counts the first event after it. Only bounded label values known at startup are created; `gateway_stamp_purchases_total` is not, because direct purchases take any depth, so its `size` can be `depth-N`.

**Custom gauges** (polled every `METRICS_BALANCE_POLL_SECONDS`):
- `gateway_wallet_bzz_balance`, `gateway_wallet_xdai_balance`
- `gateway_chequebook_available_balance`, `gateway_base_eth_balance`
  - These keep their **last value** when a read fails, so a Bee or RPC hiccup does not page as an empty wallet. So a balance gauge alone cannot say whether its value is current. On 2026-11-03 every wallet read failed for over 30 minutes and the gauges kept the last good balance. The chequebook gauge read a key the check never returns, so it stayed at 0 until #586 fixed it.
- `gateway_balance_check_ok{check}` — 1 when the last poll read that balance, 0 when the read failed and the balance gauge shows an older value (#586). A balance read successfully but below threshold is 1. `check` = `xbzz`/`xdai`/`chequebook` (always), `base_eth` (when `X402_ENABLED`), `gnosis_signer` (when `STAMP_PURCHASE_FOR_OTHERS_ENABLED`). The enabled checks exist from startup at 0. The `gnosis_signer` series is created at startup whenever `STAMP_PURCHASE_FOR_OTHERS_ENABLED` is on, and removed on the first poll if no signer is configured, so it does not report a failure for a signer that does not exist
- `gateway_balance_last_success_timestamp_seconds{check}` — Unix time of the last successful read, 0 until there is one; catches a poll loop that stopped altogether. The "Balance check failing" alert fires when a check is 0 or none succeeded for 15 minutes, for 15 minutes
- `gateway_stamp_pool_available{size}`, `gateway_stamps_total`
- `gateway_stamp_min_ttl_seconds`, `gateway_uptime_seconds`
- `gateway_bandwidth_credit_accounts`, `gateway_bandwidth_credit_bytes_total` (when `CHUNK_UPLOAD_ENABLED`)
- `gateway_stamp_spend_callers`, `gateway_stamp_spend_bzz_today` — callers holding a spend balance today, and the BZZ charged to budgets so far. Polled rather than accumulated, because the day rolls over inside the tracker and a counter would keep climbing past midnight UTC.
- `gateway_spend_uncertain_bzz_total{operation}` — BZZ kept charged against the spending limits after a failure whose outcome is uncertain (#363). A paid purchase answered 202 is settled once, by its background half: delivered (counted in `gateway_stamp_spend_bzz_total`), refused by Bee with a 4xx (hold released), or anything else (not found, found but owned by someone else, lookup error, shutdown, other Bee answers: counted here). A request cancelled after the 202 hand-off is not counted a second time (#468)
- `gateway_state_file_unreadable{file}` — 1 while a state file that fails closed cannot be read (#468), read from each store's own flag at scrape time, so it needs no traffic. `file` = `pool` (pool spending paused until a full sync reconciles the file, #547; 0 while the pool is disabled), `pool_spend_times` (the pool's hourly spend record `<pool state>_spend_times.json` was unreadable at startup: pool purchases and top-ups paused for an hour, then 0 by itself), `settlement_unknown` (every paid request 503), `idempotency` (keyed paid requests 503), `spend_budget` (limited stamp spending refused until the next UTC day), `pool_allowance` (free pool acquires refused until the next UTC day). All six exist from startup. The "State file unreadable" alert fires while any is 1. Not here: an unreadable ownership registry or bandwidth credit ledger stops startup, which "Gateway Down" reports
- `gateway_distinct_callers` — how many separate callers today. A count, not a list. Computed at scrape time, so it drops to 0 at midnight UTC without waiting for a request (#521); it also resets on restart.
- `gateway_distinct_callers_previous_day` — the previous UTC day's final count, exported all through the next day. The first scrape after midnight already reads the new day, so callers seen after the last scrape before midnight are in no sample of `gateway_distinct_callers`; the per-day panel takes this value too, as the highest sample exported during the following UTC day, and attributes it to its own day. Its window is that day exactly: `[1d] offset -1d23h59m59s999ms` takes samples after the previous midnight up to 1 ms before the next one, so a sample stored exactly at midnight belongs to the day that midnight starts. A subquery is not used there because its steps are aligned to whole minutes, and a step exactly at midnight would read either the next day's export (window end) or, by lookback, the day before's (window start). 0 when the process started after the previous day ended, or no rollover fell between the two days. After a restart partway through a day, that day's value is the count since the restart, not the whole day's; the panel takes the larger of it and the day's highest count, so this never overcounts.

**Pool gauges of the shared HTTP client** (read at scrape time, #587): `gateway_bee_http_pool_max_connections` (the configured `BEE_HTTP_MAX_CONNECTIONS`), `gateway_bee_http_pool_connections{state}` (`active` = serving a request, `idle` = kept alive, `stuck` = neither idle nor closed and held by no request, the slots httpcore 1.0.9 leaked on cancellation) and `gateway_bee_http_pool_requests` (requests waiting for or holding a connection). NaN when the pool cannot be read (a future httpx2 moved its internals); 0 before the client exists. The warning alert **Bee HTTP pool slots stuck** fires when stuck connections are at least 10% of the max for 10 minutes, per environment and host: the partial-leak phase, before self-recovery acts (it needs 25% and repeated `PoolTimeout`s, and any response resets the count). Operator action: find the source of the cancellations; if the stuck count keeps rising, restart the gateway container at a quiet moment; a restart cuts off every request in flight, including uploads, downloads and paid purchases waiting on Bee (docs/mainnet-runbook.md section 5).

**Info**: `gateway_info{version, environment, x402_enabled, pool_enabled, notary_enabled, chunk_upload_enabled}`

**Who we serve, and the line on caller data (#347).** Production traffic turned out to be almost entirely one client — the MCP plugin, run by AI agents in cloud sandboxes — and 37 of its 84 production requests were being refused with 429, because the free tier allows three requests a minute and an agent making several calls in sequence exceeds it. Finding that meant reading reverse-proxy access logs by hand on the host. `gateway_requests_by_client_total` makes it a dashboard panel. The Product KPIs row also charts it per UTC day by `client_type`, pinned to production (panel 119, #519). The data provenance app is counted as `browser` with every other browser, so app traffic cannot yet be told apart from other browser traffic.

`client_type` comes from a fixed table in `app/services/client_type.py`, **never from the `User-Agent` directly**. A metric label must not take a value the caller chooses, or anyone can mint unlimited time series — a cost attack on the metrics bill and a Prometheus instance that slows for everyone. Anything unrecognised becomes `other`.

The rule this follows: **counts and buckets, never per-user records.** No hashed or truncated IP appears in any label — a hashed IP is pseudonymised, not anonymised, so it is still personal data, *and* still high-cardinality, failing on privacy and cost at once. `gateway_distinct_callers` needs to recognise a repeat caller to count distinctness, so the middleware keeps a salted hash in memory with a random per-process salt, and exports only the total: the values cannot be reversed, cannot be checked against a precomputed table, and cannot be correlated across restarts or between the two gateways. Nothing is persisted. A test asserts no caller string reaches `/metrics`.

Per-caller detail stays in the logs, where retention is short and access is controlled, and is read when someone is investigating rather than exported continuously to a third party.

`ClientMetricsMiddleware` is registered **after** the rate limiter and x402 so that it wraps them — Starlette runs the most recently added middleware outermost, so it sees the final response including their 429s and 402s, which are the outcomes the counter exists for. It is plain ASGI rather than a `BaseHTTPMiddleware` subclass: that base class wraps each request in an anyio task group and re-plumbs the streams, which deadlocked the entire test suite when stacked behind the existing middleware. `/metrics`, `/health` and `/` are excluded — Alloy scrapes and Docker healthchecks are the two most frequent requests the gateway sees and would bury everything else.

**Bee chain-backend metrics** (scraped from the bundled Bee nodes, not produced by the gateway):
- `bee_eth_backend_total_rpc_calls` / `bee_eth_backend_total_rpc_errors` — Gnosis RPC volume and failures
- `bee_eth_backend_cache_block_number_load_errors` — failures refreshing the cached chain tip, the specific cause of a broken `/chainstate`
- `bee_eth_backend_calls_*` — per JSON-RPC method (`filter_logs`, `send_transaction`, `eth_call`, `balance`, …), each its own counter rather than a label
- `bee_eth_backend_average_block_time_seconds` — block time as the node observes it

**Public /health probe** (#586, `job="swarm_connect/health_probe"`, labels `environment`, `host`, `instance` = the URL): Alloy's blackbox exporter calls each environment's public `/health` every 60 s through DNS, TLS and the reverse proxy, with an 8 s timeout, and requires status 200 and `bee_node.healthy` true in the body. Only `probe_success`, `probe_http_status_code` (0 on a timeout or connection failure), `probe_duration_seconds` and `probe_failed_due_to_regex` (1 = answered 200 but Bee not healthy) are kept. The stack also holds Synthetic Monitoring `probe_*` series for other sites, so always select on the job. Alloy runs the config of the branch deployed last (each deploy clones its own branch and recreates Alloy), so until the probe is on `main` a main deploy removes it; the probe alerts are silent with no data, and **Public health probe missing** fires instead.

> Alloy keeps only `bee_eth_backend_*` and `up` from Bee's `/metrics`. Bee exposes 874 series per node; forwarding all of them would multiply Grafana Cloud ingest for no benefit. The API port is not published, so this is reachable only on the compose network.

> New metrics are scraped by Grafana Alloy and remote-written to Grafana Cloud automatically once deployed (no extra wiring). Adding them as **panels** on `monitoring/provisioning/dashboards/gateway-overview.json` is a separate, deliberate step (tracked in its own issue).

### Production Monitoring Stack

```
Gateway containers ──/metrics──> Alloy ──remote write──> Grafana Cloud Prometheus
  (port 8000)          └─logs──>   │   ──push──────────> Grafana Cloud Loki
Bee nodes ────────/metrics────────┘                      (dashboards + alerts)
  (port 1633, bee_eth_backend_* only)                  │
Public /health URLs <──blackbox probe── Alloy ─────────┘
```

**How it works:**
- Grafana Alloy runs as a Docker container alongside the gateways (`docker-compose.host.yml`)
- Alloy scrapes `/metrics` from both gateway containers every 15s via Docker network
- Alloy pushes metrics to Grafana Cloud Prometheus (remote write)
- Alloy also **tails both gateway containers' logs** via the Docker API and pushes them to Grafana Cloud Loki
- Grafana Cloud stores metrics (14-day retention) and hosts dashboards
- Environment labels: `development` (dev branch) and `main` (main branch)
- Alloy also **probes each environment's public `/health`** every 60 s (#586). `up` only says Alloy could scrape `/metrics`, which never calls Bee: on 2026-11-03 staging kept answering `/metrics` for over 30 minutes while every Bee call failed and users got 502s, and nothing alerted

**Docker `unhealthy` is alerted on, not acted on (#586).** The container healthcheck (`curl -f http://localhost:8000/`, 10 s timeout, 3 retries) calls the same handler as `/health`. The probe calls it with a stricter 8 s timeout, so whenever Docker marks the container unhealthy, the probe fails too and "Public health check failing" fires. Nothing restarts the container automatically, on purpose: a restart can lose changes that are not saved yet (see "State file save failures", #506). The probe runs on the gateway host itself, so it does not see the host or its network going away; `monitoring/alerting/README.md` describes Grafana Synthetic Monitoring as the option for a truly external check.

**Logs (#190).** Metrics say how many errors there were; they never say what the error was. Reading one meant SSH to the host and `docker logs` per container — how every investigation in this repository has started, including #299, where the answer was a single line in the reverse proxy's log.

Everything the gateway containers emit is shipped, not only errors. Measured before enabling: production ~3,450 lines/hour (~211 MB/month), staging ~60 lines/hour (~3 MB/month), both well inside the included allowance. Filtering at the source is what you regret during an incident, because the line you need is always the one that looked like noise.

Labels are deliberately few — `container`, `environment`, `host`, `level` — because Loki indexes by label and a high-cardinality one (a request id, an IP, a stamp id) multiplies the index and the bill. Everything else is a query-time filter. `level` is promoted from the start of each Python log line, so `{level="ERROR"}` is an index lookup rather than a substring scan.

Query in Grafana Explore, or use the two dashboard panels:
```
{job="swarm_connect/gateway", environment="main", level=~"WARNING|ERROR|CRITICAL"}
{job="swarm_connect/gateway"} |= "connection"
```

**Credentials** (stored in GitHub secrets, injected at deploy):
- `GRAFANA_CLOUD_PROM_USERNAME` — Prometheus instance ID
- `GRAFANA_CLOUD_API_TOKEN` — access policy token; needs **both** `metrics:write` and `logs:write`
- `GRAFANA_CLOUD_LOKI_URL` — region-specific push URL, e.g. `https://logs-prod-NN.grafana.net/loki/api/v1/push`
- `GRAFANA_CLOUD_LOKI_USERNAME` — Loki tenant id. **NOT the same number as the Prometheus one** — one stack has a different id per signal, and using the Prometheus id yields a 401 that reads like a bad token.

If the two Loki secrets are absent the log pipeline reports unhealthy and drops logs; metrics are unaffected. Verified by running Alloy with the new config and no Loki credentials — the process stays up and all three log components evaluate cleanly.

**The Docker socket.** Tailing container logs requires mounting `/var/run/docker.sock` into Alloy. That socket is a root-equivalent interface to the host, and `:ro` restricts writes to the socket *file*, not to the API reachable through it — anything able to execute inside that container can start a privileged one. Accepted here because Alloy is part of the same trusted stack running a config from this repository. Not a decision to copy elsewhere without re-making it.

**Dashboard:** `datafund.grafana.net/d/gateway-overview`

**Key files:**
- `monitoring/alloy/config.alloy` — Alloy scrape, public /health probe, log shipping and remote write config
- `monitoring/alerting/alert-rules.json` — alert rules (applied with `scripts/apply_grafana.py`)
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
   - CI runs the suite too: `.github/workflows/tests.yml` runs on every pull request, and `deploy.yml` calls it as its gate, so nothing deploys unless it passes. It runs on Python 3.12 (the image's minor version; `tests/test_ci_workflow.py` keeps CI, the Dockerfile and the lock on one version) from `requirements.lock` plus `requirements-dev.lock` (`--no-deps --require-hashes`, then `pip check`): the runtime set the image installs, plus the test-only packages the image does not ship (#430). Live tests are skipped. A second job, `lock-sync`, runs `scripts/check_lock_sync.sh` on pull requests: it fails if either lock no longer matches `requirements.txt` / `requirements-dev.txt`, or lists hashes that differ from PyPI (uv is installed from `.github/uv-pin.txt`, pinned by hash). Still run the suite locally first: CI is a backstop, not a substitute.
   - **Do not open or merge a PR to `dev` or `main` with failing or broken tests.** If a test is failing, either fix it or, if it is a known pre-existing failure, explicitly call it out in the PR description with a tracking issue — never let it pass silently.
   - (Historically, the x402 async migration broke ~51 tests for months precisely because nothing ran them — see the test-repair issue.)

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
- **Always run `python -m pytest tests/` locally and confirm it is green before opening or merging a PR into `dev` or `main`** - CI (`tests.yml`) also runs it on every PR and before every deploy, but a red CI run should never be the first time a failure is seen
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

## Configuration: where it lives, and when a change actually takes effect

Every one of these has bitten us. The pattern is always the same — a change is
made, something reports success, and nothing happens.

| what you change | reaches the running system when | overwritten by a deploy? |
|---|---|---|
| GitHub environment variable (`vars.*`) | next deploy writes it into the env file | n/a — this is the source of truth |
| `/opt/swarm_connect.env`, `/opt/swarm_connect_dev.env` | **container is RECREATED** — not on restart | **yes**, rewritten from GitHub vars |
| `/opt/swarm_connect_host.env` | next deploy appends it to `.env` | no — deploy only ever appends it |
| `monitoring/alloy/config.alloy` | Alloy container is recreated | yes, but see below |
| `deploy/Caddyfile` | **never automatically** — copy to `/etc/caddy/` and `systemctl reload caddy` | no |
| `monitoring/alerting/*.json`, dashboards | **never automatically** — run `scripts/apply_grafana.py` | no |
| `scripts/backup_state.sh`, `scripts/backup_bee_keys.sh`, `scripts/restore_bee_keys.sh` | **never automatically** — install under `/usr/local/sbin` and cron them by hand (`docs/mainnet-runbook.md` §4) | no |

### `docker restart` does not reload `env_file`

Docker reads `env_file` when it **creates** a container, not when it starts one.
So editing `/opt/swarm_connect.env` and running `docker restart` changes nothing,
and the container reports healthy while running the old configuration.

```bash
# WRONG — silently keeps the old values
docker restart swarm_connect-provenance_gateway-1

# RIGHT
cd /opt/swarm_connect && docker compose up -d --force-recreate --no-deps provenance_gateway
```

This cost a real incident: the stamp pool target was lowered, the container was
restarted, and it kept the old target of five while the pool held two — reporting
`low_reserve_warning: true` and preparing to buy three replacements, the exact
opposite of the change being made.

### Changing a setting properly

Both, in this order, or the change is temporary:

1. `gh variable set NAME --repo datafund/swarm_connect --env production --body VALUE`
   — so the next deploy keeps it.
2. Edit `/opt/swarm_connect.env` and **force-recreate** the container — so it
   applies now rather than at the next deploy.

Doing only (2) means the next deploy reverts it. Doing only (1) means nothing
changes until someone deploys.

### The deploy checks the x402 configuration before replacing the gateway

The `deploy` step runs `python -m app.x402.check_config` (#485) after the
checkout and `.env` are assembled and before `docker compose stop`. It runs
through `docker compose run --rm --no-deps -T "$BRANCH_SERVICE"`, so it uses
the new image and the same `env_file` and `environment` as the new container.
It calls the startup check itself (`validate_x402_config`). A configuration
the gateway would refuse exits 3 and fails the job with `x402 configuration
refused`. Any other failure (compose, the Docker daemon, a settings error)
fails it with `x402 configuration check could not run`. Either way the running
gateway stays up, and the rest of the step (host services, Alloy) is skipped.

The facilitator URL setting (`X402_FACILITATOR_URL`) must be empty or exactly
`https://api.cdp.coinbase.com/platform/v2/x402` when CDP facilitator
credentials are set: `cdp-sdk` signs each token for
that route's `/verify` and `/settle`, and the x402 client appends those to
the URL, so the bare host or any other path is refused (#527). The client
always uses the URL `cdp-sdk` returns, never the configured one.

The env file has already been rewritten by then. After a refused deploy, fix
the GitHub variables or secrets and re-run the deploy. Do **not**
force-recreate by hand in between: the new container would read the refused
values and stay down.

### A bind-mounted FILE pins its inode

`config.alloy` is mounted as a single file. A file bind mount pins the inode, and
`git pull` **replaces** the file rather than editing it, so the container goes on
reading the old one indefinitely. Compose does not help: the service definition is
unchanged, so `up -d` correctly does nothing.

Changes to it were silently dead from 2026-08-25 until #301, which force-recreates
Alloy on every deploy. If you add another single-file mount, it will need the same.

### Not everything in the repo is deployed

`deploy/Caddyfile` and `monitoring/alerting/alert-rules.json` are version
controlled for review and history. Nothing applies them. They can drift from what
is running, and have. Diff before assuming, and never overwrite the live
`/etc/caddy/Caddyfile` from the repo without diffing first — doing that once
reintroduced `tls internal` and broke TLS for two minutes.

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
   To build exactly as PR CI does (`.github/workflows/image.yml`): `docker build --pull --no-cache --platform linux/amd64 -t swarm_connect:ci .`. The deploy hosts reuse their layer cache, so a build that passes there can still fail from clean (#483); the uncached PR build is the one that counts.

2. **Check for Python version compatibility issues**:
   - Docker uses Python 3.12 (`Dockerfile`: `python:3.12-slim`, pinned by digest, Debian trixie); CI runs 3.12 too (#617)
   - Avoid Python 3.13+ features (e.g. `warnings.deprecated`, `copy.replace`, PEP 696 type-parameter defaults); a local 3.13 venv will not catch them
   - From 3.11, `asyncio.TimeoutError` is the built-in `TimeoutError` (an `OSError`), so `except asyncio.TimeoutError` also catches OS-level timeouts; httpx2 maps socket timeouts to `httpx2.TimeoutException`, which is not one
   - From 3.12, `asyncio.wait_for` is built on `asyncio.timeout`, so a result that completes in the same loop step as the deadline can still end in `TimeoutError` (3.10 returned it). The shielded call sites treat that conservatively: `stamps.py` answers 202 pending and the background half registers the batch; `settlement.py` answers 503 and gives the write-ahead up, which `_abandon_begin` clears; `idempotency.py` marks a stall that the next write clears. `BeeCallGate.acquire` keeps a slot granted in that step (tested in `tests/test_bee_call_gate.py::TestGrantRace`)
   - Bumping the base image: `docker buildx imagetools inspect python:3.12-slim`, copy the top-level index `Digest:` into the Dockerfile `FROM` (not a per-platform manifest, so arm64 dev builds keep working), update the version comment, and let the PR's `image.yml` prove it. A minor-version move also changes both `python-version` entries in `tests.yml` and the `--python-version` of both locks (regenerate with the two commands in `requirements.txt`); `tests/test_ci_workflow.py` fails until they agree
   - The codebase still writes `Optional[int]` rather than `int | None`; keep to that for consistency

3. **Common issues**:
   - Import errors → Check the dependency is in requirements.txt (or, for a test-only package, requirements-dev.txt) and that both locks were regenerated with the commands in `requirements.txt` (runtime lock first). `scripts/check_lock_sync.sh` (uv 0.11.x) shows whether they match; the PR's `lock-sync` job runs it. The image installs only `requirements.lock`, so app code must never import a package that is only in `requirements-dev.txt`
   - `build docker image` fails on a deploy host but the PR's `docker-build` check passed → the host's base image or cache differs; the PR build is uncached and pulls the base image fresh

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

## Writing style

Write all communication in the style of the [Microsoft Writing Style Guide](https://learn.microsoft.com/en-us/style-guide/welcome/). This covers issues, pull requests, review and issue comments, commit messages, and documentation.

- Use plain, direct language, with one idea per sentence. State what happened and why. Don't use figurative or clever phrasing.
- Use active voice and present tense. In documentation, address the reader as "you".
- Use sentence-case headings, and keep paragraphs short.
- Use the exact technical terms: endpoint, flag, field and function names as they appear in the code. Define any term the reader might not know, and use one term per concept.
- Spell out an abbreviation the first time you use it. Don't use slang or filler.
- Use bulleted lists for parallel items, numbered lists for steps, and tables for comparisons.
- Say whether each claim is verified, and how, or whether it is a hypothesis.
