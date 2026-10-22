#!/usr/bin/env bash
# Back up Bee node identity (#494): the keystore files in /home/bee/.bee/keys of
# each Bee volume (Ethereum key = the node's wallet, libp2p key, PSS key).
#
# This is a separate backup class from gateway state (scripts/backup_state.sh),
# with its own settings, recipient key, destination and cron lines:
#   - only the keys/ directory is read; never localstore, statestore or chain data;
#   - the volume is read live and read-only (docker run -v <volume>:/src:ro), the
#     node keeps running;
#   - the tar stream goes straight into `age`, so no plaintext copy of a keystore
#     is ever written to disk, here or remotely;
#   - the host holds only the PUBLIC age recipient; the keystore PASSWORDS are
#     never read by this script and belong in the secret manager.
#
# Install outside the deployed checkout (every deploy re-clones it), e.g.:
#   install -m 0700 scripts/backup_bee_keys.sh /usr/local/sbin/swarm_connect_backup_bee_keys
# Cron lines: see docs/mainnet-runbook.md §4 (an "on change" run and a weekly --verify).
#
# Settings live in one root-only file, /etc/swarm_connect/bee_keys.env, so cron
# and manual runs use the same values. It is parsed, not sourced: KEY=VALUE lines
# for the settings below, blank lines and # comments only, values literal and
# optionally in one pair of quotes (see load_env). BEE_KEYS_ENV_FILE names
# another absolute path; set it empty to read none. Values in the file override
# the environment. The script refuses a symlink, a non-regular file, a file not
# owned by the user running it, and a file or directory that group or others
# can write. The file holds only public values (the age recipient is the PUBLIC
# half), never a secret key.
#
# Usage: backup_bee_keys.sh            back up every node whose keys changed
#        backup_bee_keys.sh --verify   weekly re-check, uploads nothing
#
# Environment:
#   BEE_KEYS_REMOTE         REQUIRED. rsync/scp destination for the encrypted archives,
#                           e.g. backup@<backup-host>:/backups/bee_keys. Must not equal or
#                           nest with BACKUP_REMOTE (the gateway-state destination).
#   BEE_KEYS_AGE_RECIPIENT  REQUIRED. One or more space-separated age PUBLIC recipients
#                           (age1...). A dedicated key, not the gateway-state key.
#   BACKUP_REMOTE           the gateway-state destination; set it here too so the script
#                           can refuse a shared destination (it warns if it is unset).
#   BEE_KEYS_NODES          node:volume pairs (default: prod:swarm_connect_bee-data
#                           dev:swarm_connect_bee-dev-data, the compose volume names)
#   BEE_KEYS_DIR            local directory for encrypted archives and checksum records
#                           (default: /var/backups/swarm_connect_bee_keys)
#   BACKUP_DIR              the gateway-state directory, compared like BACKUP_REMOTE
#                           (default: /var/backups/swarm_connect)
#   BEE_KEYS_KEEP           encrypted archives kept locally per node (default: 3)
#   BEE_KEYS_HELPER_IMAGE   image used to read the volume (default: alpine:3.20)
#   BEE_KEYS_SSH_TIMEOUT    ssh ConnectTimeout in seconds for rsync/scp (default: 20);
#                           ServerAliveInterval=15 x 4 also ends a connection that stalls
#   BEE_KEYS_ENV_FILE       settings file to source (default: /etc/swarm_connect/bee_keys.env;
#                           empty: none)
#
# BEE_KEYS_REMOTE, BACKUP_REMOTE and the two directories must be absolute paths.
# They are compared after dropping user@, lower-casing the host and collapsing
# //, /./ and a trailing /. That is best effort: an ssh alias and the host name
# it points to cannot be told apart, so keep the two destinations distinct.
# Only one run at a time holds $BEE_KEYS_DIR/.lock (util-linux flock, required);
# an overlapping run fails. A new destination or a new set of recipients counts
# as a change and uploads a fresh copy.
#
# On change: the live keystore checksums are compared with those recorded at the
# last successful upload; unchanged nodes upload nothing. The record is updated
# only after the off-host copy succeeded, so a failed copy is retried next run.
#
# --verify: fails if the live keystores differ from the recorded checksums, or if
# the remote ciphertext (fetched back, still encrypted) differs from the recorded
# checksum. No decryption key is needed or used.
#
# Exit status: 0 on full success, 1 if any node failed (the others are still
# processed; each failure logs a `FAILED:` line), 2 for a configuration refusal.
# A full backup run touches $BEE_KEYS_DIR/.last_success, a full --verify run
# touches $BEE_KEYS_DIR/.last_verify; neither is touched on any failure.
set -u
set -o pipefail

