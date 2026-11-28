"""X402_PROTOCOL_VERSIONS in the deploy configuration (#609).

deploy.yml writes each environment's gateway env file from GitHub variables.
The setting must be in both, default to 1 (x402 v1 only, the behaviour before
v2), and reach the deploy check (`python -m app.x402.check_config`), which runs
through `docker compose run` with the service's env_file, so a bad value or a
facilitator without v2 fails the deploy before the running gateway is stopped.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

from tests import deploy_env_steps

REPO = Path(__file__).resolve().parent.parent
COMPOSE_YML = REPO / "docker-compose.yml"

# The step's env: entry. The value reaches the script only through env:, and
# the script writes NAME=$NAME for every name in its settings list (#654).
EXPRESSION = "${{ vars.X402_PROTOCOL_VERSIONS || '1' }}"

# env-file step name -> (env file it writes, compose service that reads it)
ENVIRONMENTS = {
    "write env file for dev": ("/opt/swarm_connect_dev.env", "provenance_gateway_dev"),
    "write env file for main": ("/opt/swarm_connect.env", "provenance_gateway"),
}


def _steps():
    return deploy_env_steps.steps()


def test_both_environments_write_the_setting_with_default_1():
    steps = _steps()
    for name in ENVIRONMENTS:
        step = steps[name]
        assert step["env"]["X402_PROTOCOL_VERSIONS"] == EXPRESSION, name
        assert deploy_env_steps.written_names(step).count("X402_PROTOCOL_VERSIONS") == 1, name
        assert not any(line.startswith("X402_PROTOCOL_VERSIONS=")
                       for line in deploy_env_steps.spec_lines(step)), name


def test_an_unset_or_empty_variable_renders_1_and_a_set_one_is_kept(tmp_path):
    steps = _steps()
    for name in ENVIRONMENTS:
        for i, (variables, expected) in enumerate((({}, "1"), ({"X402_PROTOCOL_VERSIONS": ""}, "1"),
                                                   ({"X402_PROTOCOL_VERSIONS": "1,2"}, "1,2"))):
            run_dir = tmp_path / f"{name.split()[-1]}-{i}"
            run_dir.mkdir()
            proc, env_file = deploy_env_steps.run_step(steps[name], variables, run_dir)
            assert proc.returncode == 0, proc.stdout + proc.stderr
            lines = env_file.read_text().splitlines()
            assert [line for line in lines if line.startswith("X402_PROTOCOL_VERSIONS=")] == \
                [f"X402_PROTOCOL_VERSIONS={expected}"], (name, variables)


def test_the_deploy_check_reads_the_env_file_the_setting_is_written_to():
    """check_config runs via `docker compose run "$BRANCH_SERVICE"`, so it gets
    the service's env_file; nothing in `environment:` may override the setting."""
    compose = yaml.safe_load(COMPOSE_YML.read_text())
    for _name, (env_file, service) in ENVIRONMENTS.items():
        svc = compose["services"][service]
        assert svc["env_file"] == [env_file], service
        environment = svc.get("environment") or []
        assert not any(str(e).startswith("X402_PROTOCOL_VERSIONS") for e in environment), service
    deploy = _steps()["deploy"]["run"]
    assert "provenance_gateway_dev" in deploy and "provenance_gateway;" in deploy
    assert re.search(r'docker compose run --rm --no-deps -T "\$BRANCH_SERVICE"[\s\\]+python -m app\.x402\.check_config',
                     deploy)


def _run_check(tmp_path, versions):
    # Placeholders only: this repository is public. 127.0.0.1:1 refuses at once.
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(REPO),
        "SWARM_BEE_API_URL": "http://localhost:1",
        "X402_ENABLED": "true",
        "X402_NETWORK": "base-sepolia",
        "X402_FACILITATOR_URL": "http://127.0.0.1:1/facilitator",
        "X402_PAY_TO_ADDRESS": "0x" + "ab" * 20,
        "X402_PROTOCOL_VERSIONS": versions,
    }
    proc = subprocess.run([sys.executable, "-m", "app.x402.check_config"],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    return proc.returncode, proc.stdout + proc.stderr


def test_the_check_passes_with_the_default(tmp_path):
    rc, out = _run_check(tmp_path, "1")
    assert rc == 0, out


def test_the_check_refuses_v2_without_a_facilitator_that_lists_it(tmp_path):
    """Exit 3: deploy.yml reports `x402 configuration refused` and stops."""
    rc, out = _run_check(tmp_path, "1,2")
    assert rc == 3, out
    assert "X402_PROTOCOL_VERSIONS includes 2" in out
    assert "set X402_PROTOCOL_VERSIONS=1" in out


def test_the_check_fails_on_a_bad_value(tmp_path):
    """A settings error: not 0, so the deploy stops (`could not run`)."""
    rc, out = _run_check(tmp_path, "1,1")
    assert rc not in (0, 3), out
    assert "X402_PROTOCOL_VERSIONS" in out
