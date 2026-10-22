"""#494: scripts/backup_bee_keys.sh, the Bee keystore backup.

Hermetic: the script runs against PATH shims for docker, age, rsync/scp and
sha256sum, so no Docker, network or real key is involved. A fake "volume" is a
directory holding keys/; a fake "remote" is a local directory. The real
end-to-end drill is tests/test_bee_keys_drill.py (opt-in, RUN_BEE_DRILL=1).
"""
import hashlib
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "backup_bee_keys.sh"
RESTORE = REPO / "scripts" / "restore_bee_keys.sh"

# Marker that appears in every fake keystore file. It must never appear in any
# file the script leaves behind: that is the "no plaintext on disk" check.
PLAIN = "PLAINTEXT-KEYSTORE-MARKER"
RECIPIENT = "age1" + "q" * 58

# Real tools the script needs, linked into an otherwise empty PATH so that the
# presence of rsync (and of docker/age) is fully controlled by each test.
TOOLS = ["bash", "sh", "date", "mkdir", "mv", "rm", "cmp", "ls", "sort", "cut",
         "touch", "mktemp", "cat", "tail", "cp", "env", "tr", "sed", "head", "dirname",
         "basename", "seq", "awk", "tar", "chown", "chmod", "id", "kill", "rmdir", "find",
         "grep", "ln", "readlink", "xargs", "printf", "wc", "stat", "sleep"]

# Fake docker. `volume inspect/create` work on directories under FAKE_VOLUMES.
# `run` executes the container's command LOCALLY with every mount point (and
# /dev/shm, the container's tmpfs) rewritten to a directory, so the real shell
# scripts are exercised with real find/sort/tar and the sha256sum shim. The
# /dev/shm stand-in is removed afterwards, as `--rm` would.
DOCKER = r'''#!PY
import os, shutil, subprocess, sys, tempfile
args = sys.argv[1:]
with open(os.environ["SHIM_LOG"], "a") as f:
    f.write("docker " + " ".join(args) + "\n")
root = os.environ["FAKE_VOLUMES"]
if args[:2] == ["volume", "inspect"]:
    sys.exit(0 if os.path.isdir(os.path.join(root, args[2])) else 1)
if args[:2] == ["volume", "create"]:
    os.makedirs(os.path.join(root, args[2]), exist_ok=True)
    sys.exit(0)
if args[0] != "run":
    sys.exit(99)
if "--entrypoint" in args and "id" in args:
    print(os.getuid() if args[-1] == "-u" else os.getgid())
    sys.exit(0)
env = dict(os.environ)
mounts, i = {}, 1
while i < len(args):
    a = args[i]
    if a == "-v":
        src, dst = args[i + 1].split(":")[:2]
        mounts[dst] = src if src.startswith("/") else os.path.join(root, src)
        i += 2
    elif a == "-e":
        k, v = args[i + 1].split("=", 1)
        env[k] = v
        i += 2
    elif a in ("--log-driver", "--name", "--network", "--entrypoint"):
        i += 2
    elif a.startswith("-"):
        i += 1
    else:
        break
cmd = args[i + 1:]
if os.environ.get("FAKE_DOCKER_KILL") and "-i" in args:
    # The container dies mid-extraction (OOM kill, daemon restart): a partial
    # keys/ stays on the volume and docker exits 137.
    os.makedirs(os.path.join(mounts["/dst"], "keys"), exist_ok=True)
    open(os.path.join(mounts["/dst"], "keys", "swarm.key"), "wb").write(b"partial")
    sys.exit(137)
if os.environ.get("FAKE_DOCKER_TAR_FAIL") and cmd and cmd[-1] == "tar":
    sys.exit(1)
shm = tempfile.mkdtemp(dir=os.environ["FAKE_SHM_ROOT"])
mounts["/dev/shm"] = shm
def rewrite(a):
    for dst in sorted(mounts, key=len, reverse=True):
        a = a.replace(dst, mounts[dst])
    return a
try:
    rc = subprocess.run([rewrite(a) for a in cmd], env=env).returncode
finally:
    shutil.rmtree(shm, ignore_errors=True)
sys.exit(rc)
'''

# Fake age: "encrypts" by reversing and hex-encoding, so the marker is not
# visible in the output. With FAKE_AGE_FAIL it writes part of the output first,
# as a real age would before dying.
AGE = r'''#!PY
import os, sys
args = sys.argv[1:]
with open(os.environ["SHIM_LOG"], "a") as f:
    f.write("age " + " ".join(args) + "\n")
out = args[args.index("-o") + 1]
data = sys.stdin.buffer.read()
with open(out, "wb") as f:
    f.write(b"FAKE-AGE\n" + data[::-1].hex().encode()[: (64 if os.environ.get("FAKE_AGE_FAIL") else None)])
sys.exit(1 if os.environ.get("FAKE_AGE_FAIL") else 0)
'''