MODE=backup
case "${1:-}" in
  '') ;;
  --verify) MODE=verify ;;
  *) echo "usage: $0 [--verify]" >&2; exit 2 ;;
esac

refuse() { echo "REFUSED: $*" >&2; exit 2; }

ENV_FILE="${BEE_KEYS_ENV_FILE-/etc/swarm_connect/bee_keys.env}"
ENV_KEYS="BEE_KEYS_REMOTE BEE_KEYS_AGE_RECIPIENT BEE_KEYS_NODES BEE_KEYS_DIR BEE_KEYS_KEEP BEE_KEYS_HELPER_IMAGE BEE_KEYS_SSH_TIMEOUT BACKUP_REMOTE BACKUP_DIR"
# load_env <file>: read KEY=VALUE lines. The file is parsed, never sourced, so
# nothing in it runs. Allowed: blank lines, # comments, and KEY=VALUE where KEY
# is one of $ENV_KEYS and VALUE is one word, or is wrapped in one pair of single
# or double quotes. Values are literal: $, ` and \ are refused, nothing is
# expanded. Any other line is refused by number, without echoing its value.
load_env() {
  local f="$1" n=0 line key val k known seen=" "
  while IFS= read -r line || [ -n "$line" ]; do
    n=$((n + 1))
    line="${line%$'\r'}"
    case "${line#"${line%%[![:space:]]*}"}" in ''|'#'*) continue ;; esac
    case "$line" in
      *=*) key="${line%%=*}"; val="${line#*=}" ;;
      *) refuse "$f line $n is not KEY=VALUE" ;;
    esac
    case "$key" in
      ''|[!A-Z_]*|*[!A-Z0-9_]*) refuse "$f line $n is not KEY=VALUE (a key is A-Z, 0-9 and _)" ;;
    esac
    known=""
    for k in $ENV_KEYS; do [ "$k" = "$key" ] && known=1; done
    [ -n "$known" ] || refuse "$f line $n: '$key' is not a known setting"
    case "$seen" in *" $key "*) refuse "$f line $n: '$key' is set twice" ;; esac
    seen="$seen$key "
    case "$val" in
      \"*\") val="${val#\"}"; val="${val%\"}"; case "$val" in *\"*) refuse "$f line $n: malformed quoting" ;; esac ;;
      \'*\') val="${val#\'}"; val="${val%\'}"; case "$val" in *\'*) refuse "$f line $n: malformed quoting" ;; esac ;;
      *[[:space:]\"\']*) refuse "$f line $n: quote a value that contains spaces or quotes" ;;
    esac
    case "$val" in *[\$\`\\]*) refuse "$f line $n: \$, \` and \\ are not allowed; values are literal" ;; esac
    printf -v "$key" '%s' "$val" 2>/dev/null || refuse "$f line $n: could not set '$key'"
    export "$key" || refuse "$f line $n: could not set '$key'"
  done < "$f"
}
if [ -n "$ENV_FILE" ]; then
  case "$ENV_FILE" in /*) ;; *) refuse "BEE_KEYS_ENV_FILE must be an absolute path" ;; esac
  if [ -L "$ENV_FILE" ]; then
    refuse "$ENV_FILE is a symlink; point BEE_KEYS_ENV_FILE at the real file"
  elif [ -e "$ENV_FILE" ]; then
    [ -f "$ENV_FILE" ] || refuse "$ENV_FILE is not a regular file"
    [ -n "$(find "$ENV_FILE" -user "$(id -u)")" ] || refuse "$ENV_FILE is not owned by uid $(id -u)"
    [ -z "$(find "$ENV_FILE" \( -perm -020 -o -perm -002 \))" ] || refuse "$ENV_FILE is writable by group or others; chmod 600 it"
    # The file's own directory must be owned by this user or root and not be
    # writable by others. The directories above it are assumed to be root-owned
    # system paths (/etc).
    env_dir="$(dirname "$ENV_FILE")"
    [ -n "$(find "$env_dir" -maxdepth 0 \( -user "$(id -u)" -o -user 0 \))" ] || refuse "the directory $env_dir is not owned by uid $(id -u) or root"
    [ -z "$(find "$env_dir" -maxdepth 0 \( -perm -020 -o -perm -002 \))" ] || refuse "the directory $env_dir is writable by group or others; chmod 700 it"
    load_env "$ENV_FILE"
  elif [ -n "${BEE_KEYS_ENV_FILE:-}" ]; then
    refuse "BEE_KEYS_ENV_FILE names $ENV_FILE, which does not exist"
  fi
fi

NODES="${BEE_KEYS_NODES:-prod:swarm_connect_bee-data dev:swarm_connect_bee-dev-data}"
DIR="${BEE_KEYS_DIR:-/var/backups/swarm_connect_bee_keys}"
STATE_DIR="${BACKUP_DIR:-/var/backups/swarm_connect}"
KEEP="${BEE_KEYS_KEEP:-3}"
IMAGE="${BEE_KEYS_HELPER_IMAGE:-alpine:3.20}"
REMOTE="${BEE_KEYS_REMOTE:-}"
RECIPIENTS="${BEE_KEYS_AGE_RECIPIENT:-}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
failed=0

# canon <dest>: print "host|/path" for a local absolute path or an rsync/scp
# [user@]host:/absolute/path, with the user dropped, the host lower-cased, and
# //, /./ and a trailing / or /. collapsed. Fails for a relative or empty path,
# an empty host, rsync daemon syntax (host::module) or a .. segment, because
# those cannot be compared reliably. Best effort only: an ssh alias and the
# host name it points to look different here, so keep the paths distinct.
canon() {
  local s="$1" host="" path
  case "$s" in
    /*) path="$s" ;;
    *:*) host="${s%%:*}"; path="${s#*:}"
         case "$host" in ''|*/*) return 1 ;; esac
         host="$(printf '%s' "${host##*@}" | tr 'A-Z' 'a-z')"
         [ -n "$host" ] || return 1 ;;
    *) return 1 ;;
  esac
  case "$path" in /*) ;; *) return 1 ;; esac
  while :; do
    case "$path" in
      *//*) path="${path%%//*}/${path#*//}" ;;
      */./*) path="${path%%/./*}/${path#*/./}" ;;
      */.) path="${path%/.}" ;;
      ?*/) path="${path%/}" ;;
      *) break ;;
    esac
  done
  [ -n "$path" ] || path=/
  case "$path/" in */../*) return 1 ;; esac
  printf '%s|%s' "$host" "$path"
}
# overlaps <canon-a> <canon-b>: same host and one path equal to or inside the other.
overlaps() {
  local ha="${1%%|*}" pa="${1#*|}" hb="${2%%|*}" pb="${2#*|}"
  [ "$ha" = "$hb" ] || return 1
  [ "$pa" = / ] || [ "$pb" = / ] || [ "$pa" = "$pb" ] && return 0
  case "$pa/" in "$pb/"*) return 0 ;; esac
  case "$pb/" in "$pa/"*) return 0 ;; esac
  return 1
}

# --- configuration checks: nothing is read or written before these pass ---
[ -n "$REMOTE" ] || refuse "BEE_KEYS_REMOTE is not set; a keystore backup without an off-host copy is lost with the host"
[ -n "$RECIPIENTS" ] || refuse "BEE_KEYS_AGE_RECIPIENT is not set"
# Split the lists into arrays with globbing off, and refuse glob characters
# outright, so nothing can expand against the working directory.
case "$RECIPIENTS$NODES" in *[\*\?\[]*) refuse "BEE_KEYS_AGE_RECIPIENT and BEE_KEYS_NODES may not contain *, ? or [" ;; esac
set -f
recips=($RECIPIENTS)
nodes=($NODES)
set +f
[ "${#recips[@]}" -gt 0 ] || refuse "BEE_KEYS_AGE_RECIPIENT is blank"
[ "${#nodes[@]}" -gt 0 ] || refuse "BEE_KEYS_NODES is blank"
age_args=()
for r in "${recips[@]}"; do
  case "$r" in
    AGE-SECRET-KEY-*) refuse "BEE_KEYS_AGE_RECIPIENT holds an age SECRET key; the host must only hold the public recipient (age1...)" ;;
    age1*) age_args+=(-r "$r") ;;
    *) refuse "BEE_KEYS_AGE_RECIPIENT entry is not an age public recipient (age1...)" ;;
  esac
done
case "$KEEP" in ''|*[!0-9]*|0*) refuse "BEE_KEYS_KEEP must be a whole number of at least 1 (no leading zero), got '$KEEP'" ;; esac
SSH_TIMEOUT="${BEE_KEYS_SSH_TIMEOUT:-20}"
case "$SSH_TIMEOUT" in ''|*[!0-9]*|0*) refuse "BEE_KEYS_SSH_TIMEOUT must be a number of seconds from 1 to 3600" ;; esac
[ "${#SSH_TIMEOUT}" -le 4 ] && [ "$SSH_TIMEOUT" -le 3600 ] || refuse "BEE_KEYS_SSH_TIMEOUT must be a number of seconds from 1 to 3600"
c_recips="$(printf '%s\n' "${recips[@]}" | LC_ALL=C sort -u | tr '\n' ' ')"
c_remote="$(canon "$REMOTE")" || refuse "BEE_KEYS_REMOTE must be an absolute path: /path or [user@]host:/path (got '$REMOTE')"
if [ -n "${BACKUP_REMOTE:-}" ]; then
  c_state="$(canon "$BACKUP_REMOTE")" || refuse "BACKUP_REMOTE is not an absolute path, so the separation cannot be checked; pass its absolute form (/path or [user@]host:/path)"
  overlaps "$c_remote" "$c_state" && refuse "BEE_KEYS_REMOTE and BACKUP_REMOTE are the same destination or nested; keystores must go to a separate path"
else
  echo "warning: BACKUP_REMOTE is not set, so BEE_KEYS_REMOTE could not be checked against the gateway-state destination"
fi
c_dir="$(canon "$DIR")" && [ "${c_dir%%|*}" = "" ] || refuse "BEE_KEYS_DIR must be an absolute local path"
c_sdir="$(canon "$STATE_DIR")" && [ "${c_sdir%%|*}" = "" ] || refuse "BACKUP_DIR must be an absolute local path"
overlaps "$c_dir" "$c_sdir" && refuse "BEE_KEYS_DIR and BACKUP_DIR are the same directory or nested"
seen=" "
for pair in "${nodes[@]}"; do
  case "$pair" in *?:?*) ;; *) refuse "BEE_KEYS_NODES entry '$pair' is not node:volume" ;; esac
  # A Docker volume NAME, never a path: "prod:/" would bind-mount the host's /.
  case "${pair#*:}" in
    ''|[!A-Za-z0-9]*|*[!A-Za-z0-9_.-]*) refuse "BEE_KEYS_NODES volume '${pair#*:}' is not a Docker volume name ([A-Za-z0-9][A-Za-z0-9_.-]*)" ;;
  esac
  case "$seen" in *" ${pair%%:*} "*) refuse "BEE_KEYS_NODES names node '${pair%%:*}' twice (duplicate)" ;; esac
  seen="$seen${pair%%:*} "
  case "${pair%%:*}" in *[!A-Za-z0-9_-]*) refuse "BEE_KEYS_NODES node name '${pair%%:*}' may only use A-Z, a-z, 0-9, _ and -" ;; esac
done

if command -v sha256sum >/dev/null 2>&1; then sha() { sha256sum "$1" | cut -d' ' -f1; }
else sha() { shasum -a 256 "$1" | cut -d' ' -f1; }; fi
SSH_OPTS="-o ConnectTimeout=$SSH_TIMEOUT -o BatchMode=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=4"
copy() {  # copy <dest> <src>...  (rsync, else scp)
  local dest="$1"; shift
  if command -v rsync >/dev/null 2>&1; then rsync -a -e "ssh $SSH_OPTS" "$@" "$dest" 9>&-
  else scp -q $SSH_OPTS "$@" "$dest" 9>&-; fi
}

command -v flock >/dev/null 2>&1 || refuse "flock (util-linux) is required; apt-get install util-linux"

umask 077
mkdir -p "$DIR" || exit 1

# One run at a time per BEE_KEYS_DIR, with util-linux flock(1) on fd 9. The
# kernel drops the lock when the last holder of fd 9 exits, however it exits.
# Every long-running child (docker, age, rsync/scp and their ssh) is started
# with 9>&-, so only this script holds it: a stray child that outlives the
# script cannot keep the lock.
exec 9>>"$DIR/.lock" || { echo "FAILED: cannot open $DIR/.lock" >&2; exit 1; }
if ! flock -n 9; then
  echo "FAILED: another run holds $DIR/.lock" >&2; exit 1
fi
WORK=""
# The work dir only ever holds checksum lists and ciphertext, never plaintext keys.
cleanup() {
  [ -n "$WORK" ] && rm -rf "$WORK"
  rm -f "$DIR"/*.part
}
trap cleanup EXIT
trap 'exit 1' INT TERM HUP
WORK="$(mktemp -d "${TMPDIR:-/tmp}/bee_keys.XXXXXX")" || exit 1

# Runs in the helper container. It copies keys/ once into the container's
# tmpfs (/dev/shm: memory, gone with the container), and takes both the
# manifest and the archive from that one copy, so they always describe the
# same files. Every regular file counts, dotfiles included; any other entry
# (link, directory, device) fails the node because a restore would refuse it.
# Manifest lines: "<sha256>  <name relative to keys/>", byte-sorted by name.
# "$1" = manifest: print the manifest. "$1" = tar: write the tar to stdout and
# the manifest to stderr, each line prefixed "MANIFEST ".
read -r -d '' SNAP <<'SH'
set -eu
mkdir /dev/shm/k
cp -a /src/keys /dev/shm/k/keys
cd /dev/shm/k/keys
if [ -n "$(find . ! -type f ! -path .)" ]; then echo "keys/ holds an entry that is not a regular file" >&2; exit 5; fi
m="$(find . -type f | sed 's|^\./||' | LC_ALL=C sort | while IFS= read -r f; do sha256sum -- "$f"; done)"
[ -n "$m" ] || { echo "keys/ is empty" >&2; exit 6; }
if [ "$1" = tar ]; then
  printf '%s\n' "$m" | sed 's/^/MANIFEST /' >&2
  cd /dev/shm/k && tar -cf - keys
else
  printf '%s\n' "$m"
fi
SH

# Every helper run whose output carries keystore bytes or the manifest uses
# --log-driver none: the default json-file driver would otherwise copy the
# container's stdout (the plaintext tar) to /var/lib/docker/containers/ on the
# host disk, and journald or syslog would keep or ship it.
live_manifest() {  # live_manifest <volume> <out>
  docker run --rm --log-driver none --network none -v "$1:/src:ro" "$IMAGE" sh -c "$SNAP" sh manifest > "$2" 9>&- && [ -s "$2" ]
}
split_err() {  # split_err <stderr file> <manifest out>: manifest lines out, the rest to stderr
  sed -n 's/^MANIFEST //p' "$1" > "$2"
  grep -v '^MANIFEST ' "$1" >&2 || true
}

# prune <node> <archive just written>: keep the newest $KEEP encrypted archives
# (and their manifests) for this node, and always the one just written. Runs
# after failed copies too, so repeated failures cannot fill the disk. Names sort
# by their UTC stamp; the glob matches only this script's exact stamp, so a
# node whose name extends this one (prod-2) is never touched.
prune() {
  ls "$DIR"/bee-keys-"$1"-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z.tar.age 2>/dev/null \
    | sort -r | tail -n +"$((KEEP + 1))" | while read -r old; do
      [ "$old" = "$2" ] && continue
      rm -f "$old" "${old%.tar.age}.sha256"; echo "pruned $old"
    done
}

for pair in "${nodes[@]}"; do
  node="${pair%%:*}"; vol="${pair#*:}"
  record="$DIR/$node.manifest"; last="$DIR/$node.last"
  if ! docker volume inspect "$vol" >/dev/null 2>&1 9>&-; then
    echo "FAILED: $node: volume $vol does not exist" >&2; failed=1; continue
  fi
  man="$WORK/$node.manifest"
  if [ "$MODE" = verify ] && ! live_manifest "$vol" "$man"; then
    echo "FAILED: $node: could not read keystore checksums from $vol" >&2; failed=1; continue
  fi

  if [ "$MODE" = verify ]; then
    if [ ! -f "$record" ] || [ ! -f "$last" ]; then
      echo "FAILED: $node: no recorded backup to verify; run without --verify first" >&2; failed=1; continue
    fi
    if ! cmp -s "$man" "$record"; then
      echo "FAILED: $node: checksum mismatch, live keystores differ from the backed-up copy" >&2; failed=1; continue
    fi
    { read -r want name; read -r rec_remote; } < "$last"
    if [ "$rec_remote" != "$c_remote" ]; then
      echo "FAILED: $node: the last upload went to another destination than BEE_KEYS_REMOTE; run without --verify to upload here" >&2; failed=1; continue
    fi
    fetched="$WORK/fetch-$node"; mkdir -p "$fetched"
    if ! copy "$fetched/" "$REMOTE/$name" "$REMOTE/${name%.tar.age}.sha256"; then
      echo "FAILED: $node: could not fetch $name from $BEE_KEYS_REMOTE" >&2; failed=1; continue
    fi
    if [ "$(sha "$fetched/$name")" != "$want" ]; then
      echo "FAILED: $node: checksum mismatch, remote $name differs from the uploaded ciphertext" >&2; failed=1; continue
    fi
    if ! cmp -s "$fetched/${name%.tar.age}.sha256" "$record"; then
      echo "FAILED: $node: checksum mismatch, remote manifest differs from the recorded one" >&2; failed=1; continue
    fi
    echo "verified $node: live keystores and remote $name match the record"
    continue
  fi

  base="bee-keys-${node}-${STAMP}"
  out="$DIR/$base.tar.age"
  # One snapshot gives both the archive (stdout, straight into age) and its
  # manifest (stderr), so they always match.
  docker run --rm --log-driver none --network none -v "$vol:/src:ro" "$IMAGE" sh -c "$SNAP" sh tar 2> "$WORK/$node.err" 9>&- \
    | age "${age_args[@]}" -o "$out.part" 9>&-
  rc=("${PIPESTATUS[@]}")
  split_err "$WORK/$node.err" "$man"
  if [ "${rc[0]}" -ne 0 ] || [ "${rc[1]}" -ne 0 ] || [ ! -s "$man" ]; then
    echo "FAILED: $node: archiving or encrypting $vol (docker exit ${rc[0]}, age exit ${rc[1]})" >&2
    rm -f "$out.part"; failed=1; continue
  fi
  rec_remote=""; rec_recips=""
  [ -f "$last" ] && { read -r _ _; read -r rec_remote; read -r rec_recips; } < "$last"
  if [ -f "$record" ] && cmp -s "$man" "$record" && [ "$rec_remote" = "$c_remote" ] && [ "$rec_recips" = "${c_recips% }" ]; then
    rm -f "$out.part"
    echo "unchanged: $node ($vol), nothing uploaded"; continue
  fi
  if ! mv "$out.part" "$out" || ! cp "$man" "$DIR/$base.sha256"; then
    echo "FAILED: $node: could not write $out or its manifest" >&2
    rm -f "$out.part" "$out" "$DIR/$base.sha256"; failed=1; continue
  fi
  cipher="$(sha "$out")"
  echo "encrypted $node ($vol) -> $out"
  if ! copy "$REMOTE/" "$out" "$DIR/$base.sha256"; then
    echo "FAILED: off-host copy of $out to $BEE_KEYS_REMOTE" >&2; failed=1
    prune "$node" "$out"; continue
  fi
  { cp "$man" "$record" && printf '%s %s\n%s\n%s\n' "$cipher" "$base.tar.age" "$c_remote" "${c_recips% }" > "$last"; } || {
    echo "FAILED: $node: could not update the local record" >&2; failed=1; prune "$node" "$out"; continue; }
  echo "copied off-host to $BEE_KEYS_REMOTE"
  prune "$node" "$out"
done

if [ "$failed" -eq 0 ]; then
  if [ "$MODE" = verify ]; then touch "$DIR/.last_verify"; else touch "$DIR/.last_success"; fi
fi
exit "$failed"
