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

The image carries runtime packages only: test-only packages are in
requirements-dev.txt and its lock, which CI installs next to the runtime lock,
and CI fails when a lock no longer matches its input (#430).
"""
import os
import re
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
TESTS_YML = REPO / ".github" / "workflows" / "tests.yml"
DEPLOY_YML = REPO / ".github" / "workflows" / "deploy.yml"
DOCKERFILE = REPO / "Dockerfile"
IMAGE_YML = REPO / ".github" / "workflows" / "image.yml"
LOCK = REPO / "requirements.lock"
REQUIREMENTS_TXT = REPO / "requirements.txt"
DEV_LOCK = REPO / "requirements-dev.lock"
REQUIREMENTS_DEV_TXT = REPO / "requirements-dev.txt"
DOCKERIGNORE = REPO / ".dockerignore"
LOCK_SYNC = REPO / "scripts" / "check_lock_sync.sh"
UV_PIN = REPO / ".github" / "uv-pin.txt"

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


def test_tests_workflow_pip_cache_is_keyed_on_both_locks():
    text = TESTS_YML.read_text()
    m = re.search(r"^(\s*)cache-dependency-path:\s*\|\s*\n((?:\1\s+\S.*\n)+)", text, re.M)
    assert m, "cache-dependency-path is not a block listing the locks"
    assert sorted(m.group(2).split()) == ["requirements-dev.lock", "requirements.lock"]


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


TLS_EXEC = 'docker exec -i "$CONTAINER" python - <<\'PY\''


def _tls_check_script():
    """The Python the TLS step feeds into the running container, indentation kept."""
    text = IMAGE_YML.read_text()
    m = re.search(r"^([ \t]*)" + re.escape(TLS_EXEC) + r"\n(.*?)^\1PY$", text, re.M | re.S)
    assert m, "image.yml has no TLS trust check fed to python in the container"
    return textwrap.dedent(m.group(2))


def test_image_checks_tls_trust_offline_in_the_running_container():
    """httpx2 trusts the OS CA bundle on Linux, not certifi (#595): the built
    image must have it and httpx2 must load it, and real handshakes with a local
    server must refuse an untrusted certificate and a wrong host name, all
    without any network."""
    blocks = _run_blocks(IMAGE_YML.read_text())
    tls = [i for i, b in enumerate(blocks) if b.startswith(TLS_EXEC)]
    assert len(tls) == 1, f"expected one TLS trust check in image.yml, found {tls}"
    smoke = next(i for i, b in enumerate(blocks) if "docker run" in b)
    remove = next(i for i, b in enumerate(blocks) if b.startswith("docker rm -f "))
    assert smoke < tls[0] < remove
    assert "continue-on-error" not in IMAGE_YML.read_text()
    # The step's own failure is not swallowed: it has no continue-on-error,
    # and its shell part (the run without the Python fed to the container)
    # neither ignores errors nor exits 0 on its own.
    steps = yaml.safe_load(IMAGE_YML.read_text())["jobs"]["docker-build"]["steps"]
    step = [s for s in steps if str(s.get("run", "")).startswith(TLS_EXEC)]
    assert len(step) == 1 and "continue-on-error" not in step[0], step
    shell = re.sub(r"<<'PY'\n.*?^PY$", "<<'PY'", step[0]["run"], flags=re.S | re.M)
    assert shell.strip() == TLS_EXEC, f"shell commands around the TLS check: {shell!r}"
    for swallow in (r"\|\|\s*(true|:)", r"\bset\s+\+e\b", r"\bexit\s+0\b"):
        assert not re.search(swallow, shell), swallow

    script = _tls_check_script()
    compile(script, "image.yml TLS check", "exec")
    # Offline: the container runs with --network none, and the check reaches
    # nothing but its own server on 127.0.0.1.
    assert re.findall(r"https?://[^\s\"'/{]*", script) == ["https://127.0.0.1:"]
    # The three handshakes: refused by default httpx2, accepted when the
    # certificate is trusted, refused for another host name.
    for handshake in (r"assert await refused\(default, url, HOST\) is not None",
                      r"assert await refused\(trusting, url, HOST\) is None",
                      r'assert wrong is not None and "mismatch" in wrong\.verify_message'):
        assert re.search(handshake, script), handshake
    # A failed check fails the step: the one except clause catches the refused
    # handshake it then examines.
    excepts = re.findall(r"^[ \t]*(except\b.*)$", script, re.M)
    assert excepts == ["except httpx2.ConnectError as e:"], excepts


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
    # One per job: pytest and lock-sync.
    assert ci == [image, image], f"tests.yml python-version {ci}, Dockerfile {image}"
    for lock in (LOCK, DEV_LOCK):
        header = re.findall(r"--python-version (\S+)", "".join(lock.read_text().splitlines(True)[:3]))
        assert header == [image], f"{lock.name} header {header}, Dockerfile {image}"
    command = re.findall(r"uv pip compile .*--python-version (\S+)", REQUIREMENTS_TXT.read_text())
    assert command == [image, image], f"requirements.txt regenerate commands {command}, Dockerfile {image}"
    command = re.findall(r"uv pip compile .*--python-version (\S+)", REQUIREMENTS_DEV_TXT.read_text())
    assert command == [image], f"requirements-dev.txt regenerate command {command}, Dockerfile {image}"


# Lowest versions without the advisories pip-audit reported on the lock (#525).
# x402 1.0.0 is pinned on purpose (2.x changes the API), so it is not listed.
LOCK_MINIMUMS = {
    "aiohttp": "3.14.4",
    "certifi": "2026.10.18",
    "dnspython": "2.9.0",
    "werkzeug": "3.1.9",
}


def _lock_pins(lock=LOCK):
    """Every `name==version` pin in a lock, by normalised name. A universal
    lock can pin one package more than once, under different markers."""
    pins = {}
    for m in re.finditer(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)", lock.read_text(), re.M):
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


# --- test-only packages stay out of the image (#430)

# Installed only for the tests. None of them may be a runtime requirement or be
# pinned in the runtime lock, which is all the image installs. (packaging is
# here because only pytest needs it today; if a runtime package starts to
# depend on it, take it off this list and off image.yml's check.)
TEST_ONLY = ("pytest", "pytest-asyncio", "pluggy", "iniconfig", "packaging")


def _requirement_names(path):
    names = set()
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", line)
        names.add(re.sub(r"[-_.]+", "-", m.group(0)).lower())
    return names


def _lock_blocks(lock):
    """{name: (version, sorted hashes)} for every package in a lock."""
    blocks = {}
    for block in re.split(r"\n(?=[A-Za-z0-9])", lock.read_text()):
        m = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)", block)
        if m:
            name = re.sub(r"[-_.]+", "-", m.group(1)).lower()
            blocks[name] = (m.group(2), sorted(re.findall(r"--hash=sha256:([0-9a-f]{64})", block)))
    return blocks


def _header_command(lock):
    lines = lock.read_text().splitlines()
    assert lines[0].startswith("# This file was autogenerated by uv"), lines[0]
    return re.sub(r"^#\s*", "", lines[1])


def test_runtime_requirements_and_lock_have_no_test_packages():
    """pytest and its plugins used to be in requirements.txt, so every image
    shipped them (#430)."""
    direct = _requirement_names(REQUIREMENTS_TXT)
    pinned = _lock_pins(LOCK)
    for name in TEST_ONLY:
        assert name not in direct, f"{name} is in requirements.txt; it belongs in requirements-dev.txt"
        assert name not in pinned, f"{name} is pinned in requirements.lock, which the image installs"


def test_dev_requirements_hold_the_test_packages():
    direct = _requirement_names(REQUIREMENTS_DEV_TXT)
    for name in ("pytest", "pytest-asyncio", "requests", "packaging"):
        assert name in direct, f"{name} (imported by the tests) is not in requirements-dev.txt"
    # The dev lock lists what the tests add; it does not pull in the runtime set.
    assert "-r requirements.txt" not in REQUIREMENTS_DEV_TXT.read_text()
    blocks = _lock_blocks(DEV_LOCK)
    for name in ("pytest", "pytest-asyncio", "pluggy", "iniconfig", "packaging", "requests"):
        assert name in blocks, f"{name} is not pinned in requirements-dev.lock"
        assert blocks[name][1], f"{name} has no hashes in requirements-dev.lock"


def test_dev_lock_agrees_with_the_runtime_lock():
    """CI installs both locks into one environment, so a package in both must
    have the same version and hashes; the dev lock is compiled against the
    runtime lock as a constraint to guarantee it."""
    runtime, dev = _lock_blocks(LOCK), _lock_blocks(DEV_LOCK)
    for name in sorted(set(runtime) & set(dev)):
        assert runtime[name] == dev[name], f"{name}: runtime {runtime[name][0]}, dev {dev[name][0]}"


def test_lock_headers_are_the_documented_commands():
    """Each lock was made by the command its input file documents, with
    hashes, for every platform, and the dev lock constrained by the runtime lock."""
    runtime = _header_command(LOCK)
    dev = _header_command(DEV_LOCK)
    assert runtime.startswith("uv pip compile requirements.txt ")
    assert dev.startswith("uv pip compile requirements-dev.txt -c requirements.lock ")
    for cmd, out in ((runtime, "requirements.lock"), (dev, "requirements-dev.lock")):
        for flag in ("--universal", "--generate-hashes", f"-o {out}"):
            assert f" {flag}" in f" {cmd} ", (flag, cmd)
    documented = re.findall(r"^#\s+(uv pip compile .*)$", REQUIREMENTS_TXT.read_text(), re.M)
    assert documented == [runtime, dev], documented
    documented = re.findall(r"^#\s+(uv pip compile .*)$", REQUIREMENTS_DEV_TXT.read_text(), re.M)
    assert documented == [dev], documented


def test_dockerfile_installs_only_the_runtime_lock():
    text = DOCKERFILE.read_text()
    install = _lock_install(text)
    assert re.findall(r"-r\s+(\S+)", install) == ["requirements.lock"], install
    copies = re.findall(r"^COPY\s+(.+)$", text, re.M)
    assert "requirements.lock ." in copies
    assert not [c for c in copies if "requirements-dev" in c], copies
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "requirements-dev" not in code
    assert "pytest" not in code
    # COPY . . must not bring the dev requirements into the image either.
    ignored = DOCKERIGNORE.read_text().splitlines()
    for name in ("requirements-dev.txt", "requirements-dev.lock", "tests/"):
        assert name in ignored, f"{name} is not in .dockerignore"


def test_tests_workflow_installs_the_runtime_and_dev_locks_together():
    install = _lock_install(TESTS_YML.read_text())
    assert re.findall(r"-r\s+(\S+)", install) == ["requirements.lock", "requirements-dev.lock"], install
    assert "--no-deps" in install and "--require-hashes" in install


def _job(text, name):
    m = re.search(rf"^  {re.escape(name)}:\n((?:(?:    .*)?\n)+)", text + "\n", re.M)
    assert m, f"tests.yml has no {name} job"
    return m.group(1)


def test_tests_workflow_checks_the_locks_are_in_sync():
    text = TESTS_YML.read_text()
    job = _job(text, "lock-sync")
    cmds = _run_lines(job)
    uv = [c for c in cmds if "uv-pin.txt" in c]
    assert uv == ["python -m pip install --no-deps --require-hashes -r .github/uv-pin.txt"], cmds
    assert not [c for c in cmds if re.search(r"pip install\b.*\buv==", c)], "uv installed without its hashes"
    assert "scripts/check_lock_sync.sh" in cmds, cmds
    assert cmds.index(uv[0]) < cmds.index("scripts/check_lock_sync.sh")
    assert re.search(r"^\s*if:\s*github\.event_name == 'pull_request'\s*$", job, re.M)
    assert "continue-on-error" not in job
    # The deploy gate (job pytest) does not depend on PyPI through it.
    assert "needs:" not in job


def test_uv_for_the_sync_check_is_pinned_by_version_and_hash():
    text = UV_PIN.read_text()
    code = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    assert re.fullmatch(r"uv==0\.11\.\d+ \\", code[0]), code[0]
    hashes = code[1:]
    assert hashes, "uv has no hashes"
    for i, line in enumerate(hashes):
        tail = "" if i == len(hashes) - 1 else " \\"
        assert re.fullmatch(r"    --hash=sha256:[0-9a-f]{64}" + re.escape(tail), line), line
    assert len(set(hashes)) == len(hashes)
    # The image never gets it: .github is not in the build context.
    assert ".github" in DOCKERIGNORE.read_text().splitlines()


def test_lock_sync_script_reruns_both_header_commands():
    assert LOCK_SYNC.stat().st_mode & stat.S_IXUSR, "scripts/check_lock_sync.sh is not executable"
    text = LOCK_SYNC.read_text()
    assert re.search(r"^LOCKS=\(requirements\.lock requirements-dev\.lock\)", text, re.M)
    assert re.search(r"^INPUTS=\(requirements\.txt requirements-dev\.txt\)", text, re.M)
    assert '"uv 0.11."*)' in text


# The script is exercised with a stand-in for uv, so these run offline. Like
# uv pip compile, the stand-in leaves an existing -o file alone (its pins and
# hashes are kept), unless told to change it, the way uv changes a lock whose
# input no longer matches it. A missing -o file (the fresh hash pass) it fills
# from the pristine locks in FAKE_UV_SOURCE, as uv would from the index, and
# FAKE_UV_FRESH_HASH adds a hash there, as when PyPI gains a file. It keeps a
# copy of the -c *.pins file it was given.
FAKE_UV = r"""#!/usr/bin/env bash
if [ "$1" = "--version" ]; then echo "uv ${FAKE_UV_VERSION:-0.11.17}"; exit 0; fi
out=""; pins=""; prev=""
for a in "$@"; do
  [ "$prev" = "-o" ] && out="$a"
  [ "$prev" = "-c" ] && [ "${a%.pins}" != "$a" ] && pins="$a"
  prev="$a"
done
echo "$PWD|$*|${UV_NO_CONFIG:-}|${UV_INDEX_URL:-}" >> "$FAKE_UV_LOG"
[ "${FAKE_UV_EXIT:-0}" = 0 ] || exit "$FAKE_UV_EXIT"
if [ -e "$out" ]; then
  [ "$out" = "${FAKE_UV_CHANGE:-}" ] && echo "extra==1.0" >> "$out"
else
  cp "$FAKE_UV_SOURCE/$(basename "$out")" "$out"
  if [ -n "${FAKE_UV_FRESH_HASH:-}" ]; then
    while IFS= read -r line; do
      printf '%s\n' "$line"
      case "$line" in "pytest=="*) printf '    --hash=sha256:%s \\\n' "$FAKE_UV_FRESH_HASH" ;; esac
    done < "$out" > "$out.new" && mv "$out.new" "$out"
  fi
  [ -n "$pins" ] && cp "$pins" "$FAKE_UV_LOG.$(basename "$out").pins"
fi
exit 0
"""


@pytest.fixture
def lock_sync(tmp_path):
    if not shutil.which("bash") or not shutil.which("diff"):
        pytest.skip("needs bash and diff")
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(LOCK_SYNC, repo / "scripts" / LOCK_SYNC.name)
    for f in (REQUIREMENTS_TXT, REQUIREMENTS_DEV_TXT, LOCK, DEV_LOCK):
        shutil.copy2(f, repo / f.name)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "uv").write_text(FAKE_UV)
    (bin_dir / "uv").chmod(0o755)
    log = tmp_path / "uv.log"

    def run(**env):
        full = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                "FAKE_UV_LOG": str(log), "FAKE_UV_SOURCE": str(REPO), **env}
        proc = subprocess.run(["bash", str(repo / "scripts" / LOCK_SYNC.name)], env=full,
                              capture_output=True, text=True, timeout=60)
        calls = log.read_text().splitlines() if log.exists() else []
        log.unlink(missing_ok=True)
        return proc, calls

    run.repo = repo
    run.pins = lambda lock: Path(f"{log}.{lock}.pins").read_text()
    return run


def _hash_lines(lock_text, package):
    """The --hash lines of one package's block in a lock."""
    m = re.search(rf"^{re.escape(package)}==[^\n]*\n((?:    --hash=\S+.*\n)+)", lock_text, re.M)
    assert m, package
    return m.group(1).splitlines()


def test_lock_sync_passes_and_leaves_the_tree_alone_when_in_sync(lock_sync):
    before = {p.name: p.read_bytes() for p in lock_sync.repo.iterdir() if p.is_file()}
    proc, calls = lock_sync(UV_INDEX_URL="https://example.invalid/simple")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert {p.name: p.read_bytes() for p in lock_sync.repo.iterdir() if p.is_file()} == before
    # Per lock: the header command on a copy, then the fresh hash pass.
    assert len(calls) == 4
    for (regen, fresh), lock in zip((calls[0:2], calls[2:4]), (LOCK, DEV_LOCK)):
        header = _header_command(lock)[len("uv "):]
        cwd, args, no_config, index = regen.split("|")
        # Run in a scratch copy, verbatim from the header, without user config.
        assert Path(cwd) != lock_sync.repo
        assert args == header + " --quiet"
        assert no_config == "1" and index == ""
        cwd, args, no_config, index = fresh.split("|")
        # The fresh pass writes a new file, constrained to the committed pins.
        assert Path(cwd) != lock_sync.repo
        assert args == header.replace(f"-o {lock.name}", f"-o out/{lock.name}") + f" -c {lock.name}.pins --quiet"
        assert no_config == "1" and index == ""
        pins = lock_sync.pins(lock.name).splitlines()
        assert pins == [line.rstrip(" \\") for line in lock.read_text().splitlines()
                        if re.match(r"[A-Za-z0-9].*==", line)]
        assert pins and not [p for p in pins if "--hash" in p or "\\" in p]


@pytest.mark.parametrize("lock", ["requirements.lock", "requirements-dev.lock"])
def test_lock_sync_fails_on_a_changed_hash(lock_sync, lock):
    """uv keeps an existing lock's hashes, so only the fresh pass can see this."""
    path = lock_sync.repo / lock
    text = path.read_text()
    first = _hash_lines(text, "requests")[0]
    path.write_text(text.replace(first, re.sub(r"sha256:[0-9a-f]{64}", "sha256:" + "0" * 64, first), 1))
    proc, _ = lock_sync()
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "sha256:" + "0" * 64 + " is not a file of this version" in proc.stdout
    assert f"{lock} lists hashes that differ" in proc.stdout
    assert "out of sync with its input" not in proc.stdout


def test_lock_sync_fails_on_a_dropped_hash(lock_sync):
    path = lock_sync.repo / "requirements-dev.lock"
    text = path.read_text()
    dropped = _hash_lines(text, "pytest")[0]
    path.write_text(text.replace(dropped + "\n", "", 1))
    proc, _ = lock_sync()
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "pytest==" in proc.stdout and "is missing from the lock" in proc.stdout


def test_lock_sync_fails_when_the_index_has_a_file_the_lock_lacks(lock_sync):
    """The known false positive: PyPI gains a file for a pinned version. The
    check fails, which is why CI runs it on pull requests only."""
    proc, _ = lock_sync(FAKE_UV_FRESH_HASH="f" * 64)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "sha256:" + "f" * 64 + " of this version is missing from the lock" in proc.stdout


@pytest.mark.parametrize("changed", ["requirements.lock", "requirements-dev.lock"])
def test_lock_sync_fails_when_a_lock_would_change(lock_sync, changed):
    proc, calls = lock_sync(FAKE_UV_CHANGE=changed)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "+extra==1.0" in proc.stdout
    assert f"{changed} is out of sync" in proc.stdout
    # A lock that must be regenerated gets no hash pass (its pins may no
    # longer satisfy the input); the other lock still gets both passes.
    assert not [c for c in calls if f"-o out/{changed} " in c]
    assert len(calls) == 3
    # The committed lock itself is not rewritten.
    assert "extra==1.0" not in (lock_sync.repo / changed).read_text()


def test_lock_sync_refuses_another_uv_version(lock_sync):
    proc, calls = lock_sync(FAKE_UV_VERSION="0.12.0")
    assert proc.returncode == 2 and "needs uv 0.11.x" in proc.stderr
    assert calls == []


def test_lock_sync_fails_when_the_compile_fails(lock_sync):
    proc, _ = lock_sync(FAKE_UV_EXIT="1")
    assert proc.returncode == 2, proc.stdout + proc.stderr


def test_lock_sync_refuses_a_lock_without_a_compile_header(lock_sync):
    lock = lock_sync.repo / "requirements.lock"
    lines = lock.read_text().splitlines(True)
    lines[1] = "#    pip-compile requirements.txt -o requirements.lock\n"
    lock.write_text("".join(lines))
    proc, calls = lock_sync()
    assert proc.returncode == 2 and "no usable compile command" in proc.stderr
    assert calls == []


# --- image.yml: the built image has no test packages (#430)

NO_TEST_PKGS_EXEC = 'docker exec -i "$CONTAINER" python - <<\'TESTPKGS\''


def _no_test_packages_script():
    text = IMAGE_YML.read_text()
    m = re.search(r"^([ \t]*)" + re.escape(NO_TEST_PKGS_EXEC) + r"\n(.*?)^\1TESTPKGS$", text, re.M | re.S)
    assert m, "image.yml has no check that test packages are not importable"
    return textwrap.dedent(m.group(2))


def test_image_checks_test_packages_are_not_importable():
    blocks = _run_blocks(IMAGE_YML.read_text())
    checks = [i for i, b in enumerate(blocks) if b.startswith(NO_TEST_PKGS_EXEC)]
    assert len(checks) == 1
    smoke = next(i for i, b in enumerate(blocks) if "docker run" in b)
    remove = next(i for i, b in enumerate(blocks) if b.startswith("docker rm -f "))
    assert smoke < checks[0] < remove
    assert not re.search(r"\|\|\s*(true|:|exit 0)\b", blocks[checks[0]])

    script = _no_test_packages_script()
    for name in ("pytest", "pytest_asyncio", "pluggy", "iniconfig", "packaging"):
        assert f'"{name}"' in script, name
    assert "except" not in script and not re.search(r"https?://", script)


def test_image_test_package_check_fails_where_pytest_is_importable():
    """Run against this interpreter, which has pytest: the check must fail."""
    proc = subprocess.run([sys.executable, "-c", _no_test_packages_script()],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode != 0
    assert "test-only packages are importable" in proc.stderr
