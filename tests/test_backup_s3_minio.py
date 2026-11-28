"""#493: the S3 off-host copy of scripts/backup_state.sh against a real MinIO.

Opt-in: skipped unless RUN_S3_BACKUP_TEST=1, because it needs a local Docker
daemon and pulls images. Real `age`, the pinned rclone and the real script run
in a throwaway "runner" container. MinIO and the runner share one
`docker network create --internal` network and nothing else, so neither can
reach the internet or the host while the tests run (the image build before
that does need the network, for apt and the image pulls).

    RUN_S3_BACKUP_TEST=1 python -m pytest tests/test_backup_s3_minio.py -s

The bucket is created with object lock and a default GOVERNANCE retention, and
the backup key is a MinIO user whose policy allows only s3:PutObject under the
prefix and s3:ListBucket on that prefix, as the runbook prescribes. So the
success case also proves that the script works with a put+list-only key on a
locked bucket.

Images, pinned by digest (multi-arch index; the tests run natively on amd64
or arm64, never emulated):
- MinIO: bitnamilegacy/minio, a repack of MinIO that bundles `mc`. MinIO's own
  images are not used because they can no longer be pulled: on 2026-11-06
  `docker buildx imagetools inspect minio/minio:latest` answered "pull access
  denied" and quay.io/minio/minio answered 401. The bitnamilegacy repository
  is frozen (no updates), which is acceptable for an emulator in a test.
- rclone: rclone/rclone at the script's RCLONE_PIN.
- runner base: debian:bookworm-slim, with `age` from Debian's apt.
"""
import os
import re
import secrets
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "backup_state.sh"

MINIO_IMAGE = ("bitnamilegacy/minio:2025.7.23-debian-12-r5"
               "@sha256:6dabb4a2088c9a79908de3bc05f4586c23ad2182c8908e7e3acbf61c1467fb20")
RCLONE_IMAGE = ("rclone/rclone:1.75.2"
                "@sha256:2687085f718d3c628f7fdfb77c52a1d344332aed543110bb88088f2c70d43eb5")
DEBIAN_IMAGE = ("debian:bookworm-slim"
                "@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc")
RUNNER_TAG = "swarm-connect-s3-backup-test:493"

BUCKET = "backups"
PREFIX = "gw/host-a"

pytestmark = pytest.mark.skipif(os.environ.get("RUN_S3_BACKUP_TEST") != "1",
                                reason="Docker + MinIO test; set RUN_S3_BACKUP_TEST=1 to run")

DOCKERFILE = f"""
FROM {RCLONE_IMAGE} AS rclone
FROM {DEBIAN_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends age \\
 && rm -rf /var/lib/apt/lists/*
COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone
"""

POLICY = f"""{{"Version": "2012-10-17", "Statement": [
 {{"Effect": "Allow", "Action": ["s3:PutObject"], "Resource": ["arn:aws:s3:::{BUCKET}/{PREFIX}/*"]}},
 {{"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": ["arn:aws:s3:::{BUCKET}"],
   "Condition": {{"StringLike": {{"s3:prefix": ["{PREFIX}/", "{PREFIX}/*"]}}}}}}]}}"""


