"""Read and run the two steps of deploy.yml that write the gateway env files.

Shared by the tests that check what those files contain. A step is run the way
the runner runs it: GitHub Actions first replaces every ${{ }} expression in
the step (in its env: map and in its script), then runs the script with
`bash -e`. Here the expressions are rendered by `render`, and /opt/ in the
script is rewritten to a temporary directory.
"""
import os
import re
import subprocess
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
DEPLOY_YML = REPO / ".github" / "workflows" / "deploy.yml"
BEFORE_654 = Path(__file__).resolve().parent / "fixtures" / "deploy_env_before_654"

# The shell the deploy runner uses. /bin/bash is GNU bash on the runner and
# bash 3.2 on macOS, so the scripts must not need bash 4.
BASH = "/bin/bash"

# env-file step name -> (env file it writes, fixture holding today's heredoc)
ENV_STEPS = {
    "write env file for dev": ("/opt/swarm_connect_dev.env", "dev.heredoc"),
    "write env file for main": ("/opt/swarm_connect.env", "main.heredoc"),
}

EXPRESSION = re.compile(r"\$\{\{ (vars|secrets)\.(\w+)(?: \|\| '([^']*)')? \}\}")


def steps():
    workflow = yaml.safe_load(DEPLOY_YML.read_text())
    return {s["name"]: s for job in workflow["jobs"].values()
            for s in job.get("steps", []) if "name" in s}


def render(text, variables):
    """Replace `${{ vars.NAME || 'default' }}` (or secrets.NAME, with or without
    a default) the way Actions does: an unset or empty value gives the default,
    or an empty string when there is none. `variables` holds both vars and
    secrets by name."""
    def sub(m):
        value = variables.get(m.group(2), "")
        return value if value else (m.group(3) or "")
    return EXPRESSION.sub(sub, text)


def spec_lines(step):
    """The lines of the step's settings list (the quoted heredoc in run:)."""
    m = re.search(r"cat << 'EOF'\n(.*?\n)EOF\n", step["run"], re.S)
    assert m, f"no settings list in {step['name']}"
    return m.group(1).splitlines()


def written_names(step):
    """Setting names the step writes as NAME=<value of $NAME>, in order. Other
    lines of the list (comments, literal NAME=value lines) are copied as is."""
    return [line for line in spec_lines(step)
            if line and not line.startswith("#") and "=" not in line]


def old_file(step_name, variables):
    """The env file today's heredoc (frozen from origin/dev before #654) writes."""
    return render((BEFORE_654 / ENV_STEPS[step_name][1]).read_text(), variables)


def run_step(step, variables, tmp_path):
    """Run the step's script with bash -e as the runner would, with /opt/
    rewritten to tmp_path/opt/. Returns (completed process, env file path)."""
    opt = tmp_path / "opt"
    opt.mkdir(exist_ok=True)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    for key, value in (step.get("env") or {}).items():
        env[key] = render(str(value), variables)
    script = render(step["run"], variables).replace("/opt/", f"{opt}/")
    path = tmp_path / "step.sh"
    path.write_text(script)
    proc = subprocess.run([BASH, "-e", str(path)], env=env, capture_output=True,
                          text=True, timeout=30)
    return proc, opt / Path(ENV_STEPS[step["name"]][0]).name
