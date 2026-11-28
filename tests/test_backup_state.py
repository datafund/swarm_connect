"""#493: scripts/backup_state.sh, the gateway-state backup and its S3 copy.

Hermetic: the script runs against an otherwise empty PATH with stand-ins for
age, rclone, rsync/scp and sha256sum, and spy wrappers around tar and gzip, so
no network, object store or real key is involved. The fake rclone keeps
"objects" in a local directory and accepts only credentials that reach it
through its environment. The real end-to-end test against MinIO, age and
rclone is tests/test_backup_s3_minio.py (opt-in, RUN_S3_BACKUP_TEST=1).
"""
import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "backup_state.sh"

# Appears in every state file. It must never appear in anything uploaded to S3.
PLAIN = "PLAINTEXT-STATE-MARKER"
RECIPIENT = "age1" + "q" * 58
KEY_ID = "TESTKEYID0123456789"
SECRET = "FAKE-S3-SECRET-" + "s9" * 10  # a stand-in, built so no scanner takes it for a key
BUCKET = "test-bucket"
PREFIX = "gw/host-a"
PIN = "v1.75.2"

TOOLS = ["bash", "sh", "date", "mkdir", "mv", "rm", "ls", "sort", "cut", "touch",
         "mktemp", "cat", "tail", "cp", "env", "tr", "sed", "head", "dirname",
         "basename", "awk", "chmod", "id", "find", "grep", "wc", "du", "printf"]

# Every shim and spy calls this first: log the argv, and log ENVLEAK if any
# environment variable holds a credential, except the two rclone variables
# that are supposed to carry them.
PRELUDE = r'''#!PY
import os, sys
_name = os.path.basename(sys.argv[0])
with open(os.environ["SHIM_LOG"], "a") as _f:
    _f.write(_name + " " + " ".join(sys.argv[1:]) + "\n")
    _allowed = ({"RCLONE_CONFIG_S3BACKUP_ACCESS_KEY_ID", "RCLONE_CONFIG_S3BACKUP_SECRET_ACCESS_KEY"}
                if _name == "rclone" else set())
    for _k, _v in os.environ.items():
        if _k not in _allowed and not _k.startswith("LEAK_") and (os.environ["LEAK_SECRET"] in _v or os.environ["LEAK_KEY_ID"] in _v):
            _f.write("ENVLEAK " + _name + " " + _k + "\n")
'''

# Spy: log, then run the real tool with the same arguments.
SPY = PRELUDE + r'''
os.execv(os.environ["REAL_" + _name.upper()], [_name] + sys.argv[1:])
'''

# Fake age: "encrypts" by reversing and hex-encoding the input file (or stdin).
AGE = PRELUDE + r'''
args = sys.argv[1:]
out = args[args.index("-o") + 1]
rest, i = [], 0
while i < len(args):
    if args[i] in ("-r", "-o"):
        i += 2
    else:
        rest.append(args[i]); i += 1
data = open(rest[0], "rb").read() if rest else sys.stdin.buffer.read()
fail = os.environ.get("FAKE_AGE_FAIL")
with open(out, "wb") as f:
    f.write(b"FAKE-AGE\n" + data[::-1].hex().encode()[: (64 if fail else None)])
sys.exit(1 if fail else 0)
'''

