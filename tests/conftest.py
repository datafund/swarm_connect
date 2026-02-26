"""
Shared test configuration.

Sets environment variables before any app modules are imported,
ensuring test-friendly defaults (e.g., rate limiting disabled).
"""
import os

# Disable global rate limiting during tests to prevent 429 responses
# from interfering with test assertions. Rate limiter unit tests
# test the component directly without relying on middleware.
os.environ["RATE_LIMIT_ENABLED"] = "false"
