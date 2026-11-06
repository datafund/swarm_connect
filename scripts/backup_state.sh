#!/usr/bin/env bash
# Back up the gateway's persistent state (#375): ownership registry, prepaid
# bandwidth credit, pool state, allowance and spend counters, and the x402
# audit log. The archives contain bearer credit tokens: treat every copy,
# including the off-host one, as secret.
#
# Install outside the deployed checkout (every deploy re-clones it), e.g.:
#   install -m 0755 scripts/backup_state.sh /usr/local/sbin/swarm_connect_backup
#   cron: 15 3 * * * root BACKUP_REMOTE=... /usr/local/sbin/swarm_connect_backup >> /var/log/swarm_connect_backup.log 2>&1
#
# Environment (all optional):
#   BACKUP_SOURCES    directories to archive (default: /opt/swarm_connect_data /opt/swarm_connect_dev_data)
#   BACKUP_DIR        where archives are written (default: /var/backups/swarm_connect)
#   BACKUP_KEEP_DAYS  local retention in whole days (default: 14)
#   BACKUP_REMOTE     rsync/scp destination for an OFF-HOST copy, e.g. backup@host:/backups/swarm_connect
#                     (root needs an ssh key and a known_hosts entry for it)
#
# Exit status is non-zero if any source failed to archive or copy; the other
# sources are still processed. A successful run touches $BACKUP_DIR/.last_success.
#
# --- Off-host copy to an S3-compatible object store (#493) -------------------
# Optional, in addition to or instead of BACKUP_REMOTE. With none of the
# settings below and no settings file, the script behaves exactly as without
# this feature. Settings (docs/mainnet-runbook.md §4):
#   BACKUP_S3_URI            s3://bucket/prefix (a prefix is required; one per host)
#   BACKUP_S3_ENDPOINT       custom endpoint URL for non-AWS providers (empty: AWS)
#   BACKUP_S3_REGION         region (empty: the tool's default)
#   BACKUP_S3_AGE_RECIPIENT  one or more space-separated age PUBLIC recipients
#                            (age1...). A dedicated key for gateway state, not the
#                            Bee keystore key. An AGE-SECRET-KEY value is refused.
#   BACKUP_S3_ACCESS_KEY_ID, BACKUP_S3_SECRET_ACCESS_KEY
#                            the credentials. Accepted ONLY from the settings
#                            file; set in the environment (a cron line) they are
#                            refused. Use a key scoped to this bucket and prefix
#                            with put and list rights only.
#   BACKUP_S3_ENV_FILE       the settings file (default: /etc/swarm_connect/backup_s3.env;
#                            empty: none). Root-only, outside the checkout. Parsed,
#                            never sourced: KEY=VALUE lines for the settings above,
#                            blank lines and # comments; values literal, optionally
#                            in one pair of quotes. Values in the file override the
#                            environment. Refused: a symlink, a non-regular file, a
#                            file not owned by the running user, a file or directory
#                            that group or others can write.
#
# Each archive is encrypted with `age -r` into a temporary directory that only
# ever holds ciphertext; the host holds no decryption key. The ciphertext and a
# `<name>.tar.gz.age.sha256` file (sha256 of the CIPHERTEXT, in sha256sum -c
# format, so a download can be checked without the key; age itself
# authenticates the plaintext on decryption) are uploaded next to each other.
# The upload is then verified by listing the prefix and comparing both objects'
# sizes with the local files. There is no re-download check, because the key
# deliberately has no read (GetObject) right.
#
# Tool: rclone, pinned to exactly RCLONE_PIN below; any other version fails the
# S3 copy. Chosen over the aws CLI because it installs on Ubuntu 24.04 as one
# upstream .deb or static binary (the aws CLI v2 is not packaged in apt), works
# with any S3-compatible provider, takes its whole configuration, credentials
# included, from RCLONE_CONFIG_* environment variables (never argv, so nothing
# shows in ps), and can run with a put+list-only key (no bucket check, no HEAD
# after upload, no destination check). The variables are exported only to the
# rclone processes, never to tar, age or rsync.
# To bump the pin: install the new version on a test host, run
# RUN_S3_BACKUP_TEST=1 pytest tests/test_backup_s3_minio.py with the new image
# tag and digest, change RCLONE_PIN here and in the runbook install step, then
# reinstall this script and the new rclone on every host together.
#
# S3 failures follow the rules above: any failure (configuration, encryption,
# upload or size check) logs `FAILED: off-host copy of <archive> to ...`, the
# local archive stays, the run exits 1 and .last_success is not touched.
set -u
set -o pipefail

