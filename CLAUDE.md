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
Currently no test framework is configured. Tests would go in the `tests/` directory.

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
- `PATCH /api/v1/stamps/{stamp_id}/extend`: Extend existing stamps with additional funds

#### Data Operations (NEW)
- `POST /api/v1/data/?stamp_id={id}&content_type={type}`: Upload raw data to Swarm
- `GET /api/v1/data/{reference}`: Download raw data from Swarm (returns bytes directly)
- `GET /api/v1/data/{reference}/json`: Download data with JSON metadata (base64-encoded)

### Dependencies and Tech Stack

- **FastAPI**: Web framework with automatic OpenAPI documentation
- **Uvicorn**: ASGI server with performance extras
- **Requests**: HTTP client for Swarm API integration
- **Pydantic**: Data validation and settings management
- **python-dotenv**: Environment file loading

### Development Notes

- No tests are currently implemented
- CORS middleware is commented out but ready to enable
- Authentication/authorization placeholder code exists but not implemented
- SSL/HTTPS support built into development server
- Logging configured at INFO level with structured error handling

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

## Git Repository Configuration

**IMPORTANT**: This repository pushes to `git@github.com:datafund/swarm_connect.git` (origin). When creating GitHub issues or pull requests, always use the `datafund/swarm_connect` repository, NOT the `crtahlin/swarm_connect` upstream repository.

Use `git remote -v` to verify the correct repository before creating issues.

## Commit Message Guidelines

- Do NOT include Claude/AI mentions, co-author tags, or "Generated with Claude" footers in commit messages
- Keep commit messages clean and professional - just describe the changes