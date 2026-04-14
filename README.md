# Provenance Gateway
A FastAPI service for Swarm network access with x402 payments, notary signing, and stamp pool management.

> ⚠️ **ALPHA SOFTWARE - PROOF OF CONCEPT**
> This software is in **Alpha stage** and should be considered a **Proof of Concept**. Use for testing and experimentation only. Not recommended for production use.

> ⚠️ **DATA PERSISTENCE WARNING**
> Storage on Swarm is **rented storage** with limited time periods. The default configuration uses very short rental periods (approximately **1 day**). **Do not expect uploaded data to persist longer than the rental period.** Data will become unavailable when the postage stamp expires.
>

## Project structure

```
swarm_connect/
├── app/                    # Main application package
│   ├── main.py             # FastAPI app instantiation and router inclusion
│   ├── api/
│   │   ├── endpoints/      # API route definitions
│   │   │   ├── stamps.py   # Stamp management (purchase, extend, list, health check)
│   │   │   ├── data.py     # Data upload/download (single + manifest)
│   │   │   ├── wallet.py   # Wallet and chequebook info
│   │   │   ├── pool.py     # Stamp pool (low-latency provisioning)
│   │   │   └── notary.py   # Notary signing status
│   │   └── models/         # Pydantic request/response models
│   ├── core/
│   │   ├── config.py       # Configuration (environment variables)
│   │   └── version.py      # Version from build
│   ├── middleware/
│   │   └── rate_limit.py   # Per-IP sliding window rate limiter
│   ├── services/
│   │   ├── swarm_api.py    # Swarm Bee API client
│   │   ├── stamp_pool.py   # Stamp pool background manager
│   │   ├── provenance.py   # Document processing for signing
│   │   └── signing.py      # Ethereum signing infrastructure
│   └── x402/               # x402 payment protocol (optional)
│       ├── dependency.py   # FastAPI dependency for payment checks
│       ├── middleware.py    # Post-response settlement + headers
│       ├── pricing.py      # BZZ → USD price calculation
│       ├── access.py       # IP whitelist/blacklist
│       ├── audit.py        # Transaction audit logging
│       └── ratelimit.py    # Per-IP rate limiting for x402
│
├── scripts/                # Operator utility scripts
│   └── generate_notary_key.py  # Generate Ethereum keypair for notary signing
├── docs/                   # Feature documentation
├── tests/                  # Unit and integration tests (700+)
│
├── .env.example            # Example environment file
├── docker-compose.yml      # Docker deployment (production + staging)
├── Dockerfile              # Container image (Python 3.10)
├── requirements.txt        # Python dependencies
└── run.py                  # Development server
``` 

## Running

### Setup and Installation

```bash
# Create virtual environment
python3 -m venv venv

# Activate virtual environment (Linux/Mac)
source venv/bin/activate
# On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Set up environment file
cp .env.example .env
# Edit .env and ensure SWARM_BEE_API_URL points to your Bee node's API endpoint
# (e.g., http://localhost:1633 or the public gateway https://api.gateway.ethswarm.org)
```

### Starting the Server

```bash
# Start the development server (with auto-reload)
python run.py

# Optional: Use different port if 8000 is taken
PORT=8001 python run.py

# For HTTPS development (requires SSL certificates)
SSL_KEYFILE=./localhost+2-key.pem SSL_CERTFILE=./localhost+2.pem python run.py
```

The server will be available at:
- HTTP: http://127.0.0.1:8000
- API Documentation: http://127.0.0.1:8000/docs
- Alternative docs: http://127.0.0.1:8000/redoc

## Architecture

Swarm Connect is a FastAPI-based API gateway that provides comprehensive access to Ethereum Swarm (distributed storage network) functionality. It offers complete postage stamp management and data operations through a clean, RESTful interface, eliminating the need for clients to interact directly with complex Swarm Bee node APIs.

### System Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              CLIENT APPLICATIONS                            │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐ │
│  │   Web Apps  │  │  Mobile App │  │  CLI Tools  │  │  Third-party Apps   │ │
│  └─────────────┘  └─────────────┘  └─────────────┘  └─────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                           HTTP/HTTPS Requests
                                    │