SOURCES="${BACKUP_SOURCES:-/opt/swarm_connect_data /opt/swarm_connect_dev_data}"
DIR="${BACKUP_DIR:-/var/backups/swarm_connect}"
KEEP="${BACKUP_KEEP_DAYS:-14}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
failed=0

case "$KEEP" in
  ''|*[!0-9]*) echo "BACKUP_KEEP_DAYS must be a whole number of days, got '$KEEP'" >&2; exit 2 ;;
esac

RCLONE_PIN="v1.75.2"
S3_KEYS="BACKUP_S3_URI BACKUP_S3_ENDPOINT BACKUP_S3_REGION BACKUP_S3_AGE_RECIPIENT BACKUP_S3_ACCESS_KEY_ID BACKUP_S3_SECRET_ACCESS_KEY"
s3_on=0       # 1 when any S3 setting or the settings file is present
s3_err=""     # why the S3 copy cannot run; every archive then fails its S3 copy
S3_ENV_FILE="${BACKUP_S3_ENV_FILE-/etc/swarm_connect/backup_s3.env}"

# Credentials are taken from the settings file only. If they are in the
# environment, refuse them and drop them so no child process inherits them.
creds_in_env=""
for k in BACKUP_S3_ACCESS_KEY_ID BACKUP_S3_SECRET_ACCESS_KEY; do
  if [ -n "${!k+x}" ]; then creds_in_env="$creds_in_env $k"; unset "$k"; fi
done
for k in $S3_KEYS; do [ -n "${!k:-}" ] && s3_on=1; done
[ -n "$creds_in_env" ] && s3_on=1
[ -n "$S3_ENV_FILE" ] && { [ -e "$S3_ENV_FILE" ] || [ -L "$S3_ENV_FILE" ] || [ -n "${BACKUP_S3_ENV_FILE:-}" ]; } && s3_on=1

# s3_bad <reason>: record why the S3 copy cannot run (the first reason wins).
s3_bad() { [ -n "$s3_err" ] || s3_err="$*"; return 1; }

