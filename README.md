# swarm_connect
Simpler server for accessing some Swarm features.

## Project structure

```
swarm_api_aggregator/
├── app/                    # Main application package
│   ├── __init__.py
│   ├── main.py             # FastAPI app instantiation and router inclusion
│   ├── api/                # API specific modules
│   │   ├── __init__.py
│   │   ├── deps.py         # Dependency injection functions (e.g., for auth later)
│   │   ├── endpoints/      # API route definitions
│   │   │   ├── __init__.py
│   │   │   └── stamps.py   # Endpoint(s) related to Swarm Stamps
│   │   └── models/         # Pydantic models for request/response validation
│   │       ├── __init__.py
│   │       └── stamp.py    # Pydantic model(s) for Stamp data
│   ├── core/               # Core application logic/configuration
│   │   ├── __init__.py
│   │   ├── config.py       # Configuration management (e.g., loading .env)
│   │   └── security.py     # Security related functions (auth, https setup later)
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

Swarm Connect is a FastAPI-based API gateway that simplifies access to Ethereum Swarm (distributed storage network) functionality. Instead of clients directly calling complex Swarm Bee node APIs, they can use this cleaner, more focused interface.

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
│  │  │   Health Check  │  │   OpenAPI Docs  │  │   Stamps Endpoint   │ │   │
│  │  │   GET /         │  │   /docs /redoc  │  │ GET /api/v1/stamps/ │ │   │
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

#### 🚀 API Features
- **Stamp Management**: Complete postage stamp lifecycle (purchase, lookup, extend, list)
- **Data Operations**: Upload and download raw data to/from Swarm network
- **Expiration Calculation**: Automatically calculates stamp expiration time (current time + TTL)
- **Stamp Purchase Options**: Both user-friendly (time/size) and advanced (amount/depth) interfaces
- **Cost Estimation**: Pre-purchase cost calculations for stamps
- **JSON API**: RESTful endpoints with structured JSON responses

#### 🔧 Technical Features
- **FastAPI Framework**: Modern, fast web framework with automatic OpenAPI documentation
- **Auto-Documentation**: Interactive API docs at `/docs` and `/redoc`
- **Type Validation**: Pydantic models ensure data integrity and type safety
- **Error Handling**: Comprehensive error responses with appropriate HTTP status codes
- **Configuration Management**: Environment-based settings with validation
- **Development Server**: Hot-reload development server with SSL support
- **Gateway Pattern**: Centralized API layer for all Swarm operations
- **Binary Data Support**: Raw data upload/download with proper content handling

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
- Makes HTTP calls to Swarm Bee node
- Handles network errors and timeouts
- Parses and normalizes API responses

#### Model Layer (`app/api/models/stamp.py`)
- Validates response data structure
- Handles optional fields and type conversion
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

### Key Value Propositions

1. **Simplified Interface**: Clean REST API vs complex Swarm protocols
2. **Complete Stamp Management**: Purchase, extend, list, and monitor stamps
3. **Data Gateway**: Unified upload/download interface for Swarm storage
4. **Enhanced Data**: Adds calculated expiration times and cost estimates
5. **Reliability**: Robust error handling and timeout management
6. **Developer Experience**: Auto-generated docs and type safety
7. **Flexibility**: Configurable for different Swarm node endpoints
8. **Gateway Architecture**: Central API layer enabling other tools (CLI, MCP)