def sh(*args, input=None, check=True, timeout=600):
    r = subprocess.run(list(args), input=input, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise AssertionError(f"{args[:3]} exit {r.returncode}\n{r.stdout}\n{r.stderr}")
    return r


@pytest.fixture(scope="module")
def minio():
    sh("docker", "build", "-q", "-t", RUNNER_TAG, "-", input=DOCKERFILE, timeout=900)
    sh("docker", "pull", "-q", MINIO_IMAGE, timeout=900)
    tag = secrets.token_hex(4)
    net, name = f"s3test-{tag}", f"s3test-minio-{tag}"
    root_user, root_pw = "root" + tag, secrets.token_hex(16)
    key_id, key_secret = "backup" + tag, secrets.token_hex(16)
    sh("docker", "network", "create", "--internal", net)
    try:
        # Credentials go in through a root-only env file, not on argv.
        envf = Path(os.environ.get("TMPDIR", "/tmp")) / f"minio-{tag}.env"
        envf.write_text(f"MINIO_ROOT_USER={root_user}\nMINIO_ROOT_PASSWORD={root_pw}\n")
        envf.chmod(0o600)
        try:
            sh("docker", "run", "-d", "--name", name, "--network", net, "--env-file", str(envf),
               "--entrypoint", "/opt/bitnami/minio/bin/minio", MINIO_IMAGE, "server", "/tmp/data")
        finally:
            envf.unlink()
        setup = f"""
set -eu
for i in $(seq 1 60); do mc alias set local http://127.0.0.1:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null 2>&1 && break; sleep 1; done
mc mb --with-lock local/{BUCKET} >/dev/null
mc retention set --default GOVERNANCE 1d local/{BUCKET} >/dev/null
cat > /tmp/policy.json <<'JSON'
{POLICY}
JSON
mc admin policy create local backup-put-list /tmp/policy.json >/dev/null
read -r KID KSECRET
mc admin user add local "$KID" "$KSECRET" >/dev/null
mc admin policy attach local backup-put-list --user "$KID" >/dev/null
echo SETUP_OK
"""
        # The backup key's id and secret go in on stdin; the root credentials
        # are already in the container's environment.
        r = sh("docker", "exec", "-i", "-e", "HOME=/tmp", name, "bash", "-c", setup,
               input=f"{key_id} {key_secret}\n", check=False)
        assert "SETUP_OK" in r.stdout, r.stdout + r.stderr
        yield {"net": net, "name": name, "key_id": key_id, "key_secret": key_secret,
               "root_user": root_user, "root_pw": root_pw}
    finally:
        sh("docker", "rm", "-f", name, check=False)
        sh("docker", "network", "rm", net, check=False)


def run_case(minio, endpoint, key_secret, after=""):
    """Run backup_state.sh in a fresh runner container; return its output."""
    script = SCRIPT.read_text()
    settings = (f"BACKUP_S3_URI=s3://{BUCKET}/{PREFIX}\n"
                f"BACKUP_S3_ENDPOINT={endpoint}\n"
                "BACKUP_S3_REGION=us-east-1\n"
                "BACKUP_S3_AGE_RECIPIENT=__RECIPIENT__\n"
                f"BACKUP_S3_ACCESS_KEY_ID={minio['key_id']}\n"
                f"BACKUP_S3_SECRET_ACCESS_KEY={key_secret}\n")
    body = f"""
set -u
cat > /usr/local/sbin/swarm_connect_backup <<'SCRIPT_EOF'
{script}
SCRIPT_EOF
chmod 0755 /usr/local/sbin/swarm_connect_backup
for d in swarm_connect_data swarm_connect_dev_data; do
  mkdir -p /opt/$d/sub
  printf '{{"token": "PLAINTEXT-STATE-MARKER %s"}}\\n' "$d" > /opt/$d/stamp_owners.json
  head -c 300000 /dev/urandom > /opt/$d/sub/blob.bin
done
age-keygen -o /root/identity 2>/dev/null
R="$(age-keygen -y /root/identity)"
install -d -m 0700 /etc/swarm_connect
umask 077
cat > /etc/swarm_connect/backup_s3.env <<'ENV_EOF'
{settings}ENV_EOF
sed -i "s/__RECIPIENT__/$R/" /etc/swarm_connect/backup_s3.env
/usr/local/sbin/swarm_connect_backup
echo "BACKUP_RC=$?"
echo "LOCAL=$(ls /var/backups/swarm_connect | tr '\\n' ' ')"
[ -e /var/backups/swarm_connect/.last_success ] && echo LAST_SUCCESS=yes || echo LAST_SUCCESS=no
{after}
"""
    # The whole script, credentials included, goes in on stdin.
    return sh("docker", "run", "--rm", "-i", "--network", minio["net"], RUNNER_TAG, "bash", "-s",
              input=body, check=False, timeout=600)


def test_success_lands_encrypted_and_restores(minio):
    endpoint = f"http://{minio['name']}:9000"
    # After the backup: fetch with the admin key (the backup key cannot read),
    # check the checksums, decrypt, unpack and compare with the source.
    after = f"""
export RCLONE_CONFIG=/tmp/empty.conf; : > "$RCLONE_CONFIG"
export RCLONE_CONFIG_ADMIN_TYPE=s3 RCLONE_CONFIG_ADMIN_PROVIDER=Other RCLONE_CONFIG_ADMIN_ENDPOINT={endpoint}
export RCLONE_CONFIG_ADMIN_ACCESS_KEY_ID={minio['root_user']} RCLONE_CONFIG_ADMIN_SECRET_ACCESS_KEY={minio['root_pw']}
export RCLONE_CONFIG_SCOPED_TYPE=s3 RCLONE_CONFIG_SCOPED_PROVIDER=Other RCLONE_CONFIG_SCOPED_ENDPOINT={endpoint}
export RCLONE_CONFIG_SCOPED_ACCESS_KEY_ID={minio['key_id']} RCLONE_CONFIG_SCOPED_SECRET_ACCESS_KEY={minio['key_secret']}
echo "OBJECTS=$(rclone lsf admin:{BUCKET}/{PREFIX}/ | sort | tr '\\n' ' ')"
mkdir /restore /restored && rclone copy admin:{BUCKET}/{PREFIX}/ /restore && cd /restore
sha256sum -c ./*.sha256 && echo CHECKSUMS_OK
grep -l PLAINTEXT-STATE-MARKER /restore/* && echo PLAINTEXT_IN_BUCKET || echo NO_PLAINTEXT_IN_BUCKET
for f in *.age; do age -d -i /root/identity -o "/tmp/${{f%.age}}" "$f" && tar -xzf "/tmp/${{f%.age}}" -C /restored; done
diff -r /opt/swarm_connect_data /restored/swarm_connect_data && diff -r /opt/swarm_connect_dev_data /restored/swarm_connect_dev_data && echo RESTORE_MATCHES
first="$(ls *.age | head -n 1)"
rclone cat "scoped:{BUCKET}/{PREFIX}/$first" >/dev/null 2>&1 && echo SCOPED_CAN_READ || echo SCOPED_CANNOT_READ
# `rclone delete` lists (allowed) and then sends the delete itself, so this
# exercises the missing delete right, not a failed HEAD.
rclone delete "scoped:{BUCKET}/{PREFIX}/" --include "/$first" --s3-no-head 2>/tmp/del.err && echo SCOPED_DELETE_EXIT_0 || echo SCOPED_DELETE_REFUSED
grep -q AccessDenied /tmp/del.err && echo DELETE_ACCESS_DENIED
rclone lsf "admin:{BUCKET}/{PREFIX}/" | grep -qx "$first" && echo OBJECT_STILL_THERE
rclone copyto /etc/hostname "scoped:{BUCKET}/elsewhere/x" --s3-no-check-bucket --s3-no-head >/dev/null 2>&1 && echo SCOPED_CAN_WRITE_ELSEWHERE || echo SCOPED_CANNOT_WRITE_ELSEWHERE
"""
    r = run_case(minio, endpoint, minio["key_secret"], after)
    out = r.stdout
    print(out, r.stderr)
    assert "BACKUP_RC=0" in out, r.stderr
    assert "LAST_SUCCESS=yes" in out
    assert out.count(f"copied off-host to s3://{BUCKET}/{PREFIX}/") == 2
    objects = re.search(r"^OBJECTS=(.*)$", out, re.M).group(1).split()
    assert len(objects) == 4
    assert sum(o.endswith(".tar.gz.age") for o in objects) == 2
    assert sum(o.endswith(".tar.gz.age.sha256") for o in objects) == 2
    for marker in ("CHECKSUMS_OK", "NO_PLAINTEXT_IN_BUCKET", "RESTORE_MATCHES", "SCOPED_CANNOT_READ",
                   "SCOPED_DELETE_REFUSED", "DELETE_ACCESS_DENIED", "OBJECT_STILL_THERE", "SCOPED_CANNOT_WRITE_ELSEWHERE"):
        assert marker in out, marker
    assert minio["key_secret"] not in out + r.stderr


def assert_failed_run(r):
    out = r.stdout
    print(out, r.stderr)
    assert "BACKUP_RC=1" in out
    assert "LAST_SUCCESS=no" in out
    assert re.search(r"^LOCAL=.*swarm_connect_data-\S+\.tar\.gz .*swarm_connect_dev_data-\S+\.tar\.gz", out, re.M)
    assert r.stderr.count(f"FAILED: off-host copy of /var/backups/swarm_connect/") == 2, r.stderr


def test_bad_credentials(minio):
    wrong = secrets.token_hex(16)
    r = run_case(minio, f"http://{minio['name']}:9000", wrong)
    assert_failed_run(r)
    assert wrong not in r.stdout + r.stderr


def test_unreachable_endpoint(minio):
    # Same host, a port nothing listens on, inside the internal network.
    r = run_case(minio, f"http://{minio['name']}:9999", minio["key_secret"])
    assert_failed_run(r)
    assert minio["key_secret"] not in r.stdout + r.stderr
