"""The deploy-time x402 configuration check (#485).

`python -m app.x402.check_config` runs in a throwaway container from the new
image, with the env the new gateway will get, before deploy.yml replaces the
running gateway. It must agree with startup: same settings loading, same
validate_x402_config(). A refused configuration then fails the deploy instead
of taking the gateway down.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.config import settings
from app.x402 import facilitator

# check_config's exit codes: deploy.yml labels a refusal differently from a
# check that could not run at all (a compose, daemon or settings error).
OK, REFUSED = 0, 3

REPO = Path(__file__).resolve().parent.parent
DEPLOY_YML = REPO / ".github" / "workflows" / "deploy.yml"

# Placeholders only: this repository is public.
PAY_TO = "0x" + "ab" * 20
BEARER = "sentinel-bearer-token-do-not-print"
CDP_ID = "sentinel-cdp-key-id-do-not-print"
CDP_SECRET = "sentinel-cdp-secret-do-not-print"

VALID = {
    "X402_ENABLED": "true",
    "X402_NETWORK": "base-sepolia",
    "X402_FACILITATOR_URL": "https://x402.org/facilitator",
    "X402_PAY_TO_ADDRESS": PAY_TO,
}


def run_check(tmp_path, overrides=None, bee_url="http://localhost:1"):
    """Run the entry point as deploy.yml does: a fresh process, settings from env.

    cwd is an empty directory so no .env (load_dotenv, env_file=".env") can
    leak values in; the environment is built from scratch for the same reason.
    """
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(REPO)}
    if bee_url is not None:
        env["SWARM_BEE_API_URL"] = bee_url
    env.update(VALID)
    env.update(overrides or {})
    env = {k: v for k, v in env.items() if v is not None}
    proc = subprocess.run(
        [sys.executable, "-m", "app.x402.check_config"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_valid_config_passes(tmp_path):
    code, out = run_check(tmp_path)
    assert code == OK, out
    assert "x402 configuration OK" in out


def test_mainnet_with_testnet_facilitator_fails_with_the_startup_message(tmp_path):
    code, out = run_check(tmp_path, {"X402_NETWORK": "base"})
    assert code == REFUSED, out
    assert "Refusing to start with x402 enabled" in out
    assert "mainnet" in out


def test_disabled_x402_passes_without_checking(tmp_path):
    code, out = run_check(tmp_path, {"X402_ENABLED": "false", "X402_NETWORK": "nonsense"})
    assert code == OK, out


def test_missing_bee_url_fails_like_startup(tmp_path):
    """A settings error stops startup, so it must stop the check too.

    It is not an x402 refusal, so it must not get the refusal exit code: under
    `python -m`, importing the app.x402 package builds Settings before main()
    runs, and the error is pydantic's traceback (input hidden), exit 1.
    """
    code, out = run_check(tmp_path, bee_url=None, overrides={
        "X402_FACILITATOR_BEARER_TOKEN": BEARER, "X402_FACILITATOR_CDP_API_KEY_SECRET": CDP_SECRET})
    assert code not in (OK, REFUSED), out
    assert "SWARM_BEE_API_URL" in out
    assert "Refusing to start" not in out
    for value in (PAY_TO, BEARER, CDP_SECRET):
        assert value not in out


@pytest.mark.parametrize("overrides", [
    # passes: bearer auth over https on a mainnet
    {"X402_NETWORK": "base", "X402_FACILITATOR_URL": "https://facilitator.example.com",
     "X402_FACILITATOR_BEARER_TOKEN": BEARER},
    # fails: credentials over plain http
    {"X402_FACILITATOR_URL": "http://facilitator.example.com", "X402_FACILITATOR_BEARER_TOKEN": BEARER},
    # fails: both kinds of credentials
    {"X402_FACILITATOR_BEARER_TOKEN": BEARER, "X402_FACILITATOR_CDP_API_KEY_ID": CDP_ID,
     "X402_FACILITATOR_CDP_API_KEY_SECRET": CDP_SECRET},
    # fails: mainnet on the public testnet facilitator
    {"X402_NETWORK": "base"},
])
def test_no_secret_or_pay_to_in_output(tmp_path, overrides):
    """Actions logs of this repository are public."""
    _, out = run_check(tmp_path, overrides)
    for value in (PAY_TO, BEARER, CDP_ID, CDP_SECRET):
        assert value not in out
        assert value.lower() not in out.lower()


@pytest.fixture
def x402(monkeypatch):
    facilitator.reset_facilitator_client()
    yield monkeypatch
    facilitator.reset_facilitator_client()


PARITY_CASES = [
    {},
    {"X402_NETWORK": "base"},
    {"X402_NETWORK": "base", "X402_FACILITATOR_URL": "https://facilitator.example.com"},
    {"X402_NETWORK": "polygon"},
    {"X402_PAY_TO_ADDRESS": "0x" + "00" * 20},
    {"X402_PAY_TO_ADDRESS": ""},
    {"X402_FACILITATOR_URL": "ftp://facilitator.example.com"},
    {"X402_ENABLED": "false", "X402_NETWORK": "nonsense"},
]


@pytest.mark.parametrize("overrides", PARITY_CASES)
def test_check_agrees_with_startup(tmp_path, x402, overrides):
    """The check exits non-zero exactly when startup's validation refuses."""
    merged = {**VALID, **overrides}
    x402.setattr(settings, "X402_ENABLED", merged["X402_ENABLED"] == "true")
    x402.setattr(settings, "X402_NETWORK", merged["X402_NETWORK"])
    x402.setattr(settings, "X402_FACILITATOR_URL", merged["X402_FACILITATOR_URL"])
    x402.setattr(settings, "X402_PAY_TO_ADDRESS", merged["X402_PAY_TO_ADDRESS"])
    x402.setattr(settings, "X402_FACILITATOR_CDP_API_KEY_ID", None)
    x402.setattr(settings, "X402_FACILITATOR_CDP_API_KEY_SECRET", None)
    x402.setattr(settings, "X402_FACILITATOR_BEARER_TOKEN", None)
    try:
        facilitator.validate_x402_config()
        startup_refuses = False
    except RuntimeError:
        startup_refuses = True
    code, out = run_check(tmp_path, overrides)
    assert code == (REFUSED if startup_refuses else OK), out


