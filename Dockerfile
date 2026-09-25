# Use an official Python runtime as a parent image
# Python 3.10+ required for x402 SDK
FROM python:3.10-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file and install dependencies
COPY requirements.txt .
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Version is passed as build arg and written to VERSION file
ARG VERSION=0.0.0-unknown
RUN echo "${VERSION}" > VERSION

# Run as an unprivileged user rather than root. The UID/GID are fixed so a host
# directory bind-mounted over /app/data can be given to this user by number
# (the deploy workflow chowns /opt/swarm_connect*_data to 10001). The code stays
# root-owned and therefore read-only to the process; only the state and log
# directories are writable.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --no-create-home app \
    && mkdir -p /app/data /app/logs \
    && chown app:app /app/data /app/logs
USER app

# Command to run the application
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