┌─────────────────────────────────────────────────────────────────────────────┐
│                          SWARM CONNECT API GATEWAY                         │
│                              (FastAPI)                                     │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                        API LAYER                                   │   │
│  │  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────────┐ │   │
│  │  │   Health Check  │  │   OpenAPI Docs  │  │  Stamps & Data APIs │ │   │
│  │  │   GET /         │  │   /docs /redoc  │  │   Complete CRUD     │ │   │
│  │  └─────────────────┘  └─────────────────┘  └─────────────────────┘ │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                    │                                       │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                      VALIDATION LAYER                              │   │
│  │  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────────┐ │   │
│  │  │  Request/Response│  │   StampDetails  │  │  Error Handling &   │ │   │
│  │  │    Validation   │  │   Pydantic Model│  │   HTTP Status Codes │ │   │
│  │  └─────────────────┘  └─────────────────┘  └─────────────────────┘ │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                    │                                       │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                      BUSINESS LOGIC LAYER                          │   │
│  │  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────────┐ │   │
│  │  │  Stamp Filtering│  │  TTL Calculation│  │   Expiration Time   │ │   │
│  │  │   by Batch ID   │  │   & Processing  │  │   Formatting        │ │   │
│  │  └─────────────────┘  └─────────────────┘  └─────────────────────┘ │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                    │                                       │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                        SERVICE LAYER                               │   │
│  │  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────────┐ │   │
│  │  │  Swarm API      │  │  HTTP Client    │  │   Error Recovery &  │ │   │
│  │  │  Integration    │  │  (httpx Async)  │  │   Retry Logic       │ │   │
│  │  └─────────────────┘  └─────────────────┘  └─────────────────────┘ │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                    │                                       │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                     CONFIGURATION LAYER                            │   │
│  │  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────────┐ │   │
│  │  │  Environment    │  │   Settings      │  │   URL Validation &  │ │   │
│  │  │  Variables      │  │   Management    │  │   Caching           │ │   │
│  │  └─────────────────┘  └─────────────────┘  └─────────────────────┘ │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                           HTTP Requests (10s timeout)
                                    │
┌─────────────────────────────────────────────────────────────────────────────┐
│                              SWARM BEE NODE                                │
│                           (localhost:1633)                                 │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                        BEE API ENDPOINTS                           │   │
│  │  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────────┐ │   │
│  │  │   GET /batches  │  │   Stamp Data    │  │   Blockchain        │ │   │
│  │  │   (All Stamps)  │  │   Repository    │  │   Integration       │ │   │
│  │  └─────────────────┘  └─────────────────┘  └─────────────────────┘ │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Core Features

#### 🚀 Stamp Management API
- **Duration-Based Purchasing**: Specify stamp duration in hours instead of raw PLUR amounts
- **Dynamic Price Calculation**: Automatically calculates costs based on current network price
- **Wallet Balance Verification**: Checks sufficient funds before purchase/extension with clear error messages
- **Purchase Stamps**: Create new postage stamps with duration (hours) or legacy amount
- **Extend Stamps**: Add time to existing stamps using duration or legacy amount
- **List All Stamps**: Retrieve comprehensive list of all available stamps with enhanced data
- **Get Stamp Details**: Fetch specific stamp information by batch ID
- **Expiration Calculation**: Automatically calculates stamp expiration time (current time + TTL)
- **Utilization Percentage**: Calculates human-readable stamp usage percentage (0-100%)
- **Utilization Status Warnings**: Status levels ("ok", "warning", "critical", "full") with actionable messages
- **Usable Flag**: Stamps at 100% utilization automatically marked as unusable
- **Data Merging**: Merges global network data with local node information for complete stamp details
- **Local Ownership Detection**: Identifies stamps owned/managed by the connected node
- **Enhanced Field Mapping**: Handles different field names between global and local APIs

#### 📁 Data Operations API
- **Unified Data Upload**: Single endpoint handles both JSON and binary data automatically
- **Collection/Manifest Upload**: Upload multiple files as TAR archive for 15x performance improvement
- **Deferred Upload Mode**: Optional deferred mode for faster upload response (data syncs to network asynchronously)
- **Configurable Redundancy**: Optional erasure coding level (0-4) for data durability vs storage cost tradeoff
- **Pre-Upload Stamp Validation**: Optional validation to check stamp usability before upload
- **Performance Timing**: W3C Server-Timing headers and optional JSON timing breakdown for latency profiling
- **Enhanced Error Messages**: Detailed feedback when uploads fail due to stamp capacity
- **SWIP-Compliant Examples**: Pre-filled with SWIP standard provenance data structure
- **Content-Type Detection**: Automatic handling based on Content-Type header
- **Raw Data Download**: Download data as binary stream or base64-encoded JSON
- **Reference-Based Access**: Access data using Swarm reference hashes
- **Provenance Support**: Built-in examples for data lineage and provenance tracking
- **Notary Signing**: Optional cryptographic signatures proving data existed at upload time (see [Notary Guide](docs/notary-guide.md))

#### 🔧 Technical Features
- **FastAPI Framework**: Modern, fast web framework with automatic OpenAPI documentation
- **Auto-Documentation**: Interactive API docs at `/docs` and `/redoc`
- **Type Validation**: Pydantic models ensure data integrity and type safety
- **Error Handling**: Comprehensive error responses with appropriate HTTP status codes
- **Configuration Management**: Environment-based settings with validation
- **Development Server**: Hot-reload development server with SSL support
- **Binary Data Support**: Direct binary upload/download with optional JSON wrapping
- **Modular Design**: Separate endpoints for stamps and data operations