# Fake rclone: version, copyto and lsf against FAKE_S3_ROOT/<bucket>/<key>.
RCLONE = PRELUDE + r'''
args = sys.argv[1:]
env = os.environ
if args and args[0] == "version":
    print("rclone " + env.get("FAKE_RCLONE_VERSION", "v1.75.2"))
    print("- os/type: fake")
    sys.exit(0)
pre = "RCLONE_CONFIG_S3BACKUP_"
assert env.get(pre + "TYPE") == "s3", "remote not defined in the environment"
assert env.get(pre + "NO_CHECK_BUCKET") == "true" and env.get(pre + "NO_HEAD") == "true"
assert open(env["RCLONE_CONFIG"]).read() == "", "rclone must not read a config file"
with open(env["SHIM_LOG"], "a") as f:
    f.write("rclone-endpoint " + env.get(pre + "ENDPOINT", "") + " region " + env.get(pre + "REGION", "") + "\n")
if env.get("FAKE_S3_UNREACHABLE"):
    print("ERROR : dial tcp 127.0.0.1:9: connect: connection refused", file=sys.stderr)
    sys.exit(1)
if env.get(pre + "ACCESS_KEY_ID") != env["LEAK_KEY_ID"] or env.get(pre + "SECRET_ACCESS_KEY") != env["LEAK_SECRET"]:
    print("ERROR : AccessDenied: Access Denied. status code: 403", file=sys.stderr)
    sys.exit(1)
root = env["FAKE_S3_ROOT"]
VALUED = {"--retries", "--low-level-retries", "--contimeout", "--timeout", "--format",
          "--separator", "--max-depth", "--include"}
pos, inc, i = [], [], 0
while i < len(args):
    if args[i] in VALUED:
        if args[i] == "--include":
            inc.append(args[i + 1].lstrip("/"))
        i += 2
    elif args[i].startswith("-"):
        i += 1
    else:
        pos.append(args[i]); i += 1
cmd, pos = pos[0], pos[1:]
def path(remote):
    assert remote.startswith("s3backup:"), remote
    return os.path.join(root, remote[len("s3backup:"):])
if cmd == "copyto":
    src, dst = pos[0], path(pos[1])
    if env.get("FAKE_S3_PUT_FAIL"):
        print("ERROR : upload failed", file=sys.stderr)
        sys.exit(1)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    data = open(src, "rb").read()
    if env.get("FAKE_S3_TRUNCATE") and dst.endswith(".age"):
        data = data[:-1]
    open(dst, "wb").write(data)
    sys.exit(0)
if cmd == "lsf":
    d = path(pos[0])
    if os.path.isdir(d):
        for n in sorted(os.listdir(d)):
            p = os.path.join(d, n)
            if os.path.isfile(p) and (not inc or n in inc):
                print(f"{os.path.getsize(p)} {n}")
    sys.exit(0)
sys.exit(99)
'''

COPY = PRELUDE + r'''
import shutil
args = [a for a in sys.argv[1:] if not a.startswith("-")]
if os.environ.get("FAKE_COPY_FAIL"):
    sys.exit(23)
*srcs, dest = args
for s in srcs:
    shutil.copy(s, dest)
'''

SHA = r'''#!PY
import hashlib, sys
for p in sys.argv[1:]:
    print(hashlib.sha256(open(p, "rb").read()).hexdigest() + "  " + p)
'''


