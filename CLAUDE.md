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

### Key Components

**Core Configuration (`app/core/config.py`)**:
- Uses `pydantic-settings` for environment variable management
- Validates SWARM_BEE_API_URL as proper URL format
- Cached settings object with `@lru_cache()` for performance

**Swarm Integration (`app/services/swarm_api.py`)**:
- Handles HTTP requests to Swarm Bee API (`/batches` endpoint)
- Includes error handling for network issues and malformed responses
- Supports both direct list responses and `{"batches": [...]}` wrapper formats

**Stamps API (`app/api/endpoints/stamps.py`)**:
- Provides `/api/v1/stamps/{stamp_id}` endpoint
- Fetches all stamps from Swarm and filters by ID
- Calculates expiration time: `current_time + batchTTL`
- Comprehensive error handling with appropriate HTTP status codes

**Data Models (`app/api/models/stamp.py`)**:
- `StampDetails` model with optional fields to handle missing data from upstream API
- Field aliases for API compatibility (`amount` aliased as `value`, etc.)
- Calculated `expectedExpiration` field in `YYYY-MM-DD-HH-MM` UTC format
- Calculated `utilizationPercent` field showing stamp usage as percentage (0-100%)

### Environment Configuration

Required environment variables:
- `SWARM_BEE_API_URL`: URL to Swarm Bee node API (e.g., `https://api.gateway.ethswarm.org`)

Optional environment variables:
- `HOST`: Server host (default: `127.0.0.1`)
- `PORT`: Server port (default: `8000`)
- `RELOAD`: Enable auto-reload (default: `true`)
- `SSL_KEYFILE`/`SSL_CERTFILE`: For HTTPS development

### API Endpoints

#### Core Endpoints
- `GET /`: Health check endpoint

#### Stamp Management
- `POST /api/v1/stamps/`: Purchase new postage stamps
- `GET /api/v1/stamps/`: List all available stamps with expiration calculations
- `GET /api/v1/stamps/{stamp_id}`: Retrieve specific stamp batch details
- `GET /api/v1/stamps/{stamp_id}/check`: Check stamp health for uploads (errors, warnings, can_upload status)
- `PATCH /api/v1/stamps/{stamp_id}/extend`: Extend existing stamps with additional funds

#### Data Operations
- `POST /api/v1/data/?stamp_id={id}&content_type={type}&redundancy={level}`: Upload raw data to Swarm (redundancy 0-4, default 2)
- `POST /api/v1/data/manifest?stamp_id={id}&redundancy={level}`: Upload TAR archive as collection/manifest (15x faster for batch uploads)
- `GET /api/v1/data/{reference}`: Download raw data from Swarm (returns bytes directly)
- `GET /api/v1/data/{reference}/json`: Download data with JSON metadata (base64-encoded)

### Dependencies and Tech Stack

- **FastAPI**: Web framework with automatic OpenAPI documentation
- **Uvicorn**: ASGI server with performance extras
- **Requests**: HTTP client for Swarm API integration
- **Pydantic**: Data validation and settings management
- **python-dotenv**: Environment file loading

### Development Notes

- Tests are implemented using pytest with mocking (see `tests/` directory)
- CORS middleware is commented out but ready to enable
- Authentication/authorization placeholder code exists but not implemented
- SSL/HTTPS support built into development server
- Logging configured at INFO level with structured error handling

## x402 Payment Integration

### Overview

The gateway supports x402 payment protocol for pay-per-request access without user accounts. When enabled, clients pay in USDC on Base chain to access stamp purchase and data upload endpoints.

**Current Status**: Development on `main-x402-upgrade` branch (testnet only)

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
└── audit.py         # Transaction audit logging
```

### Key Configuration

```bash
X402_ENABLED=false           # Master switch (default: off)
X402_FACILITATOR_URL=...     # Payment facilitator
X402_PAY_TO_ADDRESS=0x...    # USDC receiving wallet (Base)
X402_NETWORK=base-sepolia    # Network identifier
```

### Protected Endpoints (when X402_ENABLED=true)

- `POST /api/v1/stamps/` - Requires payment
- `POST /api/v1/data/` - Requires payment
- `POST /api/v1/data/manifest` - Requires payment
- `GET /api/v1/data/{ref}` - FREE (no payment required)

### Development Notes

- Branch: `main-x402-upgrade` - DO NOT MERGE TO MAIN without approval
- Python SDK is v1 only (v2 under development)
- See `docs/x402-operator-guide.md` for operator setup instructions
- All x402 transactions logged to `logs/x402_audit.jsonl`

### Testing x402

```bash
# With x402 disabled (default behavior)
X402_ENABLED=false python run.py

# With x402 enabled (requires facilitator)
X402_ENABLED=true X402_PAY_TO_ADDRESS=0x... python run.py

# Run x402 tests
python -m pytest tests/test_x402_*.py -v
```

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

**IMPORTANT**: Never push directly to main. Always:
1. Create a feature branch (e.g., `fix/docs-examples`, `feature/new-endpoint`)
2. Make commits on the branch
3. Create a PR and merge via GitHub

**Repository**: This repository pushes to `git@github.com:datafund/swarm_connect.git` (origin).

**CRITICAL - Always use datafund repo**:
- When creating GitHub issues: `gh issue create --repo datafund/swarm_connect`
- When creating PRs: `gh pr create --repo datafund/swarm_connect`
- NEVER use `crtahlin/swarm_connect` - that is the upstream fork, not the main repo
- Use `git remote -v` to verify remotes if unsure

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