# Fake rsync/scp: copy every source into the destination directory. Sources and
# destination are local paths here. FAKE_COPY_FAIL makes the copy fail.
COPY = r'''#!PY
import os, shutil, sys
name = os.path.basename(sys.argv[0])
args, it = [], iter(sys.argv[1:])
for a in it:
    if a in ("-e", "-o"):
        next(it)
    elif not a.startswith("-"):
        args.append(a)
with open(os.environ["SHIM_LOG"], "a") as f:
    f.write(name + " " + " ".join(sys.argv[1:]) + "\n")
if os.environ.get("FAKE_COPY_FAIL"):
    sys.exit(23)
*srcs, dest = args
for s in srcs:
    if not os.path.exists(s):
        sys.exit(23)
    shutil.copy(s, dest)
'''

# util-linux `flock -n <fd>` for macOS: locks the open file description the
# shell passed as <fd>, so the lock lasts until the shell closes it, as with the
# real tool. Only the form the script uses is supported.
FLOCK = r'''#!PY
import fcntl, sys
args = sys.argv[1:]
assert args[0] == "-n" and args[1].isdigit(), args
try:
    fcntl.flock(int(args[1]), fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(1)
'''

SHA = r'''#!PY
import hashlib, sys
for p in sys.argv[1:]:
    if p == "--":
        continue
    print(hashlib.sha256(open(p, "rb").read()).hexdigest() + "  " + p)
'''


class Env:
    def __init__(self, tmp: Path, rsync=True, flock=True):
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.bin.mkdir()
        for t in TOOLS:
            real = shutil.which(t)
            if real:
                (self.bin / t).symlink_to(real)
        shims = {"docker": DOCKER, "age": AGE, "sha256sum": SHA, "scp": COPY}
        if flock and not shutil.which("flock"):
            shims["flock"] = FLOCK
        elif flock:
            (self.bin / "flock").symlink_to(shutil.which("flock"))
        if rsync:
            shims["rsync"] = COPY
        for name, body in shims.items():
            p = self.bin / name
            p.write_text(body.replace("#!PY", "#!" + sys.executable, 1))
            p.chmod(p.stat().st_mode | stat.S_IXUSR)
        self.volumes = tmp / "volumes"
        self.local = tmp / "local"
        self.remote = tmp / "remote"
        self.remote.mkdir()
        self.tmpdir = tmp / "tmp"
        self.tmpdir.mkdir()
        self.shm = tmp / "shm"
        self.shm.mkdir()
        self.log = tmp / "shim.log"
        self.log.write_text("")
        self.add_volume("vol-prod")
        self.add_volume("vol-dev")
        self.env = {
            "PATH": str(self.bin),
            "HOME": str(tmp),
            "TMPDIR": str(self.tmpdir),
            "SHIM_LOG": str(self.log),
            "FAKE_VOLUMES": str(self.volumes),
            "FAKE_SHM_ROOT": str(self.shm),
            "BEE_KEYS_NODES": "prod:vol-prod dev:vol-dev",
            "BEE_KEYS_DIR": str(self.local),
            "BEE_KEYS_REMOTE": str(self.remote),
            "BEE_KEYS_AGE_RECIPIENT": RECIPIENT,
            "BACKUP_DIR": str(tmp / "state-local"),
            "BACKUP_REMOTE": str(tmp / "state-remote"),
        }

    def add_volume(self, name):
        keys = self.volumes / name / "keys"
        keys.mkdir(parents=True)
        for k in ("swarm.key", "libp2p_v2.key", "pss.key"):
            (keys / k).write_text(f'{{"crypto": "{PLAIN} {name} {k}"}}')

    def run(self, *args, **extra):
        env = dict(self.env)
        env.update(extra)
        env = {k: v for k, v in env.items() if v is not None}
        return subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True,
                              text=True, timeout=60)

    def calls(self, tool):
        return [l for l in self.log.read_text().splitlines() if l.startswith(tool + " ")]

    def assert_no_plaintext(self):
        for base in (self.local, self.remote, self.tmpdir, self.shm):
            if not base.exists():
                continue
            for p in base.rglob("*"):
                if p.is_file():
                    assert PLAIN not in p.read_text(errors="replace"), f"plaintext keystore in {p}"

    def archives(self, where):
        return sorted(p.name for p in where.glob("*.tar.age"))


@pytest.fixture
def e(tmp_path):
    return Env(tmp_path)


def test_success_encrypts_each_node_and_copies_it_off_host(e):
    r = e.run()
    assert r.returncode == 0, r.stderr
    assert "FAILED" not in r.stderr
    assert len(e.archives(e.remote)) == 2
    assert {n.split("-")[2] for n in e.archives(e.remote)} == {"prod", "dev"}
    assert len(list(e.remote.glob("*.sha256"))) == 2
    for a in e.remote.glob("*.tar.age"):
        assert a.read_bytes().startswith(b"FAKE-AGE")
    assert (e.local / ".last_success").exists()
    e.assert_no_plaintext()
    assert list(e.tmpdir.iterdir()) == [], "work dir left behind"
    assert not list(e.local.glob("*.part"))


