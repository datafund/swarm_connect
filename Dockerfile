# Use an official Python runtime as a parent image
# Python 3.10+ required for x402 SDK
FROM python:3.10-slim

# Set the working directory in the container
WORKDIR /app

# Install from the lockfile, not requirements.txt. requirements.txt only sets
# floors, so every build used to re-resolve and could ship a new major version
# nobody had tested (web3 6 -> 8). The lockfile pins every package, transitive
# ones included, with hashes; --require-hashes makes pip refuse anything else.
COPY requirements.lock .
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

# Copy the rest of the application code
COPY . .

# Version is passed as build arg and written to VERSION file
ARG VERSION=0.0.0-unknown
RUN echo "${VERSION}" > VERSION

# Command to run the application
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