class Env:
    def __init__(self, tmp: Path, rclone=True, age=True, rsync=True):
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.bin.mkdir()
        for t in TOOLS:
            real = shutil.which(t)
            if real:
                (self.bin / t).symlink_to(real)
        shims = {"sha256sum": SHA, "tar": SPY, "gzip": SPY, "scp": COPY}
        if rsync:
            shims["rsync"] = COPY
        if age:
            shims["age"] = AGE
        if rclone:
            shims["rclone"] = RCLONE
        for name, body in shims.items():
            p = self.bin / name
            p.write_text(body.replace("#!PY", "#!" + sys.executable, 1))
            p.chmod(p.stat().st_mode | stat.S_IXUSR)
        self.src = tmp / "opt"
        for name in ("swarm_connect_data", "swarm_connect_dev_data"):
            d = self.src / name
            d.mkdir(parents=True)
            (d / "stamp_owners.json").write_text(f'{{"token": "{PLAIN} {name}"}}')
            (d / "x402_audit.jsonl").write_text(f'{{"a": "{PLAIN}"}}\n')
        self.local = tmp / "backups"
        self.remote = tmp / "remote"
        self.remote.mkdir()
        self.s3 = tmp / "s3"
        self.s3.mkdir()
        self.tmpdir = tmp / "tmp"
        self.tmpdir.mkdir()
        self.etc = tmp / "etc"
        self.etc.mkdir(mode=0o700)
        self.settings = self.etc / "backup_s3.env"
        self.log = tmp / "shim.log"
        self.log.write_text("")
        self.env = {
            "PATH": str(self.bin),
            "HOME": str(tmp),
            "TMPDIR": str(self.tmpdir),
            "SHIM_LOG": str(self.log),
            "LEAK_SECRET": SECRET,
            "LEAK_KEY_ID": KEY_ID,
            "REAL_TAR": shutil.which("tar"),
            "REAL_GZIP": shutil.which("gzip"),
            "FAKE_S3_ROOT": str(self.s3),
            "BACKUP_SOURCES": f"{self.src / 'swarm_connect_data'} {self.src / 'swarm_connect_dev_data'}",
            "BACKUP_DIR": str(self.local),
            # Never pick up a real /etc/swarm_connect/backup_s3.env on the machine.
            "BACKUP_S3_ENV_FILE": "",
        }

    def write_settings(self, text=None, mode=0o600, **override):
        if text is None:
            values = {
                "BACKUP_S3_URI": f"s3://{BUCKET}/{PREFIX}",
                "BACKUP_S3_ENDPOINT": "https://objects.example.test",
                "BACKUP_S3_REGION": "eu-central-1",
                "BACKUP_S3_AGE_RECIPIENT": RECIPIENT,
                "BACKUP_S3_ACCESS_KEY_ID": KEY_ID,
                "BACKUP_S3_SECRET_ACCESS_KEY": SECRET,
            }
            values.update(override)
            text = "".join(f"{k}={v}\n" for k, v in values.items() if v is not None)
        self.settings.write_text(text)
        self.settings.chmod(mode)
        self.env["BACKUP_S3_ENV_FILE"] = str(self.settings)
        return self.settings

    def run(self, *args, direct=False, **extra):
        env = dict(self.env)
        env.update(extra)
        env = {k: v for k, v in env.items() if v is not None}
        cmd = [str(SCRIPT), *args] if direct else ["bash", str(SCRIPT), *args]
        return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)

    def calls(self, tool):
        return [l for l in self.log.read_text().splitlines() if l.startswith(tool + " ")]

    def uploads(self):
        return [c for c in self.calls("rclone") if " copyto " in c]

    def archives(self):
        return sorted(p.name for p in self.local.glob("*.tar.gz"))

    def objects(self):
        base = self.s3 / BUCKET / PREFIX
        return sorted(p.name for p in base.iterdir()) if base.is_dir() else []

    def all_uploaded_bytes(self):
        return [p.read_bytes() for p in self.s3.rglob("*") if p.is_file()]


@pytest.fixture
def e(tmp_path):
    return Env(tmp_path)


def assert_no_secret(e, r):
    assert SECRET not in r.stdout + r.stderr and KEY_ID not in r.stdout + r.stderr
    log = e.log.read_text()
    assert SECRET not in log and KEY_ID not in log, "a credential was on a command line"
    assert "ENVLEAK" not in log, [l for l in log.splitlines() if l.startswith("ENVLEAK")]


def assert_failed_offhost(e, r, n_archives=2):
    assert r.returncode == 1, r.stderr
    assert re.search(r"^FAILED: off-host copy of \S+\.tar\.gz to ", r.stderr, re.M), r.stderr
    assert len(e.archives()) == n_archives, "the local archives must stay"
    assert not (e.local / ".last_success").exists()


# --- without S3 settings: unchanged -------------------------------------------

STAMP_RE = r"\d{8}T\d{6}Z"


def expected_plain_stdout(e, remote=None):
    lines = []
    for name in ("swarm_connect_data", "swarm_connect_dev_data"):
        lines.append(rf"backed up {re.escape(str(e.src / name))} -> "
                     rf"{re.escape(str(e.local / name))}-{STAMP_RE}\.tar\.gz \(\S+\)")
        if remote:
            lines.append(rf"copied off-host to {re.escape(str(remote))}")
    return "^" + "\n".join(lines) + "\n$"


@pytest.mark.parametrize("env_file", ["", "unset"])
def test_without_s3_settings_the_output_is_unchanged(e, env_file):
    if env_file == "unset":
        if os.path.lexists("/etc/swarm_connect/backup_s3.env"):
            pytest.skip("this machine has a real /etc/swarm_connect/backup_s3.env")
        e.env.pop("BACKUP_S3_ENV_FILE")
    r = e.run()
    assert r.returncode == 0, r.stderr
    assert r.stderr == ""
    assert re.match(expected_plain_stdout(e), r.stdout), r.stdout
    assert (e.local / ".last_success").exists()
    assert e.calls("age") == [] and e.calls("rclone") == []
    assert sorted(p.name for p in e.local.iterdir()) == sorted(e.archives() + [".last_success"])
    assert list(e.tmpdir.iterdir()) == [], "no work directory without S3"