def test_manifest_holds_plaintext_checksums(e):
    assert e.run().returncode == 0
    man = (e.local / "prod.manifest").read_text()
    want = hashlib.sha256((e.volumes / "vol-prod/keys/swarm.key").read_bytes()).hexdigest()
    assert f"{want}  swarm.key" in man
    remote_man = next(e.remote.glob("bee-keys-prod-*.sha256")).read_text()
    assert remote_man == man


def test_volume_is_read_live_and_read_only(e):
    assert e.run().returncode == 0
    runs = [c for c in e.calls("docker") if c.startswith("docker run")]
    assert runs
    for c in runs:
        assert ":/src:ro" in c
        assert "stop" not in c.split()
    assert not [c for c in e.calls("docker") if " stop " in c or " pause " in c]


def test_recipient_is_passed_to_age(e):
    assert e.run().returncode == 0
    assert all(f"-r {RECIPIENT}" in c for c in e.calls("age"))


def test_encryption_failure_leaves_no_plaintext_and_no_marker(e):
    r = e.run(FAKE_AGE_FAIL="1")
    assert r.returncode == 1
    assert "FAILED: prod: archiving or encrypting" in r.stderr
    assert "FAILED: dev: archiving or encrypting" in r.stderr
    assert not (e.local / ".last_success").exists()
    assert not list(e.local.glob("*.part"))
    assert e.archives(e.local) == [] and e.archives(e.remote) == []
    assert not (e.local / "prod.manifest").exists()
    assert e.calls("rsync") == []
    e.assert_no_plaintext()
    assert list(e.tmpdir.iterdir()) == []


def test_docker_read_failure_is_a_failure(e):
    r = e.run(FAKE_DOCKER_TAR_FAIL="1")
    assert r.returncode == 1
    assert "FAILED: prod: archiving or encrypting" in r.stderr
    assert not list(e.local.glob("*.part"))
    assert not (e.local / ".last_success").exists()


def test_remote_copy_failure_is_reported_and_retried_next_run(e):
    r = e.run(FAKE_COPY_FAIL="1")
    assert r.returncode == 1
    assert "FAILED: off-host copy of" in r.stderr
    assert not (e.local / ".last_success").exists()
    assert not (e.local / "prod.manifest").exists(), "record must not claim an upload that failed"
    e.assert_no_plaintext()
    r = e.run()
    assert r.returncode == 0, r.stderr
    assert len(e.archives(e.remote)) == 2


def test_scp_is_used_without_rsync(tmp_path):
    e = Env(tmp_path, rsync=False)
    r = e.run()
    assert r.returncode == 0, r.stderr
    assert e.calls("scp") and not e.calls("rsync")
    assert len(e.archives(e.remote)) == 2


@pytest.mark.parametrize("keys_remote,state_remote", [
    ("backup@host:/backups/x", "backup@host:/backups/x"),
    ("backup@host:/backups/x/", "backup@host:/backups/x"),
    ("backup@host:/backups/x/keys", "backup@host:/backups/x"),
    ("backup@host:/backups", "backup@host:/backups/x"),
])
def test_refuses_a_destination_shared_with_gateway_state(e, keys_remote, state_remote):
    r = e.run(BEE_KEYS_REMOTE=keys_remote, BACKUP_REMOTE=state_remote)
    assert r.returncode == 2
    assert "REFUSED" in r.stderr
    assert e.calls("docker") == [] and e.calls("age") == []


def test_sibling_destination_is_accepted(e):
    r = e.run(BACKUP_REMOTE=str(e.remote) + "-state")
    assert r.returncode == 0, r.stderr


def test_refuses_local_dir_shared_with_gateway_state(e):
    r = e.run(BACKUP_DIR=str(e.local))
    assert r.returncode == 2 and "BEE_KEYS_DIR and BACKUP_DIR" in r.stderr
    assert e.calls("docker") == []


def test_warns_when_gateway_state_destination_is_unknown(e):
    r = e.run(BACKUP_REMOTE=None)
    assert r.returncode == 0
    assert "warning: BACKUP_REMOTE is not set" in r.stdout


@pytest.mark.parametrize("extra,msg", [
    ({"BEE_KEYS_REMOTE": None}, "BEE_KEYS_REMOTE is not set"),
    ({"BEE_KEYS_AGE_RECIPIENT": None}, "BEE_KEYS_AGE_RECIPIENT is not set"),
    ({"BEE_KEYS_AGE_RECIPIENT": "AGE-SECRET-KEY-1" + "Q" * 58}, "SECRET key"),
    ({"BEE_KEYS_AGE_RECIPIENT": "not-a-key"}, "not an age public recipient"),
    ({"BEE_KEYS_KEEP": "0"}, "BEE_KEYS_KEEP"),
    ({"BEE_KEYS_NODES": "prod"}, "not node:volume"),
])
def test_configuration_refusals(e, extra, msg):
    r = e.run(**extra)
    assert r.returncode == 2
    assert msg in r.stderr
    assert e.calls("docker") == []


