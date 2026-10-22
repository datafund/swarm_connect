# app/x402/check_config.py
"""
Check the x402 configuration without starting the gateway (#485).

    python -m app.x402.check_config

deploy.yml runs this in a throwaway container from the new image, with the
same env_file and environment as the gateway it is about to replace, before it
stops the running one. Exit 0: the gateway would start. Exit 1: it would
refuse, and the reason is printed.

It loads settings the way the app does (importing app.core.config) and calls
the same validate_x402_config() the app's startup calls, so the two cannot
disagree. Nothing else from startup runs: no Bee, no HTTP client, no pool, no
state files.

The output goes to the deploy log, which is public for this repository. It
names the network and the facilitator host, never the pay-to address or any
credential.
"""
import sys
from urllib.parse import urlparse


def main() -> int:
    try:
        from app.core.config import settings
        from app.x402.facilitator import validate_x402_config
        validate_x402_config()
    except Exception as e:  # anything that would also stop startup
        # validate_x402_config's messages name settings, never their secret
        # values; Settings hides its input in validation errors.
        print(f"x402 configuration check failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    if not settings.X402_ENABLED:
        print("x402 configuration OK: x402 disabled")
    else:
        host = urlparse(settings.X402_FACILITATOR_URL or "").hostname or "(CDP default)"
        print(f"x402 configuration OK: network={settings.X402_NETWORK}, facilitator host={host}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
