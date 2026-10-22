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
#   BEE_KEYS_SSH_TIMEOUT    ssh ConnectTimeout in seconds for rsync/scp (default: 20)
#
# BEE_KEYS_REMOTE, BACKUP_REMOTE and the two directories must be absolute paths.
# They are compared after dropping user@, lower-casing the host and collapsing
# //, /./ and a trailing /. That is best effort: an ssh alias and the host name
# it points to cannot be told apart, so keep the two destinations distinct.
# Only one run at a time holds $BEE_KEYS_DIR/.lock; an overlapping run fails.
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

NODES="${BEE_KEYS_NODES:-prod:swarm_connect_bee-data dev:swarm_connect_bee-dev-data}"
DIR="${BEE_KEYS_DIR:-/var/backups/swarm_connect_bee_keys}"
STATE_DIR="${BACKUP_DIR:-/var/backups/swarm_connect}"
KEEP="${BEE_KEYS_KEEP:-3}"
IMAGE="${BEE_KEYS_HELPER_IMAGE:-alpine:3.20}"
REMOTE="${BEE_KEYS_REMOTE:-}"
RECIPIENTS="${BEE_KEYS_AGE_RECIPIENT:-}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
failed=0

refuse() { echo "REFUSED: $*" >&2; exit 2; }
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
age_args=()
for r in $RECIPIENTS; do
  case "$r" in
    AGE-SECRET-KEY-*) refuse "BEE_KEYS_AGE_RECIPIENT holds an age SECRET key; the host must only hold the public recipient (age1...)" ;;
    age1*) age_args+=(-r "$r") ;;
    *) refuse "BEE_KEYS_AGE_RECIPIENT entry is not an age public recipient (age1...)" ;;
  esac
done
case "$KEEP" in ''|*[!0-9]*|0) refuse "BEE_KEYS_KEEP must be a whole number of at least 1, got '$KEEP'" ;; esac
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
for pair in $NODES; do
  case "$pair" in *?:?*) ;; *) refuse "BEE_KEYS_NODES entry '$pair' is not node:volume" ;; esac
  case "${pair%%:*}" in *[!A-Za-z0-9_-]*) refuse "BEE_KEYS_NODES node name '${pair%%:*}' may only use A-Z, a-z, 0-9, _ and -" ;; esac
done

if command -v sha256sum >/dev/null 2>&1; then sha() { sha256sum "$1" | cut -d' ' -f1; }
else sha() { shasum -a 256 "$1" | cut -d' ' -f1; }; fi
SSH_OPTS="-o ConnectTimeout=${BEE_KEYS_SSH_TIMEOUT:-20} -o BatchMode=yes"
copy() {  # copy <dest> <src>...  (rsync, else scp)
  local dest="$1"; shift
  if command -v rsync >/dev/null 2>&1; then rsync -a -e "ssh $SSH_OPTS" "$@" "$dest"
  else scp -q $SSH_OPTS "$@" "$dest"; fi
}

umask 077
mkdir -p "$DIR" || exit 1

# One run at a time per BEE_KEYS_DIR (mkdir is atomic; flock is not on every
# target). A lock whose process is gone is taken over.
LOCK="$DIR/.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  holder="$(cat "$LOCK/pid" 2>/dev/null)"
  if [ -n "$holder" ] && kill -0 "$holder" 2>/dev/null; then
    echo "FAILED: another run (pid $holder) holds $LOCK" >&2; exit 1
  fi
  echo "warning: taking over a stale lock (pid ${holder:-unknown})"
  rm -rf "$LOCK"; mkdir "$LOCK" || { echo "FAILED: could not take $LOCK" >&2; exit 1; }
fi
echo "$$" > "$LOCK/pid"
WORK=""
# The work dir only ever holds checksum lists and ciphertext, never plaintext keys.
cleanup() { [ -n "$WORK" ] && rm -rf "$WORK"; rm -f "$DIR"/*.part; rm -rf "$LOCK"; }
trap cleanup EXIT
trap 'exit 1' INT TERM HUP
WORK="$(mktemp -d "${TMPDIR:-/tmp}/bee_keys.XXXXXX")" || exit 1

# Checksums of the plaintext keystore files, read inside the helper container.
live_manifest() {  # live_manifest <volume> <out>
  docker run --rm -v "$1:/src:ro" "$IMAGE" sh -c 'cd /src/keys && sha256sum -- *' > "$2" && [ -s "$2" ]
}

for pair in $NODES; do
  node="${pair%%:*}"; vol="${pair#*:}"
  record="$DIR/$node.manifest"; last="$DIR/$node.last"
  if ! docker volume inspect "$vol" >/dev/null 2>&1; then
    echo "FAILED: $node: volume $vol does not exist" >&2; failed=1; continue
  fi
  man="$WORK/$node.manifest"
  if ! live_manifest "$vol" "$man"; then
    echo "FAILED: $node: could not read keystore checksums from $vol" >&2; failed=1; continue
  fi

  if [ "$MODE" = verify ]; then
    if [ ! -f "$record" ] || [ ! -f "$last" ]; then
      echo "FAILED: $node: no recorded backup to verify; run without --verify first" >&2; failed=1; continue
    fi
    if ! cmp -s "$man" "$record"; then
      echo "FAILED: $node: checksum mismatch, live keystores differ from the backed-up copy" >&2; failed=1; continue
    fi
    read -r want name < "$last"
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

  if [ -f "$record" ] && [ -f "$last" ] && cmp -s "$man" "$record"; then
    echo "unchanged: $node ($vol), nothing uploaded"; continue
  fi

  base="bee-keys-${node}-${STAMP}"
  out="$DIR/$base.tar.age"
  docker run --rm -v "$vol:/src:ro" "$IMAGE" tar -C /src -cf - keys | age "${age_args[@]}" -o "$out.part"
  rc=("${PIPESTATUS[@]}")
  if [ "${rc[0]}" -ne 0 ] || [ "${rc[1]}" -ne 0 ]; then
    echo "FAILED: $node: archiving or encrypting $vol (docker exit ${rc[0]}, age exit ${rc[1]})" >&2
    rm -f "$out.part"; failed=1; continue
  fi
  if ! mv "$out.part" "$out" || ! cp "$man" "$DIR/$base.sha256"; then
    echo "FAILED: $node: could not write $out or its manifest" >&2
    rm -f "$out.part" "$out" "$DIR/$base.sha256"; failed=1; continue
  fi
  cipher="$(sha "$out")"
  echo "encrypted $node ($vol) -> $out"
  if ! copy "$REMOTE/" "$out" "$DIR/$base.sha256"; then
    echo "FAILED: off-host copy of $out to $BEE_KEYS_REMOTE" >&2; failed=1; continue
  fi
  cp "$man" "$record" && printf '%s %s\n' "$cipher" "$base.tar.age" > "$last" || {
    echo "FAILED: $node: could not update the local record" >&2; failed=1; continue; }
  echo "copied off-host to $BEE_KEYS_REMOTE"

  # Keep the newest $KEEP encrypted archives (and their manifests) for this node.
  # Names sort by their UTC stamp.
  ls "$DIR"/bee-keys-"$node"-[0-9]*.tar.age 2>/dev/null | sort -r | tail -n +"$((KEEP + 1))" | while read -r old; do
    rm -f "$old" "${old%.tar.age}.sha256"; echo "pruned $old"
  done
done

if [ "$failed" -eq 0 ]; then
  if [ "$MODE" = verify ]; then touch "$DIR/.last_verify"; else touch "$DIR/.last_success"; fi
fi
exit "$failed"