def test_unknown_argument_is_refused(e):
    assert e.run("--bogus").returncode == 2


def test_unchanged_keys_upload_nothing(e):
    assert e.run().returncode == 0
    (e.local / ".last_success").unlink()
    before = e.calls("rsync")
    r = e.run()
    assert r.returncode == 0, r.stderr
    assert r.stdout.count("unchanged:") == 2
    assert e.calls("rsync") == before
    # The manifest and the archive come from one snapshot, so the run encrypts
    # and then discards the unchanged archive: nothing new is kept or sent.
    assert len(e.archives(e.local)) == 2
    assert not list(e.local.glob("*.part"))
    assert (e.local / ".last_success").exists()


def test_changed_key_is_uploaded_again(e):
    assert e.run().returncode == 0
    (e.volumes / "vol-prod/keys/swarm.key").write_text(f'{{"crypto": "{PLAIN} rotated"}}')
    r = e.run(BEE_KEYS_NODES="prod:vol-prod dev:vol-dev")
    assert r.returncode == 0, r.stderr
    assert "unchanged: dev" in r.stdout
    assert "encrypted prod" in r.stdout
    # Same-second stamps would collide, so count uploads rather than files.
    assert len(e.calls("rsync")) == 3
    e.assert_no_plaintext()


def test_local_retention_keeps_newest(e):
    for i in range(4):
        (e.local).mkdir(exist_ok=True)
        (e.local / f"bee-keys-prod-2020010{i}T000000Z.tar.age").write_text("old")
        (e.local / f"bee-keys-prod-2020010{i}T000000Z.sha256").write_text("old")
    assert e.run(BEE_KEYS_KEEP="2").returncode == 0
    kept = [n for n in e.archives(e.local) if n.startswith("bee-keys-prod-")]
    assert len(kept) == 2
    assert "bee-keys-prod-20200103T000000Z.tar.age" in kept
    assert not (e.local / "bee-keys-prod-20200100T000000Z.sha256").exists()


def test_missing_volume_fails_that_node_only(e):
    shutil.rmtree(e.volumes / "vol-dev")
    r = e.run()
    assert r.returncode == 1
    assert "FAILED: dev: volume vol-dev does not exist" in r.stderr
    assert [n.split("-")[2] for n in e.archives(e.remote)] == ["prod"]
    assert not (e.local / ".last_success").exists()


# --- weekly --verify ---------------------------------------------------------

def test_verify_passes_when_everything_matches(e):
    assert e.run().returncode == 0
    uploads = len(e.archives(e.remote))
    r = e.run("--verify")
    assert r.returncode == 0, r.stderr
    assert r.stdout.count("verified ") == 2
    assert (e.local / ".last_verify").exists()
    assert len(e.archives(e.remote)) == uploads
    assert len(e.calls("age")) == 2, "--verify must not encrypt or upload"
    assert list(e.tmpdir.iterdir()) == []


def test_verify_fails_on_live_keystore_drift(e):
    assert e.run().returncode == 0
    (e.volumes / "vol-prod/keys/pss.key").write_text("changed")
    r = e.run("--verify")
    assert r.returncode == 1
    assert "FAILED: prod: checksum mismatch, live keystores differ" in r.stderr
    assert "verified dev" in r.stdout
    assert not (e.local / ".last_verify").exists()


def test_verify_fails_on_tampered_remote_copy(e):
    assert e.run().returncode == 0
    victim = next(e.remote.glob("bee-keys-dev-*.tar.age"))
    victim.write_bytes(victim.read_bytes() + b"x")
    r = e.run("--verify")
    assert r.returncode == 1
    assert "FAILED: dev: checksum mismatch, remote" in r.stderr
    assert not (e.local / ".last_verify").exists()


def test_verify_fails_on_missing_remote_copy(e):
    assert e.run().returncode == 0
    next(e.remote.glob("bee-keys-prod-*.tar.age")).unlink()
    r = e.run("--verify")
    assert r.returncode == 1
    assert "FAILED: prod: could not fetch" in r.stderr


def test_verify_without_a_recorded_backup_fails(e):
    r = e.run("--verify")
    assert r.returncode == 1
    assert "no recorded backup" in r.stderr
    assert not (e.local / ".last_verify").exists()


def test_verify_with_missing_volume_fails(e):
    assert e.run().returncode == 0
    shutil.rmtree(e.volumes / "vol-prod")
    r = e.run("--verify")
    assert r.returncode == 1
    assert "FAILED: prod: volume vol-prod does not exist" in r.stderr


# --- restore_bee_keys.sh argument handling (the Docker path is the drill) -----