#### 🛡️ Security & Rate Limiting
- **Upload Size Limits**: Configurable maximum upload size (default: 10 MB) with clear 413 errors
- **Global Rate Limiting**: Per-IP sliding window rate limiter with burst capacity (default: 60 req/min + 10 burst)
- **Input Validation**: Strict regex validation on stamp IDs (64-char hex) and references (64-128 char hex)
- **Error Sanitization**: Internal details (IPs, ports, file paths) are never exposed in error responses
- **Server Header Suppression**: `Server` header removed to prevent version fingerprinting
- **Rate Limit Headers**: `X-RateLimit-Limit` and `X-RateLimit-Remaining` on every response

#### 🛡️ Reliability Features
- **Request Timeouts**: 10-second timeout for external API calls
- **Error Recovery**: Multiple layers of exception handling
- **Flexible Response Parsing**: Handles different Swarm API response formats
- **Logging**: Structured logging for debugging and monitoring
- **Health Checks**: Basic health check endpoint for monitoring

### Component Architecture

#### Configuration Layer (`app/core/config.py`)
- Loads environment variables on startup
- Validates Swarm Bee API URL format
- Provides cached settings to all components

#### API Layer (`app/main.py` + `app/api/endpoints/`)
- Receives HTTP requests and routes them
- Applies path parameters and validation
- Returns structured JSON responses

#### Service Layer (`app/services/swarm_api.py`)
- Makes HTTP calls to Swarm Bee node (both `/batches` and `/stamps` endpoints)
- Handles network errors and timeouts
- Parses and normalizes API responses
- **Data Merging Logic**: Combines global stamp data with local node information
- **Field Mapping**: Handles different field names between endpoints (`immutable` vs `immutableFlag`)
- **Usability Calculation**: Determines stamp usability based on TTL, depth, and immutability
- **Utilization Calculation**: Computes stamp usage as percentage: `(utilization / 2^(depth-bucketDepth)) * 100`
- **Local Detection**: Identifies stamps owned by the connected node

#### Model Layer (`app/api/models/stamp.py`)
- Validates response data structure with enhanced fields
- Handles optional fields and type conversion
- **Local Ownership Field**: Boolean indicator for node-owned stamps
- **Enhanced Nullable Fields**: Proper handling of potentially missing data from different endpoints
- Formats output for API consumers

### Data Flow

```
1. Client → FastAPI Router → Endpoint Handler
2. Endpoint → Service Layer → External Swarm API
3. Service → Business Logic → Data Processing
4. Response ← Pydantic Model ← Formatted Data
```

### Available API Endpoints

#### Core Endpoints
- `GET /`: Health check endpoint

#### Stamp Management
- `POST /api/v1/stamps/`: Purchase new postage stamps with time-based or advanced parameters
- `GET /api/v1/stamps/`: List stamps (default: local only; `?global=true` for all; `?wallet=0x...` for wallet-filtered)
- `GET /api/v1/stamps/{stamp_id}`: Retrieve specific stamp batch details
- `GET /api/v1/stamps/{stamp_id}/check`: Check stamp health for uploads (errors and warnings)
- `PATCH /api/v1/stamps/{stamp_id}/extend`: Extend existing stamps with additional funds

#### Data Operations
- `POST /api/v1/data/?stamp_id={id}&content_type={type}`: Upload raw data to Swarm
- `POST /api/v1/data/manifest?stamp_id={id}`: Upload TAR archive as collection/manifest (15x faster for batch uploads)
- `GET /api/v1/data/{reference}`: Download raw data from Swarm (returns bytes directly)
- `GET /api/v1/data/{reference}/json`: Download data with JSON metadata (base64-encoded)

#### Wallet Information
- `GET /api/v1/wallet`: Get the wallet address and BZZ balance of the Bee node
- `GET /api/v1/chequebook`: Get the chequebook address and balance information of the Bee node

#### Notary Signing
- `GET /api/v1/notary/info`: Check notary availability and get public address for verification
- `GET /api/v1/notary/status`: Simplified notary status for health checks
- `POST /api/v1/data/?sign=notary`: Upload with notary signature (see [Notary Guide](docs/notary-guide.md))

### Key Value Propositions

1. **Complete Gateway Solution**: Full stamp lifecycle and data operations in one service
2. **Simplified Interface**: Clean REST API vs complex Swarm protocols
3. **Enhanced Data**: Adds calculated expiration times to raw stamp data
4. **Reliability**: Robust error handling and timeout management
5. **Developer Experience**: Auto-generated docs and type safety
6. **Flexibility**: Configurable for different Swarm node endpoints
7. **Binary Support**: Native handling of raw data with multiple access patterns
8. **x402 Payment Support**: Optional pay-per-request monetization via USDC

## x402 Payment Gateway (Optional)

