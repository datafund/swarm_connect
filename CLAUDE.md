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
- **Middleware**: Rate limiting in `app/middleware/rate_limit.py` (sliding window per-IP)
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
- `RATE_LIMIT_ENABLED`: Enable per-IP rate limiting (default: `true`)
- `RATE_LIMIT_PER_MINUTE`: Requests per minute per IP (default: `60`)
- `RATE_LIMIT_BURST`: Extra burst capacity above per-minute limit (default: `10`)

Notary signing (optional):
- `NOTARY_ENABLED`: Enable notary signing feature (default: `false`)
- `NOTARY_PRIVATE_KEY`: Hex-encoded Ethereum private key for signing (64 characters, no 0x prefix). Generate with `python scripts/generate_notary_key.py`

CORS (browser access):
- `CORS_ALLOWED_ORIGINS`: Allowed origins, `*` for all or comma-separated list (default: `*`)
- `CORS_ALLOW_CREDENTIALS`: Allow credentials in CORS requests (default: `false`)

### API Endpoints

#### Core Endpoints
- `GET /`: Health check endpoint

#### Stamp Management
- `POST /api/v1/stamps/`: Purchase new postage stamps (records purchase time for propagation tracking)
- `GET /api/v1/stamps/`: List stamps (default: local only). Supports `?global=true` for all stamps, `?wallet=0x...` for wallet-filtered view (x402)
- `GET /api/v1/stamps/{stamp_id}`: Retrieve specific stamp batch details including propagation timing
- `GET /api/v1/stamps/{stamp_id}/check`: Check stamp health for uploads (errors, warnings, can_upload status, propagation status)
- `PATCH /api/v1/stamps/{stamp_id}/extend`: Extend existing stamps with additional funds

**Stamp list query parameters**:
- `global` (bool): If true, return all stamps including non-local (old behavior)
- `wallet` (string): Filter to stamps accessible by this wallet address (requires x402 enabled)

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

#### Stamp Pool (Low-Latency Provisioning)
- `GET /api/v1/pool/status`: Get pool status and reserve levels
- `POST /api/v1/pool/acquire`: Acquire stamp from pool instantly (<5 seconds vs >1 minute)
- `GET /api/v1/pool/available`: List available stamps in pool
- `POST /api/v1/pool/check`: Trigger manual pool maintenance

#### Notary Signing (Provenance)
- `GET /api/v1/notary/info`: Check notary availability and get public address for verification
- `GET /api/v1/notary/status`: Simplified notary status for health checks
- `POST /api/v1/data/?sign=notary`: Upload with notary signature (adds `sign` parameter to data upload)

### Dependencies and Tech Stack

- **FastAPI**: Web framework with automatic OpenAPI documentation
- **Uvicorn**: ASGI server with performance extras
- **Requests**: HTTP client for Swarm API integration
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

3. **Create PR to merge into `dev`**:
   - All code must go through PR review
   - CI/CD automatically deploys to staging (`provenance-gateway.dev.datafund.io`)

4. **Test on staging environment** before promoting to production

5. **Create PR to merge `dev` into `main`**:
   - Only after staging validation
   - CI/CD automatically deploys to production (`provenance-gateway.datafund.io`)

### Branch Protection Rules

- **Never push directly to `main`** - always use PRs
- **Never push directly to `dev`** - always use PRs from feature branches
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