def test_without_s3_settings_backup_remote_is_unchanged(e):
    r = e.run(BACKUP_REMOTE=str(e.remote))
    assert r.returncode == 0 and r.stderr == ""
    assert re.match(expected_plain_stdout(e, e.remote), r.stdout), r.stdout
    assert sorted(p.name for p in e.remote.iterdir()) == e.archives()
    assert e.calls("age") == [] and e.calls("rclone") == []


def test_without_s3_settings_a_failed_remote_copy_is_unchanged(e):
    r = e.run(BACKUP_REMOTE=str(e.remote), FAKE_COPY_FAIL="1")
    assert r.returncode == 1
    assert r.stderr.count(f"FAILED: off-host copy of ") == 2
    assert re.fullmatch(rf"(FAILED: off-host copy of {re.escape(str(e.local))}/swarm_connect_(dev_)?data-"
                        rf"{STAMP_RE}\.tar\.gz to {re.escape(str(e.remote))}\n){{2}}", r.stderr), r.stderr
    assert not (e.local / ".last_success").exists()


def test_the_installed_path_runs_through_its_shebang(e):
    # cron runs /usr/local/sbin/swarm_connect_backup directly, so the shebang
    # and the executable bit are what pick the interpreter.
    assert os.access(SCRIPT, os.X_OK)
    assert SCRIPT.read_text().splitlines()[0] == "#!/usr/bin/env bash"
    r = e.run(direct=True)
    assert r.returncode == 0, r.stderr
    assert re.match(expected_plain_stdout(e), r.stdout)


# --- with S3 settings ---------------------------------------------------------

def test_success_uploads_ciphertext_and_its_checksum(e):
    e.write_settings()
    r = e.run()
    assert r.returncode == 0, r.stderr
    assert (e.local / ".last_success").exists()
    names = e.archives()
    assert e.objects() == sorted([n + ".age" for n in names] + [n + ".age.sha256" for n in names])
    base = e.s3 / BUCKET / PREFIX
    for n in names:
        cipher = (base / (n + ".age")).read_bytes()
        assert cipher.startswith(b"FAKE-AGE\n")
        # The checksum covers the ciphertext, in sha256sum -c format.
        assert (base / (n + ".age.sha256")).read_text() == \
            f"{hashlib.sha256(cipher).hexdigest()}  {n}.age\n"
    assert r.stdout.count(f"copied off-host to s3://{BUCKET}/{PREFIX}/") == 2
    assert "size verified" in r.stdout
    assert_no_secret(e, r)
    assert list(e.tmpdir.iterdir()) == [], "the work directory is removed"


def test_settings_reach_rclone_through_its_environment(e):
    e.write_settings()
    e.run()
    assert "rclone-endpoint https://objects.example.test region eu-central-1" in e.log.read_text()
    for c in e.calls("rclone"):
        assert "--config" not in c and "--s3-" not in c


def test_no_plaintext_is_ever_uploaded(e):
    e.write_settings()
    assert e.run().returncode == 0
    for data in e.all_uploaded_bytes():
        assert PLAIN.encode() not in data
    # rclone is only ever handed the ciphertext and its checksum.
    assert len(e.uploads()) == 4
    for c in e.uploads():
        src = c.split()[-2]
        assert src.endswith(".tar.gz.age") or src.endswith(".tar.gz.age.sha256"), c


def test_the_recipient_is_passed_to_age(e):
    e.write_settings()
    e.run()
    ages = e.calls("age")
    assert len(ages) == 2 and all(f"-r {RECIPIENT}" in c for c in ages)


def test_both_targets_together(e):
    e.write_settings()
    r = e.run(BACKUP_REMOTE=str(e.remote))
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in e.remote.iterdir()) == e.archives()
    assert len(e.objects()) == 4