# s3_load <file>: read KEY=VALUE lines, as in backup_bee_keys.sh. Values are
# set as shell variables only, never exported. A bad line is reported by
# number, without its value.
s3_load() {
  local f="$1" n=0 line key val k known seen=" "
  while IFS= read -r line || [ -n "$line" ]; do
    n=$((n + 1))
    line="${line%$'\r'}"
    case "${line#"${line%%[![:space:]]*}"}" in ''|'#'*) continue ;; esac
    case "$line" in
      *=*) key="${line%%=*}"; val="${line#*=}" ;;
      *) s3_bad "$f line $n is not KEY=VALUE"; return 1 ;;
    esac
    case "$key" in
      ''|[!A-Z_]*|*[!A-Z0-9_]*) s3_bad "$f line $n is not KEY=VALUE (a key is A-Z, 0-9 and _)"; return 1 ;;
    esac
    known=""
    for k in $S3_KEYS; do [ "$k" = "$key" ] && known=1; done
    [ -n "$known" ] || { s3_bad "$f line $n: '$key' is not a known setting"; return 1; }
    case "$seen" in *" $key "*) s3_bad "$f line $n: '$key' is set twice"; return 1 ;; esac
    seen="$seen$key "
    case "$val" in
      \"*\") val="${val#\"}"; val="${val%\"}"; case "$val" in *\"*) s3_bad "$f line $n: malformed quoting"; return 1 ;; esac ;;
      \'*\') val="${val#\'}"; val="${val%\'}"; case "$val" in *\'*) s3_bad "$f line $n: malformed quoting"; return 1 ;; esac ;;
      *[[:space:]\"\']*) s3_bad "$f line $n: quote a value that contains spaces or quotes"; return 1 ;;
    esac
    case "$val" in *[\$\`\\]*) s3_bad "$f line $n: \$, \` and \\ are not allowed; values are literal"; return 1 ;; esac
    printf -v "$key" '%s' "$val" 2>/dev/null || { s3_bad "$f line $n: could not set '$key'"; return 1; }
  done < "$f"
}

# s3_config: load and check every S3 setting once, before any archive is made.
s3_config() {
  local f="$S3_ENV_FILE" d r
  [ -z "$creds_in_env" ] || s3_bad "${creds_in_env# } must be set in the settings file ($f), not in the environment" || return 1
  if [ -n "$f" ]; then
    case "$f" in /*) ;; *) s3_bad "BACKUP_S3_ENV_FILE must be an absolute path"; return 1 ;; esac
    if [ -L "$f" ]; then
      s3_bad "$f is a symlink; point BACKUP_S3_ENV_FILE at the real file"; return 1
    elif [ -e "$f" ]; then
      [ -f "$f" ] || { s3_bad "$f is not a regular file"; return 1; }
      [ -n "$(find "$f" -user "$(id -u)")" ] || { s3_bad "$f is not owned by uid $(id -u)"; return 1; }
      [ -z "$(find "$f" \( -perm -020 -o -perm -002 \))" ] || { s3_bad "$f is writable by group or others; chmod 600 it"; return 1; }
      d="$(dirname "$f")"
      [ -n "$(find "$d" -maxdepth 0 \( -user "$(id -u)" -o -user 0 \))" ] || { s3_bad "the directory $d is not owned by uid $(id -u) or root"; return 1; }
      [ -z "$(find "$d" -maxdepth 0 \( -perm -020 -o -perm -002 \))" ] || { s3_bad "the directory $d is writable by group or others; chmod 700 it"; return 1; }
      [ -r "$f" ] || { s3_bad "$f is not readable"; return 1; }
      s3_load "$f" || return 1
    elif [ -n "${BACKUP_S3_ENV_FILE:-}" ]; then
      s3_bad "BACKUP_S3_ENV_FILE names $f, which does not exist"; return 1
    fi
  fi

  S3_URI="${BACKUP_S3_URI:-}"
  [ -n "$S3_URI" ] || { s3_bad "BACKUP_S3_URI is not set"; return 1; }
  case "$S3_URI" in s3://*) ;; *) s3_bad "BACKUP_S3_URI must be s3://bucket/prefix"; return 1 ;; esac
  r="${S3_URI#s3://}"
  S3_BUCKET="${r%%/*}"
  S3_PREFIX=""; [ "$r" != "$S3_BUCKET" ] && S3_PREFIX="${r#*/}"
  while [ "${S3_PREFIX%/}" != "$S3_PREFIX" ]; do S3_PREFIX="${S3_PREFIX%/}"; done
  case "$S3_BUCKET" in
    ''|[!a-z0-9]*|*[!a-z0-9]|*[!a-z0-9.-]*) s3_bad "BACKUP_S3_URI bucket name is not valid (a-z, 0-9, . and -)"; return 1 ;;
  esac
  [ "${#S3_BUCKET}" -ge 3 ] && [ "${#S3_BUCKET}" -le 63 ] || { s3_bad "BACKUP_S3_URI bucket name must be 3 to 63 characters"; return 1; }
  [ -n "$S3_PREFIX" ] || { s3_bad "BACKUP_S3_URI needs a prefix (s3://bucket/prefix) that the key is scoped to"; return 1; }
  case "$S3_PREFIX" in
    *[!A-Za-z0-9._/-]*|/*|*//*|.|..|./*|../*|*/.|*/..|*/./*|*/../*) s3_bad "BACKUP_S3_URI prefix may use A-Z, a-z, 0-9, ., _, - and single / between segments"; return 1 ;;
  esac
  S3_ENDPOINT="${BACKUP_S3_ENDPOINT:-}"
  if [ -n "$S3_ENDPOINT" ]; then
    case "$S3_ENDPOINT" in
      http://?*|https://?*) ;;
      *) s3_bad "BACKUP_S3_ENDPOINT must be an http:// or https:// URL"; return 1 ;;
    esac
    case "$S3_ENDPOINT" in *[!A-Za-z0-9.:/_-]*) s3_bad "BACKUP_S3_ENDPOINT may only hold a scheme, host, port and path (no user@)"; return 1 ;; esac
  fi
  S3_REGION="${BACKUP_S3_REGION:-}"
  case "$S3_REGION" in *[!a-z0-9-]*) s3_bad "BACKUP_S3_REGION may only use a-z, 0-9 and -"; return 1 ;; esac

  local recipients="${BACKUP_S3_AGE_RECIPIENT:-}" rs
  [ -n "$recipients" ] || { s3_bad "BACKUP_S3_AGE_RECIPIENT is not set; archives are only uploaded encrypted"; return 1; }
  case "$recipients" in *[\*\?\[]*) s3_bad "BACKUP_S3_AGE_RECIPIENT may not contain *, ? or ["; return 1 ;; esac
  set -f; rs=($recipients); set +f
  age_args=()
  for r in "${rs[@]}"; do
    case "$r" in
      AGE-SECRET-KEY-*) s3_bad "BACKUP_S3_AGE_RECIPIENT holds an age SECRET key; the host must only hold the public recipient (age1...)"; return 1 ;;
      age1*) case "$r" in *[!a-z0-9]*) s3_bad "BACKUP_S3_AGE_RECIPIENT entry is not an age public recipient (age1...)"; return 1 ;; esac
             age_args+=(-r "$r") ;;
      *) s3_bad "BACKUP_S3_AGE_RECIPIENT entry is not an age public recipient (age1...)"; return 1 ;;
    esac
  done
  [ "${#age_args[@]}" -gt 0 ] || { s3_bad "BACKUP_S3_AGE_RECIPIENT is blank"; return 1; }

  S3_KEY_ID="${BACKUP_S3_ACCESS_KEY_ID:-}"
  S3_SECRET="${BACKUP_S3_SECRET_ACCESS_KEY:-}"
  [ -n "$S3_KEY_ID" ] && [ -n "$S3_SECRET" ] || { s3_bad "BACKUP_S3_ACCESS_KEY_ID and BACKUP_S3_SECRET_ACCESS_KEY must both be set in $f"; return 1; }

  command -v age >/dev/null 2>&1 || { s3_bad "age is not installed (apt-get install age)"; return 1; }
  command -v rclone >/dev/null 2>&1 || { s3_bad "rclone is not installed; install rclone $RCLONE_PIN (docs/mainnet-runbook.md §4)"; return 1; }
  local found
  found="$(rclone version 2>/dev/null | head -n 1)"
  found="${found#rclone }"
  [ "$found" = "$RCLONE_PIN" ] || { s3_bad "rclone ${found:-of unknown version} is installed, expected exactly $RCLONE_PIN (the pinned version; see the script header to bump it)"; return 1; }
  S3_LABEL="s3://$S3_BUCKET/$S3_PREFIX"
  return 0
}

if command -v sha256sum >/dev/null 2>&1; then sha() { sha256sum "$1" | cut -d' ' -f1; }
else sha() { shasum -a 256 "$1" | cut -d' ' -f1; }; fi
fsize() { local s; s="$(wc -c < "$1")" && printf '%s' "${s//[[:space:]]/}"; }

# rcl <args>: run rclone with an on-the-fly remote "s3backup" defined in its
# environment only. RCLONE_CONFIG points at an empty file, so no rclone.conf is
# read. Its stderr goes to $WORK/rclone.err; s3_copy prints it redacted.
rcl() {
  (
    export RCLONE_CONFIG="$WORK/rclone.conf"
    export RCLONE_CONFIG_S3BACKUP_TYPE=s3
    export RCLONE_CONFIG_S3BACKUP_PROVIDER=Other
    export RCLONE_CONFIG_S3BACKUP_ENV_AUTH=false
    export RCLONE_CONFIG_S3BACKUP_ACCESS_KEY_ID="$S3_KEY_ID"
    export RCLONE_CONFIG_S3BACKUP_SECRET_ACCESS_KEY="$S3_SECRET"
    export RCLONE_CONFIG_S3BACKUP_ENDPOINT="$S3_ENDPOINT"
    export RCLONE_CONFIG_S3BACKUP_REGION="$S3_REGION"
    export RCLONE_CONFIG_S3BACKUP_NO_CHECK_BUCKET=true
    export RCLONE_CONFIG_S3BACKUP_NO_HEAD=true
    exec rclone --retries 3 --low-level-retries 3 --contimeout 15s --timeout 60s "$@"
  ) 2>> "$WORK/rclone.err"
}
show_rclone_err() {
  local l
  [ -s "$WORK/rclone.err" ] || return 0
  while IFS= read -r l; do
    l="${l//"$S3_SECRET"/<redacted>}"; l="${l//"$S3_KEY_ID"/<redacted>}"
    printf '  rclone: %s\n' "$l" >&2
  done < "$WORK/rclone.err"
  : > "$WORK/rclone.err"
}

# s3_copy <archive>: encrypt, checksum, upload both objects, compare sizes.
s3_copy() {
  local out="$1" name enc rc listing want_enc want_sum got_enc got_sum
  name="$(basename "$out").age"
  enc="$WORK/$name"
  age "${age_args[@]}" -o "$enc.part" "$out"
  rc=$?
  if [ "$rc" -ne 0 ] || [ ! -s "$enc.part" ]; then
    echo "FAILED: off-host copy of $out to $S3_LABEL: encryption failed (age exit $rc), nothing uploaded" >&2
    rm -f "$enc.part"; return 1
  fi
  mv "$enc.part" "$enc" || { echo "FAILED: off-host copy of $out to $S3_LABEL: could not write the ciphertext" >&2; return 1; }
  printf '%s  %s\n' "$(sha "$enc")" "$name" > "$enc.sha256" \
    || { echo "FAILED: off-host copy of $out to $S3_LABEL: could not write the checksum" >&2; rm -f "$enc" "$enc.sha256"; return 1; }
  for f in "$enc" "$enc.sha256"; do
    rcl copyto --no-check-dest "$f" "s3backup:$S3_BUCKET/$S3_PREFIX/$(basename "$f")"
    rc=$?
    if [ "$rc" -ne 0 ]; then
      echo "FAILED: off-host copy of $out to $S3_LABEL (rclone exit $rc uploading $(basename "$f"))" >&2
      show_rclone_err; rm -f "$enc" "$enc.sha256"; return 1
    fi
  done
  want_enc="$(fsize "$enc")"; want_sum="$(fsize "$enc.sha256")"
  listing="$(rcl lsf --files-only --max-depth 1 --format sp --separator ' ' \
    --include "/$name" --include "/$name.sha256" "s3backup:$S3_BUCKET/$S3_PREFIX/")"
  rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "FAILED: off-host copy of $out to $S3_LABEL (rclone exit $rc listing the upload for the size check)" >&2
    show_rclone_err; rm -f "$enc" "$enc.sha256"; return 1
  fi
  got_enc="$(printf '%s\n' "$listing" | awk -v n="$name" '$2 == n {print $1}')"
  got_sum="$(printf '%s\n' "$listing" | awk -v n="$name.sha256" '$2 == n {print $1}')"
  if [ "$got_enc" != "$want_enc" ] || [ "$got_sum" != "$want_sum" ]; then
    echo "FAILED: off-host copy of $out to $S3_LABEL: size check failed (remote ${got_enc:-missing}/${got_sum:-missing} bytes, local $want_enc/$want_sum)" >&2
    rm -f "$enc" "$enc.sha256"; return 1
  fi
  show_rclone_err
  rm -f "$enc" "$enc.sha256"
  echo "copied off-host to $S3_LABEL/$name (encrypted, $want_enc bytes, size verified)"
}

umask 077
mkdir -p "$DIR" || exit 1

WORK=""
if [ "$s3_on" -eq 1 ]; then
  if s3_config; then
    # The work directory only ever holds ciphertext and checksums.
    trap '[ -n "$WORK" ] && rm -rf "$WORK"' EXIT
    trap 'exit 1' INT TERM HUP
    WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm_connect_backup.XXXXXX")" && : > "$WORK/rclone.conf" \
      || s3_bad "could not create a temporary directory"
  fi
fi
s3_tried=0

for src in $SOURCES; do
  if [ ! -d "$src" ]; then echo "skip: $src does not exist"; continue; fi
  name="$(basename "$src")"
  out="$DIR/${name}-${STAMP}.tar.gz"
  # State files are written atomically (temp file + rename), so a finished file
  # is always consistent; leave the in-flight temp files out. The audit log is
  # append-only, so a copy taken mid-append is at worst missing its last line.
  tar -czf "$out.part" --exclude='.tmp-*' --exclude='*.tmp' -C "$(dirname "$src")" "$name"
  rc=$?
  if [ "$rc" -gt 1 ]; then
    echo "FAILED: archiving $src (tar exit $rc)" >&2
    rm -f "$out.part"; failed=1; continue
  fi
  [ "$rc" -eq 1 ] && echo "warning: a file under $src changed while it was archived (tar exit 1)"
  mv "$out.part" "$out"
  echo "backed up $src -> $out ($(du -h "$out" | cut -f1))"
  if [ -n "${BACKUP_REMOTE:-}" ]; then
    if command -v rsync >/dev/null 2>&1; then rsync -a "$out" "$BACKUP_REMOTE/"; else scp -q "$out" "$BACKUP_REMOTE/"; fi
    if [ $? -ne 0 ]; then echo "FAILED: off-host copy of $out to $BACKUP_REMOTE" >&2; failed=1
    else echo "copied off-host to $BACKUP_REMOTE"; fi
  fi
  if [ "$s3_on" -eq 1 ]; then
    s3_tried=1
    if [ -n "$s3_err" ]; then
      echo "FAILED: off-host copy of $out to S3: $s3_err" >&2; failed=1
    elif ! s3_copy "$out"; then
      failed=1
    fi
  fi
done

if [ "$s3_on" -eq 1 ] && [ -n "$s3_err" ]; then
  [ "$s3_tried" -eq 1 ] || echo "FAILED: off-host copy to S3: $s3_err" >&2
  failed=1
fi

# Only this script's own archives, only in $DIR itself.
find "$DIR" -maxdepth 1 -type f \( -name 'swarm_connect*_data-*.tar.gz' -o -name '*.tar.gz.part' \) \
  -mtime +"$KEEP" -print -delete

if [ "$failed" -eq 0 ]; then
  touch "$DIR/.last_success"
fi
exit "$failed"