The gateway supports optional x402 payment integration, enabling pay-per-request access without user accounts.

### Overview

When `X402_ENABLED=true`, protected endpoints (`POST /stamps/`, `POST /data/`) require USDC payment on Base chain. The gateway uses received payments to fund Swarm operations.

```
┌──────────────┐     USDC      ┌──────────────┐     xBZZ      ┌──────────────┐
│    Client    │ ────────────> │   Gateway    │ ────────────> │  Swarm Bee   │
│  (Base chain)│               │  (2 wallets) │               │ (Gnosis chain)│
└──────────────┘               └──────────────┘               └──────────────┘
```

### Quick Start

1. **Enable x402** in `.env`:
   ```bash
   X402_ENABLED=true
   X402_PAY_TO_ADDRESS=0xYourBaseWallet
   ```

2. **Without payment**, protected endpoints return HTTP 402:
   ```bash
   curl -X POST http://localhost:8000/api/v1/stamps/
   # Returns 402 with payment requirements
   ```

3. **With payment** (using x402 client):
   ```bash
   curl -X POST http://localhost:8000/api/v1/stamps/ \
        -H "X-PAYMENT: <base64-encoded-payment>"
   # Returns 200 with stamp details
   ```

### Features

- **Pay-per-request**: No accounts, no subscriptions
- **Access control**: IP whitelist/blacklist with CIDR support
- **Rate limiting**: Per-IP request throttling (default: 10/min)
- **Audit logging**: JSON lines format for all transactions
- **Pre-flight checks**: Validates gateway wallet balances before accepting payments

### Protected Endpoints

| Endpoint | Payment Required |
|----------|-----------------|
| `POST /api/v1/stamps/` | Yes |
| `POST /api/v1/data/` | Yes |
| `POST /api/v1/data/manifest` | Yes |
| `GET /api/v1/data/{ref}` | No (free) |
| `GET /api/v1/stamps/` | No (free) |

### Configuration

```bash
# Core
X402_ENABLED=true
X402_PAY_TO_ADDRESS=0x...       # Your USDC receiving wallet
X402_NETWORK=base-sepolia       # or "base" for mainnet

# Pricing
X402_BZZ_USD_RATE=0.50          # BZZ to USD rate
X402_MARKUP_PERCENT=50          # Profit margin
X402_MIN_PRICE_USD=0.01         # Minimum charge

# Access Control
X402_WHITELIST_IPS=127.0.0.1    # Free access
X402_BLACKLIST_IPS=             # Blocked IPs
X402_RATE_LIMIT_PER_IP=10       # Requests/min per IP

# Audit
X402_AUDIT_LOG_PATH=logs/x402_audit.jsonl
```

### Documentation

See [x402 Operator Guide](docs/x402-operator-guide.md) for complete setup instructions.

## Monitoring

The gateway exposes a Prometheus metrics endpoint at `GET /metrics` for operational monitoring.

**What's tracked:**
- HTTP request rate, latency, and error rate (auto-instrumented)
- Upload/download counts and bytes transferred
- Stamp purchases and pool acquisitions
- Wallet balances (BZZ, xDAI, chequebook, Base ETH)
- Stamp pool availability and TTL
- x402 payment mode breakdown
- Rate limit rejections

**Configuration:**
```bash
METRICS_ENABLED=true                # Expose /metrics (default: true)
METRICS_BALANCE_POLL_SECONDS=60     # Balance polling interval (default: 60s)
GATEWAY_ENVIRONMENT=production      # Environment label for multi-instance setups
```