def test_a_failed_rsync_copy_still_fails_with_s3_working(e):
    e.write_settings()
    r = e.run(BACKUP_REMOTE=str(e.remote), FAKE_COPY_FAIL="1")
    assert_failed_offhost(e, r)
    assert f"to {e.remote}" in r.stderr
    assert len(e.objects()) == 4


def test_bad_credentials(e):
    e.write_settings(BACKUP_S3_SECRET_ACCESS_KEY="wrong-secret")
    r = e.run()
    assert_failed_offhost(e, r)
    assert r.stderr.count(f"FAILED: off-host copy of ") == 2
    assert f"to s3://{BUCKET}/{PREFIX} (rclone exit 1" in r.stderr
    assert "AccessDenied" in r.stderr
    assert e.objects() == []
    assert "wrong-secret" not in r.stdout + r.stderr


def test_unreachable_endpoint(e):
    e.write_settings()
    r = e.run(FAKE_S3_UNREACHABLE="1")
    assert_failed_offhost(e, r)
    assert "connection refused" in r.stderr
    assert_no_secret(e, r)


def test_a_failed_upload(e):
    e.write_settings()
    r = e.run(FAKE_S3_PUT_FAIL="1")
    assert_failed_offhost(e, r)


def test_a_size_mismatch_after_upload_fails(e):
    e.write_settings()
    r = e.run(FAKE_S3_TRUNCATE="1")
    assert_failed_offhost(e, r)
    assert "size check failed" in r.stderr


def test_an_encryption_failure_uploads_nothing(e):
    e.write_settings()
    r = e.run(FAKE_AGE_FAIL="1")
    assert_failed_offhost(e, r)
    assert "encryption failed" in r.stderr
    assert e.uploads() == [] and e.objects() == []
    assert list(e.tmpdir.iterdir()) == []


@pytest.mark.parametrize("override,msg", [
    ({"BACKUP_S3_AGE_RECIPIENT": None}, "BACKUP_S3_AGE_RECIPIENT is not set"),
    ({"BACKUP_S3_AGE_RECIPIENT": "AGE-SECRET-KEY-1" + "Q" * 58}, "SECRET key"),
    ({"BACKUP_S3_AGE_RECIPIENT": "ssh-ed25519"}, "not an age public recipient"),
    ({"BACKUP_S3_AGE_RECIPIENT": "age1*"}, "may not contain"),
])
def test_encryption_is_mandatory(e, override, msg):
    e.write_settings(**override)
    r = e.run()
    assert_failed_offhost(e, r)
    assert msg in r.stderr
    assert e.calls("age") == [] and e.uploads() == [] and e.objects() == []
    assert "AGE-SECRET-KEY" not in r.stdout


@pytest.mark.parametrize("override,msg", [
    ({"BACKUP_S3_URI": None}, "BACKUP_S3_URI is not set"),
    ({"BACKUP_S3_URI": "https://bucket/prefix"}, "must be s3://bucket/prefix"),
    ({"BACKUP_S3_URI": f"s3://{BUCKET}"}, "needs a prefix"),
    ({"BACKUP_S3_URI": f"s3://{BUCKET}/"}, "needs a prefix"),
    ({"BACKUP_S3_URI": f"s3://{BUCKET}/a/../b"}, "prefix may use"),
    ({"BACKUP_S3_URI": "s3://Bad_Bucket/p"}, "bucket name is not valid"),
    ({"BACKUP_S3_ENDPOINT": "ftp://x"}, "http:// or https://"),
    ({"BACKUP_S3_ENDPOINT": "https://user@host"}, "no user@"),
    ({"BACKUP_S3_REGION": "EU"}, "BACKUP_S3_REGION"),
    ({"BACKUP_S3_SECRET_ACCESS_KEY": None}, "must both be set"),
])
def test_configuration_errors_fail_the_s3_copy_and_keep_local_archives(e, override, msg):
    e.write_settings(**override)
    r = e.run()
    assert_failed_offhost(e, r)
    assert r.stderr.count(f"to S3: ") == 2 and msg in r.stderr
    assert e.uploads() == []


