"""CI tests what the image ships (#523).

tests.yml is the pull-request test gate and, through deploy.yml, the deploy
gate. It must install the same locked, hash-checked set as the Dockerfile, not
requirements.txt (floors only), and fail on a lock that pip check rejects.
The workflow files are read as text, like test_x402_check_config.py does.

image.yml builds the image on every pull request with no layer cache, so a
lock that a clean build rejects fails the PR instead of the next deploy, and
checks that the built image answers GET / with Bee unreachable (#486).

CI, the image and the lock name one Python minor version, and the image's
base is pinned by digest (#617).
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TESTS_YML = REPO / ".github" / "workflows" / "tests.yml"
DEPLOY_YML = REPO / ".github" / "workflows" / "deploy.yml"
DOCKERFILE = REPO / "Dockerfile"
IMAGE_YML = REPO / ".github" / "workflows" / "image.yml"
LOCK = REPO / "requirements.lock"
REQUIREMENTS_TXT = REPO / "requirements.txt"

LOCK_INSTALL = re.compile(r"pip install\b[^\n]*-r requirements\.lock")


def _run_lines(text):
    """The commands of every `run:` key, one per entry."""
    return [m.group(1).strip() for m in re.finditer(r"^\s*(?:-\s*)?run:\s*(.+)$", text, re.M)]


def _lock_install(text):
    found = [line for line in text.splitlines() if LOCK_INSTALL.search(line)]
    assert len(found) == 1, f"expected one install from requirements.lock, found {found}"
    return found[0]


def test_tests_workflow_installs_the_lock_with_hashes():
    install = _lock_install(TESTS_YML.read_text())
    assert "--require-hashes" in install
    assert "--no-deps" in install
    assert install.strip() in [f"run: {cmd}" for cmd in _run_lines(TESTS_YML.read_text())]


def test_tests_workflow_does_not_install_requirements_txt():
    for cmd in _run_lines(TESTS_YML.read_text()):
        assert "requirements.txt" not in cmd, cmd


def test_tests_workflow_runs_pip_check_after_install_and_before_pytest():
    cmds = _run_lines(TESTS_YML.read_text())
    install = next(i for i, c in enumerate(cmds) if LOCK_INSTALL.search(c))
    check = next((i for i, c in enumerate(cmds) if re.fullmatch(r"(python -m )?pip check", c)), None)
    pytest_ = next(i for i, c in enumerate(cmds) if "pytest" in c)
    assert check is not None, "tests.yml does not run pip check as its own step"
    assert install < check < pytest_


def test_tests_workflow_pip_cache_is_keyed_on_the_lock():
    text = TESTS_YML.read_text()
    assert re.search(r"^\s*cache-dependency-path:\s*requirements\.lock\s*$", text, re.M)


def test_tests_workflow_flags_match_the_dockerfile():
    docker = _lock_install(DOCKERFILE.read_text())
    ci = _lock_install(TESTS_YML.read_text())
    for flag in ("--no-deps", "--require-hashes"):
        assert flag in docker and flag in ci, flag


def test_deploy_workflow_gates_on_tests_workflow():
    assert re.search(r"^\s*uses:\s*\./\.github/workflows/tests\.yml\s*$", DEPLOY_YML.read_text(), re.M)


def test_tests_workflow_gates_cannot_be_turned_off_silently():
    """No step may be marked continue-on-error, and no gate command may
    swallow its exit status, or a failed install, pip check or test run would
    leave the job green."""
    text = TESTS_YML.read_text()
    assert "continue-on-error" not in text
    gates = [c for c in _run_lines(text)
             if LOCK_INSTALL.search(c) or re.fullmatch(r"(python -m )?pip check", c) or "pytest" in c]
    assert len(gates) == 3, gates
    swallow = re.compile(r"\|\|\s*(true|:|exit 0)\b|;\s*(true|:|exit 0)\s*$|\|\|\s*echo\b")
    for cmd in gates:
        assert not swallow.search(cmd), cmd


# --- image.yml: the image is built on pull requests, from a clean state (#486)

def _run_blocks(text):
    """Every `run:` value, a `run: |` block joined into one string."""
    blocks, lines, i = [], text.splitlines(), 0
    while i < len(lines):
        m = re.match(r"^(\s*)(?:-\s*)?run:\s*(.*)$", lines[i])
        if not m:
            i += 1
            continue
        if m.group(2).strip() != "|":
            blocks.append(m.group(2).strip())
            i += 1
            continue
        indent, body, i = len(m.group(1)), [], i + 1
        while i < len(lines) and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > indent):
            body.append(lines[i].strip())
            i += 1
        blocks.append("\n".join(body))
    return blocks


def _docker_build():
    builds = [b for b in _run_blocks(IMAGE_YML.read_text()) if re.search(r"\bdocker build\b", b)]
    assert len(builds) == 1, f"expected one docker build in image.yml, found {builds}"
    return builds[0]


def _smoke():
    smokes = [b for b in _run_blocks(IMAGE_YML.read_text()) if "docker run" in b]
    assert len(smokes) == 1, f"expected one docker run in image.yml, found {smokes}"
    return smokes[0]


def test_image_workflow_runs_on_pull_request():
    on = re.search(r"^on:\s*\n((?:[ \t]+.*\n)+)", IMAGE_YML.read_text(), re.M)
    assert on, "image.yml has no on: block"
    assert re.search(r"^\s+pull_request:", on.group(1), re.M)


def test_image_workflow_is_not_called_by_deploy():
    # deploy hosts build the image themselves; building it here too only slows
    # every deploy down.
    assert "workflow_call" not in IMAGE_YML.read_text()
    assert "image.yml" not in DEPLOY_YML.read_text()


def test_image_build_has_no_layer_cache():
    build = _docker_build()
    for flag in ("--no-cache", "--pull", "--platform linux/amd64"):
        assert flag in build, flag
    text = IMAGE_YML.read_text()
    for cache in ("cache-from", "cache-to", "type=gha", "actions/cache", "setup-buildx"):
        assert cache not in text, cache


def test_image_smoke_runs_the_built_image_with_an_unreachable_bee():
    tag = re.search(r"-t\s+(\S+)", _docker_build()).group(1)
    smoke = _smoke()
    run = smoke[smoke.index("docker run"):].split("\n")[:2]
    run = " ".join(run)
    assert run.rstrip().endswith(tag), run
    assert "--network none" in run
    assert re.search(r"-e SWARM_BEE_API_URL=http://localhost:\d+", run)


def test_image_smoke_checks_root_with_curl_fail_and_a_timeout():
    smoke = _smoke()
    probe = re.search(r"docker exec \S+ curl (-\S+(?: \S+)*) http://localhost:8000/\s*;", smoke)
    assert probe, "the smoke check does not curl GET / inside the container"
    assert re.search(r"-\w*f", probe.group(1)), "curl without -f passes on a 5xx"
    assert "--max-time" in probe.group(1)
    # A dead container ends the wait, and its log is shown before failing.
    assert "{{.State.Running}}" in smoke
    assert "docker logs" in smoke
    assert smoke.rstrip().endswith("exit 1")


def test_image_runs_the_x402_config_check_in_the_running_container():
    """The issue's verification step: python -m app.x402.check_config inside the
    built image, after GET / answered, and before the container is removed."""
    blocks = _run_blocks(IMAGE_YML.read_text())
    checks = [i for i, b in enumerate(blocks)
              if re.fullmatch(r'docker exec "?\$CONTAINER"? python -m app\.x402\.check_config', b)]
    assert len(checks) == 1, f"expected one x402 config check in image.yml, found {checks}"
    smoke = next(i for i, b in enumerate(blocks) if "docker run" in b)
    remove = next(i for i, b in enumerate(blocks) if b.startswith("docker rm -f "))
    assert smoke < checks[0] < remove
    swallow = re.compile(r"\|\|\s*(true|:|exit 0)\b|;\s*(true|:|exit 0)\s*$")
    assert not swallow.search(blocks[checks[0]])
    # Its step is not allowed to fail quietly.
    assert "continue-on-error" not in IMAGE_YML.read_text()


def test_image_smoke_always_removes_the_container():
    text = IMAGE_YML.read_text()
    assert re.search(r"if:\s*always\(\)\s*\n\s*run:\s*docker rm -f ", text)


# --- one Python version everywhere, and a base image pinned by digest (#617)

FROM_LINE = re.compile(r"^FROM\s+(\S+)", re.M)


def _from_images():
    return FROM_LINE.findall(DOCKERFILE.read_text())


def _dockerfile_python():
    images = _from_images()
    assert len(images) == 1, f"expected one FROM in the Dockerfile, found {images}"
    m = re.fullmatch(r"python:(\d+\.\d+)(?:\.\d+)?-slim(?:-\w+)?@sha256:[0-9a-f]{64}", images[0])
    assert m, f"base image is not python:X.Y-slim pinned by digest: {images[0]}"
    return m.group(1)


def test_dockerfile_base_image_is_pinned_by_digest():
    """Without the digest each deploy host builds from whatever base image it
    has cached, so hosts can differ in patch level."""
    images = _from_images()
    assert len(images) == 1, images
    assert re.search(r"@sha256:[0-9a-f]{64}$", images[0]), images[0]
    _dockerfile_python()


def test_python_version_is_the_same_in_ci_the_image_and_the_lock():
    """CI must test the interpreter the image runs, and the lock must be
    resolved for it: a lock compiled for another minor version can pin
    different packages (the lock's 3.10 and 3.11+ branches differed in eight)."""
    image = _dockerfile_python()
    ci = re.findall(r"^\s*python-version:\s*[\"']?([0-9.]+)[\"']?\s*$", TESTS_YML.read_text(), re.M)
    assert ci == [image], f"tests.yml python-version {ci}, Dockerfile {image}"
    header = re.findall(r"--python-version (\S+)", "".join(LOCK.read_text().splitlines(True)[:3]))
    assert header == [image], f"requirements.lock header {header}, Dockerfile {image}"
    command = re.findall(r"uv pip compile .*--python-version (\S+)", REQUIREMENTS_TXT.read_text())
    assert command == [image], f"requirements.txt regenerate command {command}, Dockerfile {image}"


# Lowest versions without the advisories pip-audit reported on the lock (#525).
# x402 1.0.0 is pinned on purpose (2.x changes the API), so it is not listed.
LOCK_MINIMUMS = {
    "aiohttp": "3.14.4",
    "certifi": "2026.10.18",
    "dnspython": "2.9.0",
    "werkzeug": "3.1.9",
}


def _lock_pins():
    """Every `name==version` pin in the lock, by normalised name. A universal
    lock can pin one package more than once, under different markers."""
    pins = {}
    for m in re.finditer(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)", LOCK.read_text(), re.M):
        name = re.sub(r"[-_.]+", "-", m.group(1)).lower()
        pins.setdefault(name, []).append(m.group(2))
    return pins


def test_lock_pins_are_at_or_above_the_advisory_fixes():
    """A regeneration must not take these back below the fixed versions."""
    from packaging.version import Version

    pins = _lock_pins()
    for name, minimum in LOCK_MINIMUMS.items():
        assert name in pins, f"{name} is no longer in requirements.lock; update LOCK_MINIMUMS"
        for version in pins[name]:
            assert Version(version) >= Version(minimum), (
                f"requirements.lock pins {name}=={version}, below {minimum} (advisory fix, #525)"
            )
