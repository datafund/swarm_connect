# swarm_connect
Simpler server for accessing some Swarm features.

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
│   │   │   └── data.py     # Endpoints for data upload/download
│   │   └── models/         # Pydantic models for request/response validation
│   │       ├── __init__.py
│   │       ├── stamp.py    # Pydantic models for stamp data
│   │       └── data.py     # Pydantic models for data operations
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



```
python3 -m venv /path/to/pythonvenv
# use the binaries in that folder from now on

pip install -r requirements.txt

# Copy .env.example to .env.

# Edit .env and ensure SWARM_BEE_API_URL points to your Bee node's API endpoint (e.g., http://localhost:1633 or the public gateway https://api.gateway.ethswarm.org).

# if port 8000 is taken, use a different one, e.g.:
export PORT=8001

```

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
- **Purchase Stamps**: Create new postage stamps with specified amount and depth
- **Extend Stamps**: Add funds to existing stamps to extend their validity
- **List All Stamps**: Retrieve comprehensive list of all available stamps with enhanced data
- **Get Stamp Details**: Fetch specific stamp information by batch ID
- **Expiration Calculation**: Automatically calculates stamp expiration time (current time + TTL)
- **Data Merging**: Merges global network data with local node information for complete stamp details
- **Local Ownership Detection**: Identifies stamps owned/managed by the connected node
- **Enhanced Field Mapping**: Handles different field names between global and local APIs

#### 📁 Data Operations API
- **Raw Data Upload**: Upload binary data directly to Swarm network
- **Raw Data Download**: Download data as binary stream or base64-encoded JSON
- **Content-Type Support**: Configurable content types for uploaded data
- **Reference-Based Access**: Access data using Swarm reference hashes

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
Purchase a new postage stamp.
- **Request Body**: `{"amount": 2000000000, "depth": 17, "label": "my-stamp"}`
- **Response**: `{"batchID": "...", "message": "Postage stamp purchased successfully"}`

#### `GET /api/v1/stamps/`
List all available postage stamps.
- **Response**: `{"stamps": [...], "total_count": N}`

#### `GET /api/v1/stamps/{stamp_id}`
Get detailed information about a specific stamp.
- **Response**: Detailed stamp information with calculated expiration time

#### `PATCH /api/v1/stamps/{stamp_id}/extend`
Extend an existing stamp by adding more funds.
- **Request Body**: `{"amount": 2000000000}`
- **Response**: `{"batchID": "...", "message": "Postage stamp extended successfully"}`

### Data Operation Endpoints

#### `POST /api/v1/data/?stamp_id={id}&content_type={type}`
Upload raw binary data to Swarm.
- **Request Body**: Raw binary data
- **Response**: `{"reference": "...", "message": "Data uploaded successfully"}`

#### `GET /api/v1/data/{reference}`
Download raw data from Swarm as binary stream.
- **Response**: Raw binary data with `application/octet-stream` content type

#### `GET /api/v1/data/{reference}/json`
Download data as JSON with base64-encoded content.
- **Response**: `{"data": "base64-encoded-content", "size": N, "reference": "..."}`