def test_trailing_slash_on_the_prefix_is_ignored(e):
    e.write_settings(BACKUP_S3_URI=f"s3://{BUCKET}/{PREFIX}/")
    assert e.run().returncode == 0
    assert len(e.objects()) == 4


def test_s3_settings_in_the_environment_without_a_file_also_enable_s3(e):
    r = e.run(BACKUP_S3_URI=f"s3://{BUCKET}/{PREFIX}")
    assert_failed_offhost(e, r)
    assert "BACKUP_S3_AGE_RECIPIENT is not set" in r.stderr


def test_non_secret_settings_may_come_from_the_environment(e):
    e.write_settings(BACKUP_S3_URI=None, BACKUP_S3_AGE_RECIPIENT=None)
    r = e.run(BACKUP_S3_URI=f"s3://{BUCKET}/{PREFIX}", BACKUP_S3_AGE_RECIPIENT=RECIPIENT)
    assert r.returncode == 0, r.stderr
    assert len(e.objects()) == 4


def test_the_file_value_beats_the_environment(e):
    e.write_settings()
    r = e.run(BACKUP_S3_URI=f"s3://{BUCKET}/other")
    assert r.returncode == 0
    assert len(e.objects()) == 4


@pytest.mark.parametrize("var", ["BACKUP_S3_ACCESS_KEY_ID", "BACKUP_S3_SECRET_ACCESS_KEY"])
def test_credentials_in_the_environment_are_refused(e, var):
    # A cron line like `BACKUP_S3_SECRET_ACCESS_KEY=... swarm_connect_backup`
    # would put the secret in the crontab and in every child's environment.
    e.write_settings()
    r = e.run(**{var: SECRET if "SECRET" in var else KEY_ID})
    assert_failed_offhost(e, r)
    assert "must be set in the settings file" in r.stderr
    assert e.uploads() == []
    log = e.log.read_text()
    assert "ENVLEAK tar" not in log and "ENVLEAK gzip" not in log, \
        "the refused credential must not reach child processes"


def test_credentials_are_never_on_a_command_line_or_in_child_environments(e):
    e.write_settings()
    r = e.run(BACKUP_REMOTE=str(e.remote))
    assert r.returncode == 0
    assert e.calls("rclone") and e.calls("age") and e.calls("tar") and e.calls("rsync")
    assert_no_secret(e, r)


def test_credentials_are_redacted_if_rclone_prints_them(e):
    e.write_settings()
    fake = e.bin / "rclone"
    body = fake.read_text().replace(
        'if env.get("FAKE_S3_UNREACHABLE"):',
        'if env.get("FAKE_ECHO_SECRET"):\n'
        '    print("ERROR : bad key " + env[pre + "ACCESS_KEY_ID"] + " " + env[pre + "SECRET_ACCESS_KEY"], file=sys.stderr)\n'
        '    sys.exit(1)\n'
        'if env.get("FAKE_S3_UNREACHABLE"):')
    fake.write_text(body)
    r = e.run(FAKE_ECHO_SECRET="1")
    assert r.returncode == 1
    assert "bad key <redacted> <redacted>" in r.stderr
    assert SECRET not in r.stderr and KEY_ID not in r.stderr


# --- rclone pin ---------------------------------------------------------------

def test_rclone_version_must_match_the_pin(e):
    e.write_settings()
    r = e.run(FAKE_RCLONE_VERSION="v1.76.0")
    assert_failed_offhost(e, r)
    assert f"rclone v1.76.0 is installed, expected exactly {PIN}" in r.stderr
    assert e.uploads() == []


def test_the_pin_in_the_script_matches_the_docs():
    text = SCRIPT.read_text()
    assert f'RCLONE_PIN="{PIN}"' in text
    runbook = (REPO / "docs" / "mainnet-runbook.md").read_text()
    assert f"rclone-{PIN}-linux-" in runbook


def test_missing_rclone_fails_the_s3_copy(tmp_path):
    e = Env(tmp_path, rclone=False)
    e.write_settings()
    r = e.run()
    assert_failed_offhost(e, r)
    assert "rclone is not installed" in r.stderr


def test_missing_age_fails_the_s3_copy(tmp_path):
    e = Env(tmp_path, age=False)
    e.write_settings()
    r = e.run()
    assert_failed_offhost(e, r)
    assert "age is not installed" in r.stderr


