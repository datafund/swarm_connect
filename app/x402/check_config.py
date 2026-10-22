# app/x402/check_config.py
"""
Check the x402 configuration without starting the gateway (#485).

    python -m app.x402.check_config

deploy.yml runs this in a throwaway container from the new image, with the
same env_file and environment as the gateway it is about to replace, before it
stops the running one.

Exit codes:
- 0: the gateway would start.
- 3: the gateway would refuse this x402 configuration; the reason is printed.
- anything else: the check could not decide. A settings error is one case:
  under `python -m`, importing the app.x402 package already builds Settings
  (app/x402/__init__.py imports dependency.py), so it fails before main() runs
  and exits 1 with pydantic's traceback. That traceback names the setting but
  not its value (Settings sets hide_input_in_errors). Startup fails the same
  way on the same error.

deploy.yml labels 3 as a refusal and anything else as a check that could not
run, so a compose or daemon error is not reported as a bad configuration.

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

EXIT_OK = 0
EXIT_REFUSED = 3
EXIT_COULD_NOT_RUN = 1

# The prefix validate_x402_config() puts on every refusal.
REFUSAL_PREFIX = "Refusing to start with x402 enabled"


def main() -> int:
    from app.core.config import settings
    from app.x402 import facilitator
    try:
        facilitator.validate_x402_config()
    except RuntimeError as e:
        if str(e).startswith(REFUSAL_PREFIX):
            # Its messages name settings, never their secret values.
            print(f"x402 configuration check failed: {e}", file=sys.stderr)
            return EXIT_REFUSED
        print(f"x402 configuration check could not run: RuntimeError: {e}", file=sys.stderr)
        return EXIT_COULD_NOT_RUN
    except Exception as e:
        print(f"x402 configuration check could not run: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_COULD_NOT_RUN
    if not settings.X402_ENABLED:
        print("x402 configuration OK: x402 disabled")
    else:
        host = urlparse(settings.X402_FACILITATOR_URL or "").hostname or "(CDP default)"
        print(f"x402 configuration OK: network={settings.X402_NETWORK}, facilitator host={host}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