def test_check_is_the_startup_check(x402, capsys):
    """Same function, not a copy: a change to the validation reaches both."""
    from app.x402 import check_config
    calls = []

    def fake():
        calls.append(1)
        raise RuntimeError("Refusing to start with x402 enabled: from the startup check")

    x402.setattr(facilitator, "validate_x402_config", fake)
    assert check_config.main() == REFUSED
    assert calls == [1]
    assert "from the startup check" in capsys.readouterr().err


def test_an_error_that_is_not_a_refusal_is_not_labelled_one(x402, capsys):
    """Only validate_x402_config's refusal gets exit 3; anything else means the
    check could not decide, and deploy.yml must not call that a refusal."""
    from app.x402 import check_config

    def broken():
        raise ImportError("something unrelated is missing")

    x402.setattr(facilitator, "validate_x402_config", broken)
    code = check_config.main()
    assert code not in (OK, REFUSED)
    err = capsys.readouterr().err
    assert "could not run" in err
    assert "Refusing to start" not in err


# --- deploy.yml: the check runs before the running gateway is replaced -------

def _deploy_step(text):
    m = re.search(r"\n      - name: deploy\n(.*?)(?=\n      - name: )", text, re.S)
    assert m, "deploy step not found"
    return m.group(1)


def test_deploy_runs_the_check_before_replacing_the_gateway():
    text = DEPLOY_YML.read_text()
    step = _deploy_step(text)
    check = step.find("python -m app.x402.check_config")
    assert check != -1, "deploy step does not run the x402 configuration check"
    # After the checkout swap and the .env assembly it needs for compose...
    assert step.find("mv swarm_connect.new swarm_connect") < check
    assert step.find('echo "COMPOSE_PROJECT_NAME=swarm_connect" >> .env') < check
    # ...and before anything stops or recreates the running gateway.
    assert check < step.find('docker compose stop "$BRANCH_SERVICE"')
    assert check < step.find('docker compose up -d --force-recreate --no-deps "$BRANCH_SERVICE"')
    # The env files are written and the image built in earlier steps.
    assert text.find("- name: build docker image") < text.find("- name: deploy\n")
    assert text.find("- name: write env file for main") < text.find("- name: deploy\n")


def test_deploy_check_uses_the_new_containers_env_and_fails_the_job():
    step = _deploy_step(DEPLOY_YML.read_text())
    block = step[:step.find('docker compose stop "$BRANCH_SERVICE"')]
    # The service's own env_file and environment, the new image, no other services.
    assert re.search(r'docker compose run --rm --no-deps -T "\$BRANCH_SERVICE"[\s\\]+python -m app\.x402\.check_config', block)
    after = block[block.find("python -m app.x402.check_config"):]
    # Exit 3 is a refusal; any other non-zero exit (compose interpolation,
    # the daemon, a settings error) is labelled as a check that could not run.
    # Both stop the job before the gateway is touched.
    refused = re.search(r"\n\s*3\)(.*?);;", after, re.S)
    assert refused and "x402 configuration refused" in refused.group(1) and "exit 1" in refused.group(1)
    other = re.search(r"\n\s*\*\)(.*?);;", after, re.S)
    assert other and "could not run" in other.group(1) and "exit 1" in other.group(1)
    assert "refused" not in other.group(1)