# --- the settings file ----------------------------------------------------------

@pytest.mark.parametrize("mode", [0o620, 0o602, 0o666])
def test_a_settings_file_others_can_write_is_refused(e, mode):
    e.write_settings(mode=mode)
    r = e.run()
    assert_failed_offhost(e, r)
    assert "writable by group or others" in r.stderr


def test_a_settings_file_in_a_writable_directory_is_refused(e):
    e.write_settings()
    e.etc.chmod(0o777)
    try:
        r = e.run()
    finally:
        e.etc.chmod(0o700)
    assert_failed_offhost(e, r)
    assert "chmod 700" in r.stderr


def test_a_symlinked_settings_file_is_refused(e):
    real = e.write_settings()
    link = e.etc / "link.env"
    link.symlink_to(real)
    r = e.run(BACKUP_S3_ENV_FILE=str(link))
    assert_failed_offhost(e, r)
    assert "symlink" in r.stderr


def test_a_named_settings_file_that_does_not_exist_is_refused(e):
    r = e.run(BACKUP_S3_ENV_FILE=str(e.etc / "missing.env"))
    assert_failed_offhost(e, r)
    assert "missing.env, which does not exist" in r.stderr


def test_a_relative_settings_file_path_is_refused(e):
    r = e.run(BACKUP_S3_ENV_FILE="backup_s3.env")
    assert_failed_offhost(e, r)
    assert "absolute path" in r.stderr


@pytest.mark.parametrize("line,msg", [
    ("export BACKUP_S3_URI=s3://b/p", "is not KEY=VALUE"),
    ("SOMETHING_ELSE=1", "is not a known setting"),
    (f"BACKUP_S3_SECRET_ACCESS_KEY=$HOME{SECRET}", "values are literal"),
    (f"BACKUP_S3_SECRET_ACCESS_KEY=a {SECRET}", "quote a value"),
    ("rm -rf /", "is not KEY=VALUE"),
])
def test_a_bad_settings_line_is_refused_without_echoing_it(e, line, msg):
    e.write_settings(text=line + "\n")
    r = e.run()
    assert_failed_offhost(e, r)
    assert msg in r.stderr
    assert SECRET not in r.stderr and "rm -rf" not in r.stderr


def test_a_duplicate_key_is_refused(e):
    e.write_settings(text=f"BACKUP_S3_URI=s3://{BUCKET}/a\nBACKUP_S3_URI=s3://{BUCKET}/b\n")
    r = e.run()
    assert_failed_offhost(e, r)
    assert "set twice" in r.stderr


def test_comments_blank_lines_and_quotes_are_accepted(e):
    e.write_settings(text=(
        "# gateway-state off-host copy\n\n"
        f"BACKUP_S3_URI='s3://{BUCKET}/{PREFIX}'\n"
        f"BACKUP_S3_AGE_RECIPIENT=\"{RECIPIENT} {'age1' + 'p' * 58}\"\n"
        f"BACKUP_S3_ACCESS_KEY_ID={KEY_ID}\n"
        f"BACKUP_S3_SECRET_ACCESS_KEY=\"{SECRET}\"\n"))
    r = e.run()
    assert r.returncode == 0, r.stderr
    assert len(e.objects()) == 4
    ages = e.calls("age")
    assert len(ages) == 2 and all(c.count(" -r ") == 2 for c in ages)


def test_a_config_error_without_any_archive_still_fails(e):
    e.write_settings(BACKUP_S3_URI=None)
    r = e.run(BACKUP_SOURCES=str(e.tmp / "nothing-here"))
    assert r.returncode == 1
    assert "FAILED: off-host copy to S3: BACKUP_S3_URI is not set" in r.stderr
    assert not (e.local / ".last_success").exists()


def test_a_failed_source_with_s3_still_uploads_the_others(e):
    e.write_settings()
    r = e.run(BACKUP_SOURCES=f"{e.src / 'swarm_connect_data'} {e.tmp / 'not-a-dir'}")
    assert r.returncode == 0
    assert "skip: " in r.stdout
    assert len(e.objects()) == 2