**Stack:** Prometheus + Grafana Cloud + Telegram alerting. See the [monitoring epic](https://github.com/datafund/swarm_connect/issues/179) for setup details.

## API Endpoints

### Stamp Management Endpoints

#### `POST /api/v1/stamps/`
Purchase a new postage stamp with duration-based or legacy amount pricing.

**Simple usage with size presets (recommended):**
```json
{"duration_hours": 48, "size": "small", "label": "my-stamp"}
```

**Size presets:**
| Size | Use case |
|------|----------|
| `small` | One small document (default) |
| `medium` | Several medium documents |
| `large` | Several large documents |

**Parameters:**
- `duration_hours`: Desired stamp duration in hours (default: 25)
- `size`: Storage size preset - "small", "medium", or "large" (default: "small")
- `depth`: Advanced - explicit depth value 16-32 (overridden by size if both provided)
- `label`: Optional user-defined label

**Using defaults (25 hours, size small):**
```json
{}
```

**Legacy amount mode:**
```json
{"amount": 8000000000, "depth": 17}
```

**Response**: `{"batchID": "...", "message": "Postage stamp purchased successfully"}`

**Error (insufficient funds):**
```json
{
  "detail": "Insufficient funds to purchase stamp. Required: 1.50 BZZ, Available: 0.25 BZZ, Shortfall: 1.25 BZZ"
}
```

#### `GET /api/v1/stamps/`
List postage stamps. Default returns only local stamps (usable for uploads).

**Query Parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `global` | bool | `false` | If true, return all stamps including non-local |
| `wallet` | string | - | Filter to stamps accessible by this wallet (requires x402 enabled) |
| `exclusive` | bool | `false` | With `wallet`: only stamps purchased by this wallet (excludes shared/untracked) |

- **Response**: `{"stamps": [...], "total_count": N}`
- Each stamp includes `accessMode`: `"owned"` (exclusive), `"shared"` (free tier), or `null` (untracked)

#### `GET /api/v1/stamps/{stamp_id}`
Get detailed information about a specific stamp.
- **Response**: Detailed stamp information with calculated expiration time

**Stamp Utilization Status Fields:**
| Field | Description |
|-------|-------------|
| `utilizationPercent` | Usage percentage (0-100%) |
| `utilizationStatus` | Status level: "ok", "warning", "critical", "full" |
| `utilizationWarning` | Actionable message when status is elevated |
| `usable` | Boolean - false when stamp is expired, invalid, or 100% full |

**Status Thresholds:**
| Status | Range | Meaning |
|--------|-------|---------|
| `ok` | 0-80% | Plenty of capacity |
| `warning` | 80-95% | Approaching full capacity |
| `critical` | 95-99.99% | Nearly full, action recommended |
| `full` | 100% | Cannot accept more data |

**Example response with warning:**
```json
{
  "batchID": "abc123...",
  "utilizationPercent": 87.5,
  "utilizationStatus": "warning",
  "utilizationWarning": "Stamp is approaching full capacity (87.5% utilized). Monitor usage and consider purchasing additional stamps.",
  "usable": true
}
```

#### `PATCH /api/v1/stamps/{stamp_id}/extend`
Extend an existing stamp by adding more time.

**Duration-based (recommended):**
```json
{"duration_hours": 48}
```

**Using default (25 hours):**
```json
{}
```

**Legacy amount mode:**
```json
{"amount": 8000000000}
```

**Response**: `{"batchID": "...", "message": "Postage stamp extended successfully"}`

### Data Operation Endpoints

#### `POST /api/v1/data/?stamp_id={id}&content_type={type}&validate_stamp={bool}&deferred={bool}&include_timing={bool}&redundancy={level}`
Upload data to Swarm (JSON or binary).
- **Request Body**: JSON data (default) or raw binary data
- **Content-Type**: `application/json` (default) or `application/octet-stream` for binary
- **Size Limit**: Maximum upload size is configurable (default: 10 MB). Returns 413 if exceeded.
- **validate_stamp**: Optional (default: false) - Pre-validate stamp before upload
- **deferred**: Optional (default: false) - Use deferred upload mode
- **include_timing**: Optional (default: false) - Include timing breakdown in response
- **redundancy**: Optional (default: 2) - Erasure coding level (0-4)
- **Response**: `{"reference": "...", "message": "Data uploaded successfully", "timing": null}`
- **Features**: Pre-filled with SWIP-compliant provenance data example structure

**Deferred vs Direct Upload:**
| Mode | Parameter | Description |
|------|-----------|-------------|
| Direct | `deferred=false` (default) | Chunks uploaded directly to network. Ensures immediate availability. Safer for gateway use cases. |
| Deferred | `deferred=true` | Data goes to local node first, syncs to network asynchronously. Faster upload response but data may not be immediately retrievable. |

**Redundancy Levels (Erasure Coding):**
| Level | Name | Chunk Loss Tolerance | Use Case |
|-------|------|---------------------|----------|
| 0 | none | 0% | Maximum storage efficiency, no redundancy |
| 1 | medium | 1% | Basic redundancy for stable networks |
| 2 | strong | 5% | **Default** - Good balance of durability and cost |
| 3 | insane | 10% | High durability for important data |
| 4 | paranoid | 50% | Maximum durability for critical data |

Higher redundancy levels increase data durability at the cost of more storage space (and stamp usage).

**Examples:**
```bash
# Standard upload (direct mode, default redundancy=2)
curl -X POST "http://localhost:8000/api/v1/data/?stamp_id=YOUR_STAMP_ID" \
     -F "file=@data.json"

# Upload with deferred mode (faster response)
curl -X POST "http://localhost:8000/api/v1/data/?stamp_id=YOUR_STAMP_ID&deferred=true" \
     -F "file=@data.json"

# Upload with stamp validation
curl -X POST "http://localhost:8000/api/v1/data/?stamp_id=YOUR_STAMP_ID&validate_stamp=true" \
     -F "file=@data.json"

# Upload with timing information
curl -X POST "http://localhost:8000/api/v1/data/?stamp_id=YOUR_STAMP_ID&include_timing=true" \
     -F "file=@data.json"

# Upload with maximum redundancy (paranoid mode)
curl -X POST "http://localhost:8000/api/v1/data/?stamp_id=YOUR_STAMP_ID&redundancy=4" \
     -F "file=@data.json"

# Upload with no redundancy (storage-efficient)
curl -X POST "http://localhost:8000/api/v1/data/?stamp_id=YOUR_STAMP_ID&redundancy=0" \
     -F "file=@data.json"
```
Returns 400 if stamp is full (100% utilized), not usable, or invalid redundancy level. Returns 404 if stamp not found. Returns 413 if file exceeds the upload size limit.

**Upload Size Limit Error (413):**
```json
{
  "code": "FILE_TOO_LARGE",
  "message": "Upload exceeds maximum size of 10 MB.",
  "max_size_mb": 10
}
```

**Performance Timing Response** (when `include_timing=true`):
```json
{
  "reference": "abc123...",
  "message": "File 'data.json' uploaded successfully",
  "timing": {
    "stamp_validate_ms": null,
    "file_read_ms": 0.12,
    "bee_upload_ms": 145.67,
    "total_ms": 146.01
  }
}
```

**Server-Timing Header** (always included):
```
Server-Timing: file-read-ms;dur=0.12, bee-upload-ms;dur=145.67, total-ms;dur=146.01
```
The W3C Server-Timing header is visible in browser DevTools Network tab for easy performance profiling.

#### `POST /api/v1/data/manifest?stamp_id={id}&validate_stamp={bool}&deferred={bool}&include_timing={bool}&redundancy={level}`
Upload multiple files as a TAR archive collection/manifest.
- **Performance**: 15x faster than individual uploads (50 files in ~500ms vs ~14s)
- **Request**: Multipart form-data with TAR archive file
- **validate_stamp**: Optional (default: false) - Pre-validate stamp before upload
- **deferred**: Optional (default: false) - Use deferred upload mode (see table above)
- **include_timing**: Optional (default: false) - Include timing breakdown in response
- **redundancy**: Optional (default: 2) - Erasure coding level (0-4) - see redundancy table above
- **Response**: `{"reference": "manifest-hash...", "file_count": 50, "message": "Collection uploaded successfully", "timing": null}`

**Usage Example:**
```bash
# Create TAR archive with multiple files
tar -cvf files.tar file1.json file2.json file3.json

# Upload as collection (direct mode, default redundancy=2)
curl -X POST "http://localhost:8000/api/v1/data/manifest?stamp_id=YOUR_STAMP_ID" \
     -F "file=@files.tar"

# Upload with deferred mode (faster response)
curl -X POST "http://localhost:8000/api/v1/data/manifest?stamp_id=YOUR_STAMP_ID&deferred=true" \
     -F "file=@files.tar"

# Upload with pre-validation
curl -X POST "http://localhost:8000/api/v1/data/manifest?stamp_id=YOUR_STAMP_ID&validate_stamp=true" \
     -F "file=@files.tar"

# Upload with timing information
curl -X POST "http://localhost:8000/api/v1/data/manifest?stamp_id=YOUR_STAMP_ID&include_timing=true" \
     -F "file=@files.tar"

# Upload with maximum redundancy for critical archives
curl -X POST "http://localhost:8000/api/v1/data/manifest?stamp_id=YOUR_STAMP_ID&redundancy=4" \
     -F "file=@files.tar"
```

**Manifest Timing Response** (when `include_timing=true`):
```json
{
  "reference": "manifest-hash...",
  "file_count": 50,
  "message": "Collection uploaded successfully with 50 files",
  "timing": {
    "stamp_validate_ms": null,
    "file_read_ms": 0.35,
    "tar_validate_ms": 1.22,
    "tar_count_ms": 0.89,
    "bee_upload_ms": 487.15,
    "total_ms": 489.92,
    "file_count": 50,
    "ms_per_file": 9.80,
    "files_per_second": 102.06
  }
}
```
The `ms_per_file` and `files_per_second` metrics are useful for comparing local Bee node performance vs gateway performance.

**Accessing individual files after upload:**
- Via Bee node: `GET /bzz/{manifest_reference}/{file_path}`
- Via bee-js: Use `MantarayNode.unmarshal()` to extract individual file references

#### `GET /api/v1/data/{reference}`
Download raw data from Swarm as a file (triggers browser download).
- **Use case**: End users downloading files, browser integration
- **Response**: Raw binary data with user-friendly filename
- **Headers**:
  - `Content-Disposition: attachment; filename="provenance-abc12345.json"`
  - `Content-Type`: Auto-detected (application/json, image/png, etc.)
- **Filenames**:
  - JSON data → `provenance-{hash}.json`
  - Images → `image-{hash}.png/jpg`
  - PDFs → `document-{hash}.pdf`
  - Text → `text-{hash}.txt`
  - Binary → `data-{hash}.bin`

#### `GET /api/v1/data/{reference}/json`
Download data as JSON with metadata (for API clients).
- **Use case**: Web apps, mobile apps, API integrations needing metadata
- **Response**: `{"data": "base64-encoded-content", "content_type": "application/json", "size": 2048, "reference": "abc..."}`
- **Benefits**: Get file metadata without triggering download, programmatic access

### Wallet Information Endpoints

#### `GET /api/v1/wallet`
Get the wallet address and BZZ balance of the connected Bee node.
- **Response**: `{"walletAddress": "0x...", "bzzBalance": "254399000000000"}`
- **Use case**: Identify the Ethereum wallet address and check BZZ token balance
- **BZZ Balance**: Returned in wei (smallest unit of BZZ token)
- **Note**: Only available when connected to local Bee nodes, not public gateways

#### `GET /api/v1/chequebook`
Get the chequebook address and balance information of the connected Bee node.
- **Response**: `{"chequebookAddress": "0x...", "availableBalance": "1000000000", "totalBalance": "1000000000"}`
- **Use case**: Identify the chequebook smart contract address and check available funds
- **Balance Fields**:
  - `availableBalance`: Funds available for creating new postage stamps (in wei)
  - `totalBalance`: Total funds in the chequebook (in wei)
- **Note**: Only available when connected to local Bee nodes, not public gateways

### Stamp Health Check Endpoint

#### `GET /api/v1/stamps/{stamp_id}/check`
Perform a comprehensive health check on a stamp to determine if it can be used for uploads.

**Use cases:**
- Check if a recently purchased stamp is ready for use (propagation delay)
- Verify a stamp before starting a large batch upload
- Diagnose why uploads are failing

**Response:**
```json
{
  "stamp_id": "abc123...",
  "can_upload": true,
  "errors": [],
  "warnings": [
    {
      "code": "HIGH_UTILIZATION",
      "message": "Stamp is 82% utilized.",
      "suggestion": "Monitor usage and consider purchasing additional stamps."
    }
  ],
  "status": {
    "exists": true,
    "local": true,
    "usable": true,
    "utilizationPercent": 82.5,
    "utilizationStatus": "warning",
    "batchTTL": 86400,
    "expectedExpiration": "2026-01-12-17-30",
    "secondsSincePurchase": null,
    "estimatedReadyAt": null,
    "propagationStatus": "ready"
  }
}
```

**Error Codes** (blocking - `can_upload: false`):
| Code | Description |
|------|-------------|
| `NOT_FOUND` | Stamp doesn't exist on the connected node |
| `NOT_LOCAL` | Stamp exists but isn't owned by this Bee node |
| `EXPIRED` | Stamp TTL has reached 0 |
| `NOT_USABLE` | Stamp not yet usable (e.g., propagation delay) |
| `FULL` | Stamp is at 100% utilization |

**Warning Codes** (non-blocking - `can_upload: true`):
| Code | Description |
|------|-------------|
| `LOW_TTL` | Stamp expires in less than 1 hour |
| `NEARLY_FULL` | Stamp is 95%+ utilized |
| `HIGH_UTILIZATION` | Stamp is 80%+ utilized |

## Security

### Upload Size Limits

File uploads are limited to **10 MB** by default. This applies to both `/api/v1/data/` and `/api/v1/data/manifest` endpoints.

```bash
# Configure in .env (value in megabytes)
MAX_UPLOAD_SIZE_MB=10
```

Uploads exceeding the limit receive a **413** response:
```json
{"code": "FILE_TOO_LARGE", "message": "Upload exceeds maximum size of 10 MB.", "max_size_mb": 10}
```

### Rate Limiting

Global per-IP rate limiting protects against abuse. Uses a sliding window algorithm with burst capacity.

```bash
# Configure in .env
RATE_LIMIT_ENABLED=true       # Master switch (default: true)
RATE_LIMIT_PER_MINUTE=60      # Requests per minute per IP (default: 60)
RATE_LIMIT_BURST=10           # Extra burst capacity (default: 10)
```

Every response includes rate limit headers:
```
X-RateLimit-Limit: 70
X-RateLimit-Remaining: 65
```

When the limit is exceeded, the gateway returns **429**:
```json
{"error": "Rate limit exceeded", "detail": "Too many requests. Try again in 42 seconds.", "retry_after": 42}
```

**Exempt paths** (never rate-limited): `/`, `/health`, `/docs`, `/redoc`, `/openapi.json`

**Note**: Rate limiting is automatically disabled when x402 payment is enabled (x402 has its own rate limiting).

### Input Validation

All path and query parameters are validated with strict patterns:

| Parameter | Pattern | Example |
|-----------|---------|---------|
| `stamp_id` | 64-character hex | `a1b2c3...` (64 chars) |
| `reference` | 64-128 character hex | `d4e5f6...` (64-128 chars) |

Invalid inputs receive a **422** response before any processing occurs.

### Error Sanitization

Error messages never expose internal infrastructure details:
- No internal IP addresses or hostnames
- No port numbers or file paths
- No stack traces or library versions
- Generic messages like "The Bee node may be unavailable" instead of connection strings

### Server Header

The `Server` header is suppressed in all responses to prevent version fingerprinting.

## Troubleshooting

### Common Upload Errors

#### "Stamp not found" (404)
**Cause:** The stamp ID doesn't exist on the connected Bee node.

**Solutions:**
1. Verify the stamp ID is correct
2. Check you're connected to the right Bee node with `GET /api/v1/stamps/`
3. The stamp may have expired and been removed from the network

#### "Stamp is not owned by the connected Bee node" (NOT_LOCAL)
**Cause:** The stamp exists on the network but wasn't purchased through this node.

**Solutions:**
1. Use a stamp with `"local": true` from `GET /api/v1/stamps/`
2. Connect to the Bee node that owns this stamp
3. Purchase a new stamp through this node

#### "Stamp is not yet usable" (NOT_USABLE)
**Cause:** After purchasing a stamp, there's a ~2 minute propagation delay before it can be used.

**Solutions:**
1. Wait for the stamp to propagate (~2 minutes after purchase)
2. Check stamp status with `GET /api/v1/stamps/{stamp_id}/check` — use the `propagationStatus` field
3. The `propagationStatus` field transitions: `"propagating"` → `"ready"` when the stamp becomes usable
4. Use `estimatedReadyAt` (ISO 8601) to know when to retry

**Propagation timing fields** (available on all stamp responses):
- `secondsSincePurchase`: How long since purchase through this gateway (null for external stamps)
- `estimatedReadyAt`: ISO 8601 timestamp when stamp should be ready
- `propagationStatus`: `"propagating"` (waiting), `"ready"` (usable), `"unknown"` (not tracked by this gateway)

**Example workflow after purchase:**
```bash
# Purchase stamp
curl -X POST "http://localhost:8000/api/v1/stamps/" \
     -H "Content-Type: application/json" \
     -d '{"duration_hours": 25, "size": "small"}'
# Response: {"batchID": "abc123...", "message": "..."}

# Check propagation status (repeat until propagationStatus="ready")
curl "http://localhost:8000/api/v1/stamps/abc123.../check"
# Response includes: "propagationStatus": "propagating", "estimatedReadyAt": "2026-03-16T12:02:00+00:00"

# Now upload (once propagationStatus="ready")
curl -X POST "http://localhost:8000/api/v1/data/?stamp_id=abc123..." \
     -F "file=@data.json"
```

#### "Stamp has expired" (EXPIRED)
**Cause:** The stamp's TTL has reached 0.

**Solutions:**
1. Purchase a new stamp with `POST /api/v1/stamps/`
2. Extend an existing non-expired stamp with `PATCH /api/v1/stamps/{id}/extend`

#### "Stamp is completely full" (FULL)
**Cause:** The stamp has reached 100% utilization and cannot accept more data.

**Solutions:**
1. Purchase a new stamp with larger capacity (`size: "large"`)
2. Use a different stamp that has remaining capacity

### Structured Error Responses

When uploads fail due to stamp issues, the API returns structured error responses with actionable suggestions:

```json
{
  "detail": {
    "code": "NOT_USABLE",
    "message": "Stamp is not yet usable for uploads. If this stamp was recently purchased, it may take 30-90 seconds for the network to propagate the stamp.",
    "suggestion": "Wait 30-90 seconds after purchase and try again. Check stamp status with GET /api/v1/stamps/{stamp_id}/check to monitor when it becomes usable.",
    "stamp_id": "abc123...",
    "stamp_status": {
      "exists": true,
      "local": true,
      "usable": false,
      "utilizationPercent": 0,
      "batchTTL": 86400
    }
  }
}
```

### Pre-Upload Validation

Use `validate_stamp=true` to check stamp validity before upload:

```bash
curl -X POST "http://localhost:8000/api/v1/data/?stamp_id=abc123...&validate_stamp=true" \
     -F "file=@data.json"
```

This adds a small latency overhead but catches common stamp issues early with clear error messages.

## License

This project is licensed under the [MIT License](LICENSE) - see the LICENSE file for details.

### License Summary
- ✅ **Commercial use** - Use in commercial applications
- ✅ **Distribution** - Distribute copies or substantial portions
- ✅ **Modification** - Modify and create derivative works
- ✅ **Private use** - Use privately without restrictions
- ⚠️ **Include license** - Include MIT license and copyright notice
- ❌ **No liability** - No warranty or liability from authors

The MIT License encourages adoption while maintaining attribution, making it ideal for:
- Research and academic projects
- Commercial integrations
- Open-source ecosystem growth
- Ethereum/Web3 community standards
