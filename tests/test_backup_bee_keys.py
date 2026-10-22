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
         "basename", "seq"]

DOCKER = r'''#!PY
import hashlib, io, os, sys, tarfile
args = sys.argv[1:]
with open(os.environ["SHIM_LOG"], "a") as f:
    f.write("docker " + " ".join(args) + "\n")
root = os.environ["FAKE_VOLUMES"]
if args[:2] == ["volume", "inspect"]:
    sys.exit(0 if os.path.isdir(os.path.join(root, args[2])) else 1)
if args[0] == "run":
    vol = next(a for a in args if a.endswith(":/src:ro")).split(":")[0]
    keys = os.path.join(root, vol, "keys")
    if "sha256sum" in " ".join(args):
        for n in sorted(os.listdir(keys)):
            h = hashlib.sha256(open(os.path.join(keys, n), "rb").read()).hexdigest()
            print(f"{h}  {n}")
        sys.exit(0)
    if "tar" in args:
        if os.environ.get("FAKE_DOCKER_TAR_FAIL"):
            sys.exit(1)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as t:
            t.add(keys, arcname="keys")
        sys.stdout.buffer.write(buf.getvalue())
        sys.exit(0)
sys.exit(99)
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
args = [a for a in sys.argv[1:] if not a.startswith("-")]
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

SHA = r'''#!PY
import hashlib, sys
for p in sys.argv[1:]:
    print(hashlib.sha256(open(p, "rb").read()).hexdigest() + "  " + p)
'''


class Env:
    def __init__(self, tmp: Path, rsync=True):
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.bin.mkdir()
        for t in TOOLS:
            real = shutil.which(t)
            if real:
                (self.bin / t).symlink_to(real)
        shims = {"docker": DOCKER, "age": AGE, "sha256sum": SHA, "scp": COPY}
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
        for base in (self.local, self.remote, self.tmpdir):
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
    assert len(e.calls("age")) == 2, "age must not run again"
    assert (e.local / ".last_success").exists()


def test_changed_key_is_uploaded_again(e):
    assert e.run().returncode == 0
    (e.volumes / "vol-prod/keys/swarm.key").write_text(f'{{"crypto": "{PLAIN} rotated"}}')
    r = e.run(BEE_KEYS_NODES="prod:vol-prod dev:vol-dev")
    assert r.returncode == 0, r.stderr
    assert "unchanged: dev" in r.stdout
    assert "encrypted prod" in r.stdout
    # Same-second stamps would collide, so count calls rather than files.
    assert len(e.calls("age")) == 3
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