@pytest.mark.parametrize("args,msg", [
    (["check", "vol", "zz"], "64 hex"),
    (["check", "vol", "ab" * 31], "64 hex"),
    (["check", "vol", "ab" * 32], "BEE_PASSWORD"),
    (["restore", "vol", "/nonexistent/manifest"], "not found"),
    (["restore", "vol"], "manifest"),
    (["bogus", "vol"], ""),
])
def test_restore_refuses_bad_arguments_before_touching_docker(tmp_path, args, msg):
    e = Env(tmp_path)
    r = subprocess.run(["bash", str(RESTORE), *args], env=e.env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 2
    assert msg in r.stderr
    assert e.calls("docker") == []


def test_scripts_are_executable():
    for s in (SCRIPT, RESTORE, REPO / "scripts" / "bee_keys_drill.sh"):
        assert os.access(s, os.X_OK), s


# --- destination separation: normalised comparison (review round 1) ----------

@pytest.mark.parametrize("keys_remote,state_remote", [
    ("host:/b/x", "user@host:/b/x"),
    ("backup@HOST:/b/x", "host:/b/x/"),
    ("host:/b//x", "host:/b/x"),
    ("host:/b/./x", "host:/b/x"),
    ("host:/b/x/.", "host:/b/x"),
    ("user@host:/b/x/keys", "host:/b//x"),
    ("/srv/b/x", "/srv/b/./x/"),
])
def test_refuses_shared_destination_after_normalising(e, keys_remote, state_remote):
    r = e.run(BEE_KEYS_REMOTE=keys_remote, BACKUP_REMOTE=state_remote)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "same destination or nested" in r.stderr
    assert e.calls("docker") == []


@pytest.mark.parametrize("keys_remote", [
    "host:b/x",          # relative to the remote home: cannot be compared
    "host:",             # empty path
    "user@host:",
    "host::module",      # rsync daemon syntax
    "relative/dir",
    ":/b/x",             # empty host
    "host:/b/../x",
])
def test_refuses_relative_or_empty_keys_remote(e, keys_remote):
    r = e.run(BEE_KEYS_REMOTE=keys_remote)
    assert r.returncode == 2
    assert "BEE_KEYS_REMOTE must be an absolute path" in r.stderr
    assert e.calls("docker") == []


def test_refuses_relative_gateway_state_remote(e):
    r = e.run(BEE_KEYS_REMOTE="host:/home/backup/b/x", BACKUP_REMOTE="host:b/x")
    assert r.returncode == 2
    assert "BACKUP_REMOTE" in r.stderr and "absolute" in r.stderr


def test_different_hosts_are_separate(e):
    r = e.run(BACKUP_REMOTE="otherhost:" + str(e.remote))
    assert r.returncode == 0, r.stderr


def test_same_path_on_another_host_than_local_is_separate(e):
    # A local BEE_KEYS_REMOTE and a remote BACKUP_REMOTE with the same path
    # are different destinations.
    r = e.run(BACKUP_REMOTE="host:" + str(e.remote))
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("nodes", ["a/b:vol-prod", "pr od:vol-prod", ":vol-prod", "prod:", "pröd:vol-prod"])
def test_refuses_bad_node_names(e, nodes):
    r = e.run(BEE_KEYS_NODES=nodes)
    assert r.returncode == 2
    assert e.calls("docker") == []


@pytest.mark.parametrize("other", ["prod-x", "prod-2", "prod-20200101"])
def test_prune_does_not_touch_a_node_sharing_the_prefix(e, other):
    e.local.mkdir()
    for i in range(3):
        (e.local / f"bee-keys-{other}-2020010{i}T000000Z.tar.age").write_text("other node")
        (e.local / f"bee-keys-{other}-2020010{i}T000000Z.sha256").write_text("other node")
    r = e.run(BEE_KEYS_NODES="prod:vol-prod", BEE_KEYS_KEEP="1")
    assert r.returncode == 0, r.stderr
    assert len(list(e.local.glob(f"bee-keys-{other}-*.tar.age"))) == 3
    assert len(list(e.local.glob(f"bee-keys-{other}-*.sha256"))) == 3


def test_ssh_connect_timeout_is_set_for_rsync_and_scp(tmp_path):
    e = Env(tmp_path)
    assert e.run().returncode == 0
    assert all("ConnectTimeout=" in c for c in e.calls("rsync"))
    sub = tmp_path / "scp"
    sub.mkdir()
    e2 = Env(sub, rsync=False)
    assert e2.run().returncode == 0
    assert e2.calls("scp") and all("ConnectTimeout=" in c for c in e2.calls("scp"))


def hold_lock(e):
    import fcntl
    e.local.mkdir(exist_ok=True)
    f = open(e.local / ".lock", "a")
    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return f


def test_a_concurrent_run_is_refused_and_leaves_the_other_runs_files(e):
    held = hold_lock(e)                               # another run holds the lock
    (e.local / "bee-keys-prod-x.tar.age.part").write_text("in flight")
    r = e.run()
    held.close()
    assert r.returncode == 1
    assert "FAILED: another run" in r.stderr
    assert (e.local / "bee-keys-prod-x.tar.age.part").exists()
    assert e.calls("docker") == []
    assert not (e.local / ".last_success").exists()


def test_a_leftover_unheld_lock_file_does_not_block(e):
    e.local.mkdir()
    (e.local / ".lock").write_text("whatever an earlier run left")
    r = e.run()
    assert r.returncode == 0, r.stderr


def test_lock_is_released_after_a_failed_run(e):
    assert e.run(FAKE_COPY_FAIL="1").returncode == 1
    held = hold_lock(e)                               # would raise if still held
    held.close()


def test_lock_is_released_when_the_run_is_killed(e):
    import signal
    import time
    e.local.mkdir()
    (e.bin / "age").unlink()
    (e.bin / "age").write_text("#!/bin/sh\nsleep 30\n")   # hang inside the run
    (e.bin / "age").chmod(0o755)
    env = dict(e.env)
    p = subprocess.Popen(["bash", str(SCRIPT)], env=env, start_new_session=True,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.time() + 20
    while not e.calls("docker") and time.time() < deadline:
        time.sleep(0.1)
    os.killpg(p.pid, signal.SIGKILL)                   # no trap runs on SIGKILL
    p.wait(timeout=30)
    held = hold_lock(e)                                # the kernel released it
    held.close()


def test_refuses_to_run_without_flock(tmp_path):
    e = Env(tmp_path, flock=False)
    r = e.run()
    assert r.returncode == 2
    assert "flock" in r.stderr
    assert e.calls("docker") == []


def test_failed_mv_is_a_failure(e):
    real = shutil.which("mv")
    (e.bin / "mv").unlink()
    (e.bin / "mv").write_text(f'#!/bin/sh\n[ -n "${{FAKE_MV_FAIL:-}}" ] && exit 1\nexec {real} "$@"\n')
    (e.bin / "mv").chmod(0o755)
    r = e.run(FAKE_MV_FAIL="1")
    assert r.returncode == 1
    assert "FAILED: prod" in r.stderr
    assert e.calls("rsync") == []
    assert not (e.local / ".last_success").exists()
    assert not list(e.local.glob("*.part"))


# --- restore_bee_keys.sh restore, against the docker shim (review round 1) ----

KEYS = {"swarm.key": b'{"crypto": "a"}', "libp2p_v2.key": b'{"crypto": "b"}', "pss.key": b'{"crypto": "c"}'}


def make_tar(files, extra_member=None):
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        d = tarfile.TarInfo("keys")
        d.type = tarfile.DIRTYPE
        d.mode = 0o755
        t.addfile(d)
        for n, data in files.items():
            ti = tarfile.TarInfo(f"keys/{n}")
            ti.size = len(data)
            ti.mode = 0o644          # world-readable in the archive on purpose
            t.addfile(ti, io.BytesIO(data))
        if extra_member:
            ti = tarfile.TarInfo(extra_member)
            ti.size = 1
            t.addfile(ti, io.BytesIO(b"x"))
    return buf.getvalue()


def make_manifest(path, files):
    lines = sorted(f"{hashlib.sha256(d).hexdigest()}  {n}" for n, d in files.items())
    path.write_text("\n".join(lines) + "\n")
    return path


def restore(e, vol, data, manifest, **extra):
    args = ["bash", str(RESTORE), "restore", vol] + ([str(manifest)] if manifest else [])
    return subprocess.run(args, env=dict(e.env, **extra), input=data, capture_output=True, timeout=60)


def test_restore_writes_keys_owner_only(e):
    m = make_manifest(e.tmp / "m.sha256", KEYS)
    r = restore(e, "vol-r", make_tar(KEYS), m)
    assert r.returncode == 0, r.stderr
    keys = e.volumes / "vol-r" / "keys"
    assert sorted(p.name for p in keys.iterdir()) == sorted(KEYS)
    assert stat.S_IMODE(keys.stat().st_mode) == 0o700
    for p in keys.iterdir():
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
        assert p.stat().st_uid == os.getuid()
    assert (keys / "swarm.key").read_bytes() == KEYS["swarm.key"]


def test_restore_ignores_members_outside_keys(e):
    m = make_manifest(e.tmp / "m.sha256", KEYS)
    r = restore(e, "vol-r", make_tar(KEYS, extra_member="statestore/x"), m)
    assert r.returncode == 0, r.stderr
    assert not (e.volumes / "vol-r" / "statestore").exists()


def test_restore_refuses_a_non_empty_volume(e):
    stale = e.volumes / "vol-r" / "statestore"
    stale.mkdir(parents=True)
    m = make_manifest(e.tmp / "m.sha256", KEYS)
    r = restore(e, "vol-r", make_tar(KEYS), m)
    assert r.returncode == 2
    assert b"not empty" in r.stderr
    assert not (e.volumes / "vol-r" / "keys").exists()


def test_restore_requires_a_manifest(e):
    r = restore(e, "vol-r", make_tar(KEYS), None)
    assert r.returncode == 2
    assert b"manifest" in r.stderr
    assert e.calls("docker") == []


def test_restore_removes_keys_on_checksum_mismatch(e):
    bad = dict(KEYS, **{"pss.key": b"other"})
    m = make_manifest(e.tmp / "m.sha256", bad)
    r = restore(e, "vol-r", make_tar(KEYS), m)
    assert r.returncode == 1
    assert b"do not match" in r.stderr
    assert not (e.volumes / "vol-r" / "keys").exists()


def test_restore_refuses_an_extra_file(e):
    m = make_manifest(e.tmp / "m.sha256", KEYS)
    r = restore(e, "vol-r", make_tar(dict(KEYS, **{"extra.key": b"x"})), m)
    assert r.returncode == 1
    assert b"do not match" in r.stderr
    assert not (e.volumes / "vol-r" / "keys").exists()


def test_restore_refuses_a_missing_file(e):
    m = make_manifest(e.tmp / "m.sha256", KEYS)
    partial = {k: v for k, v in KEYS.items() if k != "pss.key"}
    r = restore(e, "vol-r", make_tar(partial), m)
    assert r.returncode == 1
    assert not (e.volumes / "vol-r" / "keys").exists()


def test_truncated_input_leaves_nothing_and_can_be_retried(e):
    m = make_manifest(e.tmp / "m.sha256", KEYS)
    data = make_tar(KEYS)
    r = restore(e, "vol-r", data[:700], m)
    assert r.returncode == 1
    assert not (e.volumes / "vol-r" / "keys").exists()
    r = restore(e, "vol-r", data, m)
    assert r.returncode == 0, r.stderr


def test_empty_input_fails_cleanly(e):
    m = make_manifest(e.tmp / "m.sha256", KEYS)
    r = restore(e, "vol-r", b"", m)
    assert r.returncode == 1
    assert not (e.volumes / "vol-r" / "keys").exists()


# --- review round 2 ----------------------------------------------------------

def decode_fake_age(path):
    data = path.read_bytes()
    assert data.startswith(b"FAKE-AGE\n")
    return bytes.fromhex(data.split(b"\n", 1)[1].decode())[::-1]


def tar_members(data):
    import io
    import tarfile
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        return {m.name: t.extractfile(m).read() for m in t.getmembers() if m.isfile()}


def test_one_container_run_per_node_gives_archive_and_manifest(e):
    assert e.run().returncode == 0
    runs = [c for c in e.calls("docker") if c.startswith("docker run")]
    assert len(runs) == 2, runs
    for node in ("prod", "dev"):
        archive = next(e.remote.glob(f"bee-keys-{node}-*.tar.age"))
        files = tar_members(decode_fake_age(archive))
        want = [f"{hashlib.sha256(files[n]).hexdigest()}  {n[len('keys/'):]}" for n in sorted(files)]
        manifest = archive.with_name(archive.name.replace(".tar.age", ".sha256")).read_text().splitlines()
        assert manifest == want


def test_dotfiles_are_in_the_manifest_and_survive_a_round_trip(e):
    (e.volumes / "vol-prod/keys/.hidden").write_text("dot")
    assert e.run(BEE_KEYS_NODES="prod:vol-prod").returncode == 0
    archive = next(e.remote.glob("bee-keys-prod-*.tar.age"))
    manifest = archive.with_name(archive.name.replace(".tar.age", ".sha256"))
    assert "  .hidden" in manifest.read_text()
    r = restore(e, "vol-r", decode_fake_age(archive), manifest)
    assert r.returncode == 0, r.stderr
    hidden = e.volumes / "vol-r/keys/.hidden"
    assert hidden.read_text() == "dot"
    assert stat.S_IMODE(hidden.stat().st_mode) == 0o600


def test_restore_refuses_an_extra_dotfile(e):
    m = make_manifest(e.tmp / "m.sha256", KEYS)
    r = restore(e, "vol-r", make_tar(dict(KEYS, **{".extra": b"x"})), m)
    assert r.returncode == 1
    assert not (e.volumes / "vol-r" / "keys").exists()


def test_a_non_file_entry_in_keys_fails_the_node(e):
    (e.volumes / "vol-prod/keys/link").symlink_to("swarm.key")
    r = e.run()
    assert r.returncode == 1
    assert "FAILED: prod" in r.stderr
    assert "unchanged" not in r.stdout
    assert [n.split("-")[2] for n in e.archives(e.remote)] == ["dev"]
    e.assert_no_plaintext()


def test_empty_keys_dir_fails_the_node(e):
    for p in (e.volumes / "vol-dev/keys").iterdir():
        p.unlink()
    r = e.run()
    assert r.returncode == 1
    assert "FAILED: dev" in r.stderr


def test_a_new_destination_counts_as_a_change(e):
    assert e.run().returncode == 0
    other = e.tmp / "remote2"
    other.mkdir()
    r = e.run(BEE_KEYS_REMOTE=str(other) + "/")
    assert r.returncode == 0, r.stderr
    assert "unchanged" not in r.stdout
    assert len(e.archives(other)) == 2
    # and the same destination spelled differently is not a change
    r = e.run(BEE_KEYS_REMOTE=str(other) + "//")
    assert r.stdout.count("unchanged:") == 2


def test_verify_uses_the_destination_of_the_last_upload(e):
    assert e.run().returncode == 0
    other = e.tmp / "remote2"
    other.mkdir()
    r = e.run("--verify", BEE_KEYS_REMOTE=str(other))
    assert r.returncode == 1
    assert "destination" in r.stderr


def test_interrupted_restore_leaves_no_partial_keys(e):
    # tar implementations buffer differently (GNU tar reads whole 10240-byte
    # records before extracting; busybox and bsdtar do not). The first member
    # is large, and three full records are fed before stalling, so every tar
    # has created keys/ and started writing a file by then.
    import signal
    import time
    files = {"big.key": os.urandom(64 * 1024), **KEYS}
    m = make_manifest(e.tmp / "m.sha256", files)
    data = make_tar(files)
    assert len(data) > 4 * 10240
    p = subprocess.Popen(["bash", str(RESTORE), "restore", "vol-r", str(m)], env=e.env,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         start_new_session=True)
    p.stdin.write(data[:3 * 10240])
    p.stdin.flush()
    keys = e.volumes / "vol-r" / "keys"
    deadline = time.time() + 20
    while not (keys / "big.key").exists() and time.time() < deadline:
        time.sleep(0.1)
    assert (keys / "big.key").exists(), "restore never started writing"
    os.killpg(p.pid, signal.SIGINT)             # Ctrl-C reaches the whole group
    p.wait(timeout=30)
    assert p.returncode != 0
    assert not keys.exists(), "partial keys/ left behind"


# --- review round 3 ----------------------------------------------------------

def test_helper_runs_that_carry_keystore_bytes_have_no_container_log(e):
    assert e.run().returncode == 0
    assert e.run("--verify").returncode == 0
    m = make_manifest(e.tmp / "m.sha256", KEYS)
    assert restore(e, "vol-r", make_tar(KEYS), m).returncode == 0
    runs = [c.split() for c in e.calls("docker") if c.startswith("docker run")]
    sensitive = [a for a in runs if any(x.endswith(":/src:ro") for x in a) or "-i" in a]
    assert len(sensitive) >= 5          # 2 backup, 2 verify, 1 restore
    for argv in sensitive:
        i = argv.index("--log-driver")
        assert argv[i + 1] == "none", argv


def test_container_killed_mid_restore_is_cleaned_up(e):
    m = make_manifest(e.tmp / "m.sha256", KEYS)
    r = restore(e, "vol-r", make_tar(KEYS), m, FAKE_DOCKER_KILL="1")
    assert r.returncode == 1
    assert b"removed" in r.stderr
    assert not (e.volumes / "vol-r" / "keys").exists()


def test_a_new_recipient_counts_as_a_change(e):
    assert e.run().returncode == 0
    r = e.run(BEE_KEYS_AGE_RECIPIENT="age1" + "z" * 58)
    assert r.returncode == 0, r.stderr
    assert "unchanged" not in r.stdout
    assert all("age1" + "z" * 58 in c for c in e.calls("age")[-2:])
    # the same recipients in another order are not a change
    two = f"{RECIPIENT} age1{'z' * 58}"
    assert e.run(BEE_KEYS_AGE_RECIPIENT=two).returncode == 0
    r = e.run(BEE_KEYS_AGE_RECIPIENT=f"age1{'z' * 58} {RECIPIENT}")
    assert r.stdout.count("unchanged:") == 2


def test_failed_copies_do_not_pile_up_local_archives(e):
    e.local.mkdir()
    for i in range(5):
        (e.local / f"bee-keys-prod-2020010{i}T000000Z.tar.age").write_text("old")
        (e.local / f"bee-keys-prod-2020010{i}T000000Z.sha256").write_text("old")
    r = e.run(FAKE_COPY_FAIL="1", BEE_KEYS_KEEP="2", BEE_KEYS_NODES="prod:vol-prod")
    assert r.returncode == 1
    kept = e.archives(e.local)
    assert len(kept) == 2
    assert "bee-keys-prod-20200104T000000Z.tar.age" in kept
    assert any(n.startswith("bee-keys-prod-20") and not n.startswith("bee-keys-prod-2020") for n in kept), \
        "the archive just written must be kept"


def test_keep_one_never_removes_the_archive_just_written_on_failure(e):
    e.local.mkdir()
    (e.local / "bee-keys-prod-20991231T235959Z.tar.age").write_text("clock skew: newer than now")
    r = e.run(FAKE_COPY_FAIL="1", BEE_KEYS_KEEP="1", BEE_KEYS_NODES="prod:vol-prod")
    assert r.returncode == 1
    kept = e.archives(e.local)
    assert len([n for n in kept if not n.startswith("bee-keys-prod-2099")]) == 1
