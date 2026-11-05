# Official Python image, pinned by digest so every host and CI build the same
# base (#617, #430). Without the digest each deploy host built from whatever
# python:*-slim it had cached, so hosts could differ in patch level.
# The digest is the multi-platform index: linux/amd64 (the deploy hosts and
# image.yml) gets one fixed manifest from it, and arm64 dev machines still
# build natively.
# Pinned 2026-11-05: Python 3.12.16, Debian trixie.
#
# To bump it (a CPython or Debian security release, or monthly):
#   docker buildx imagetools inspect python:3.12-slim
# copy the top-level "Digest:" (the index, not a per-platform manifest) here,
# update the line above, and let the pull request's image.yml build prove it.
# A minor-version move (3.12 -> 3.13) also changes tests.yml python-version and
# the --python-version of requirements.lock; tests/test_ci_workflow.py fails
# until all of them agree.
FROM python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30

# Set the working directory in the container
WORKDIR /app

# Install from the lockfile, not requirements.txt. requirements.txt only sets
# floors, so every build used to re-resolve and could ship a new major version
# nobody had tested (web3 6 -> 8). The lockfile pins every package, transitive
# ones included, with hashes; --require-hashes makes pip refuse anything else.
COPY requirements.lock .
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*
# --no-deps: the lockfile is complete, so pip must not resolve anything itself.
# Without it, pip 23 treats a dependency's extra (x402 -> fastapi[standard]) as a
# new unpinned requirement and refuses the build in --require-hashes mode.
# pip check then fails the build if the lock ever misses a dependency.
RUN pip install --no-cache-dir --no-deps --require-hashes -r requirements.lock \
    && pip check

# Copy the rest of the application code
COPY . .

# Version is passed as build arg and written to VERSION file
ARG VERSION=0.0.0-unknown
RUN echo "${VERSION}" > VERSION

# The code is read-only to the runtime user below, so Python could never cache
# bytecode at run time; compile it once here instead.
RUN python -m compileall -q app

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
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header", "--timeout-graceful-shutdown", "10"]
