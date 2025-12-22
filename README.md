# swarm_connect
Simpler server for accessing some Swarm features.

> ⚠️ **ALPHA SOFTWARE - PROOF OF CONCEPT**
> This software is in **Alpha stage** and should be considered a **Proof of Concept**. Use for testing and experimentation only. Not recommended for production use.

> ⚠️ **DATA PERSISTENCE WARNING**
> Storage on Swarm is **rented storage** with limited time periods. The default configuration uses very short rental periods (approximately **1 day**). **Do not expect uploaded data to persist longer than the rental period.** Data will become unavailable when the postage stamp expires.

## Project structure

```
swarm_connect/
├── app/                    # Main application package
│   ├── __init__.py
│   ├── main.py             # FastAPI app instantiation and router inclusion
│   ├── api/                # API specific modules
│   │   ├── __init__.py
│   │   ├── endpoints/      # API route definitions
│   │   │   ├── __init__.py
│   │   │   ├── stamps.py   # Endpoints for Swarm stamp management
│   │   │   ├── data.py     # Endpoints for data upload/download
│   │   │   └── wallet.py   # Endpoints for wallet information
│   │   └── models/         # Pydantic models for request/response validation
│   │       ├── __init__.py
│   │       ├── stamp.py    # Pydantic models for stamp data
│   │       ├── data.py     # Pydantic models for data operations
│   │       └── wallet.py   # Pydantic models for wallet information
│   ├── core/               # Core application logic/configuration
│   │   ├── __init__.py
│   │   └── config.py       # Configuration management (e.g., loading .env)
│   └── services/           # Logic for interacting with external services
│       ├── __init__.py
│       └── swarm_api.py    # Functions to call the EthSwarm Bee API
│
├── tests/                  # Unit and integration tests (Recommended)
│   └── ...
│
├── .env                    # Environment variables (API keys, URLs - NOT committed to Git)
├── .env.example            # Example environment file (Committed to Git)
├── .gitignore              # Files/directories to ignore in Git
├── requirements.txt        # Python package dependencies
├── README.md               # Project description, setup, and usage instructions
└── run.py                  # Script to easily run the development server
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
│  │  │  Integration    │  │  (Requests)     │  │   Retry Logic       │ │   │
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
- **Data Merging**: Merges global network data with local node information for complete stamp details
- **Local Ownership Detection**: Identifies stamps owned/managed by the connected node
- **Enhanced Field Mapping**: Handles different field names between global and local APIs

#### 📁 Data Operations API
- **Unified Data Upload**: Single endpoint handles both JSON and binary data automatically
- **SWIP-Compliant Examples**: Pre-filled with SWIP standard provenance data structure
- **Content-Type Detection**: Automatic handling based on Content-Type header
- **Raw Data Download**: Download data as binary stream or base64-encoded JSON
- **Reference-Based Access**: Access data using Swarm reference hashes
- **Provenance Support**: Built-in examples for data lineage and provenance tracking

#### 🔧 Technical Features
- **FastAPI Framework**: Modern, fast web framework with automatic OpenAPI documentation
- **Auto-Documentation**: Interactive API docs at `/docs` and `/redoc`
- **Type Validation**: Pydantic models ensure data integrity and type safety
- **Error Handling**: Comprehensive error responses with appropriate HTTP status codes
- **Configuration Management**: Environment-based settings with validation
- **Development Server**: Hot-reload development server with SSL support
- **Binary Data Support**: Direct binary upload/download with optional JSON wrapping
- **Modular Design**: Separate endpoints for stamps and data operations

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
- `GET /api/v1/stamps/`: List all available stamps with expiration calculations
- `GET /api/v1/stamps/{stamp_id}`: Retrieve specific stamp batch details
- `PATCH /api/v1/stamps/{stamp_id}/extend`: Extend existing stamps with additional funds

#### Data Operations
- `POST /api/v1/data/?stamp_id={id}&content_type={type}`: Upload raw data to Swarm
- `GET /api/v1/data/{reference}`: Download raw data from Swarm (returns bytes directly)
- `GET /api/v1/data/{reference}/json`: Download data with JSON metadata (base64-encoded)

#### Wallet Information
- `GET /api/v1/wallet`: Get the wallet address and BZZ balance of the Bee node
- `GET /api/v1/chequebook`: Get the chequebook address and balance information of the Bee node

### Key Value Propositions

1. **Complete Gateway Solution**: Full stamp lifecycle and data operations in one service
2. **Simplified Interface**: Clean REST API vs complex Swarm protocols
3. **Enhanced Data**: Adds calculated expiration times to raw stamp data
4. **Reliability**: Robust error handling and timeout management
5. **Developer Experience**: Auto-generated docs and type safety
6. **Flexibility**: Configurable for different Swarm node endpoints
7. **Binary Support**: Native handling of raw data with multiple access patterns

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
List all available postage stamps.
- **Response**: `{"stamps": [...], "total_count": N}`

#### `GET /api/v1/stamps/{stamp_id}`
Get detailed information about a specific stamp.
- **Response**: Detailed stamp information with calculated expiration time

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

#### `POST /api/v1/data/?stamp_id={id}&content_type={type}`
Upload data to Swarm (JSON or binary).
- **Request Body**: JSON data (default) or raw binary data
- **Content-Type**: `application/json` (default) or `application/octet-stream` for binary
- **Response**: `{"reference": "...", "message": "Data uploaded successfully"}`
- **Features**: Pre-filled with SWIP-compliant provenance data example structure

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
