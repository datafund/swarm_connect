# GEMINI.md

This file provides guidance to Gemini when working with code in this repository.

## Project Overview

This is a FastAPI-based service that provides a simplified API layer for accessing Swarm (EthSwarm Bee) features. The application acts as an aggregator that connects to Swarm Bee nodes and exposes specific functionality through REST endpoints.

### Key Features

*   **Stamp Management API**: Purchase, extend, list, and get details for postage stamps.
*   **Data Operations API**: Upload and download raw data to/from Swarm.
*   **FastAPI Framework**: Modern, fast web framework with automatic OpenAPI documentation.
*   **Type Validation**: Pydantic models ensure data integrity and type safety.
*   **Configuration Management**: Environment-based settings with validation.

## Building and Running

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

## Development Conventions

*   **Code Style**: The project follows standard Python conventions.
*   **Testing**: No tests are currently implemented.
*   **Contribution**: No contribution guidelines are specified.

## Architecture Overview

### Application Structure

*   **FastAPI Application**: Main app defined in `app/main.py` with modular router inclusion.
*   **Configuration Management**: Centralized in `app/core/config.py` using Pydantic Settings with .env support.
*   **API Layer**: Organized under `app/api/` with separate endpoints and models.
*   **Service Layer**: External API integration handled in `app/services/`.
*   **Models**: Pydantic models for request/response validation in `app/api/models/`.

### Key Components

*   **Core Configuration (`app/core/config.py`)**: Manages environment variables and provides cached settings.
*   **Swarm Integration (`app/services/swarm_api.py`)**: Handles HTTP requests to the Swarm Bee API.
*   **Stamps API (`app/api/endpoints/stamps.py`)**: Provides endpoints for stamp management.
*   **Data API (`app/api/endpoints/data.py`)**: Provides endpoints for data operations.
*   **Data Models (`app/api/models/`)**: Pydantic models for request/response validation.

### Environment Configuration

Required environment variables:

*   `SWARM_BEE_API_URL`: URL to Swarm Bee node API (e.g., `https://api.gateway.ethswarm.org`)

Optional environment variables:

*   `HOST`: Server host (default: `127.0.0.1`)
*   `PORT`: Server port (default: `8000`)
*   `RELOAD`: Enable auto-reload (default: `true`)
*   `SSL_KEYFILE`/`SSL_CERTFILE`: For HTTPS development

## API Endpoints

### Core Endpoints

*   `GET /`: Health check endpoint.

### Stamp Management

*   `POST /api/v1/stamps/`: Purchase new postage stamps.
*   `GET /api/v1/stamps/`: List all available stamps with expiration calculations.
*   `GET /api/v1/stamps/{stamp_id}`: Retrieve specific stamp batch details.
*   `PATCH /api/v1/stamps/{stamp_id}/extend`: Extend existing stamps with additional funds.

### Data Operations

*   `POST /api/v1/data/?stamp_id={id}&content_type={type}`: Upload raw data to Swarm.
*   `GET /api/v1/data/{reference}`: Download raw data from Swarm (returns bytes directly).
*   `GET /api/v1/data/{reference}/json`: Download data with JSON metadata (base64-encoded).
