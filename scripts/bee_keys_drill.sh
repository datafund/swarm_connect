#!/usr/bin/env bash
# Local restore drill for the Bee keystore backup (#494). Uses a THROWAWAY Bee
# identity in local Docker only: nothing here touches a real node or a real key.
# Every Bee container and every helper that sees keystore bytes runs with
# --network none. Pulling the images, and the two alpine containers of the
# step-10 log-driver probe (which see no keys), may use Docker's default network.
#
# Steps: create an identity with a mined (non-zero) overlay nonce -> back it up with backup_bee_keys.sh while the node
# runs (read-only mount) -> verify -> confirm a second run uploads nothing ->
# wipe the node and its volume -> decrypt and restore with restore_bee_keys.sh
# into a fresh volume -> show that a networkless start without the overlay hint
# fails -> check that a fresh networkless container reports the same Ethereum
# address and overlay, and that the nonce is reported non-zero. Every helper run
# that carries keystore bytes must use --log-driver none (checked from the
# recorded docker argv, and shown against the daemon) -> restart without
# the hint and check the overlay again. Also: a truncated restore leaves the
# volume empty and can be retried, and `check` flags a nonce-0 identity.
#
# Needs: docker, age, age-keygen. Run: scripts/bee_keys_drill.sh
# Or via pytest: RUN_BEE_DRILL=1 python -m pytest tests/test_bee_keys_drill.py -s
# Exit 0 only if every step passed.
set -u
set -o pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
BEE_IMAGE="${BEE_IMAGE:-ethersphere/bee:${BEE_VERSION:-2.8.1}}"
CURL="${BEE_KEYS_CURL_IMAGE:-curlimages/curl:8.10.1}"
export BEE_IMAGE
for t in docker age age-keygen; do command -v "$t" >/dev/null || { echo "drill needs $t on PATH" >&2; exit 2; }; done

ID="beedrill$$"
SRC_VOL="${ID}-src"; DST_VOL="${ID}-dst"; NEG_VOL="${ID}-neg"; ZERO_VOL="${ID}-zero"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/bee_keys_drill.XXXXXX")"
# Record every docker invocation (the scripts' included) to check their flags.
REAL_DOCKER="$(command -v docker)"
mkdir "$WORK/bin"
printf '#!/bin/sh\nprintf "%%s\\n" "$*" >> "%s/docker-argv.log"\nexec "%s" "$@"\n' "$WORK" "$REAL_DOCKER" > "$WORK/bin/docker"
chmod +x "$WORK/bin/docker"
export PATH="$WORK/bin:$PATH"
# backup_bee_keys.sh needs util-linux flock (present on the Ubuntu hosts). macOS
# has none, so this local drill alone supplies a stand-in with the same fd
# semantics: it locks the open file description the shell passed in.
if ! command -v flock >/dev/null 2>&1; then
  command -v python3 >/dev/null || { echo "drill needs flock or python3" >&2; exit 2; }
  cat > "$WORK/bin/flock" <<'PY'
#!/usr/bin/env python3
import fcntl, sys
assert sys.argv[1] == "-n" and sys.argv[2].isdigit(), sys.argv
try:
    fcntl.flock(int(sys.argv[2]), fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(1)
PY
  chmod +x "$WORK/bin/flock"
  echo "note: no flock on this machine; the drill uses a python fcntl stand-in"
fi
# no_daemon_log <pattern> <what>: every recorded `docker run` matching the
# pattern must have used --log-driver none and --network none, and there must
# be at least one.
no_daemon_log() {
  local n bad nonet
  n="$(grep -c -- "$1" "$WORK/docker-argv.log")"
  bad="$(grep -- "$1" "$WORK/docker-argv.log" | grep -vc -- '--log-driver none')"
  nonet="$(grep -- "$1" "$WORK/docker-argv.log" | grep -vc -- '--network none')"
  [ "$n" -gt 0 ] || die "no $2 run was recorded"
  [ "$bad" -eq 0 ] || die "$bad $2 run(s) without --log-driver none"
  [ "$nonet" -eq 0 ] || die "$nonet $2 run(s) without --network none"
  echo "all $n $2 run(s) used --log-driver none and --network none: the plaintext stream never reached a container log or a network"
}
export BEE_PASSWORD="drill-$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')"
cleanup() {
  docker rm -f "$ID-zero" "$ID-src" "$ID-neg" "$ID-again" >/dev/null 2>&1
  docker volume rm -f "$SRC_VOL" "$DST_VOL" "$NEG_VOL" "$ZERO_VOL" >/dev/null 2>&1
  rm -rf "$WORK"
}
trap cleanup EXIT
step() { echo; echo "== $*"; }
die() { echo "DRILL FAILED: $*" >&2; exit 1; }

start_bee() {  # start_bee <container> <volume> [bee start flags...]
  local c="$1" v="$2"; shift 2
  docker run -d --name "$c" --network none -v "$v:/home/bee/.bee" \
    -e BEE_MAINNET=true -e BEE_PASSWORD -e BEE_DATA_DIR=/home/bee/.bee -e BEE_API_ADDR=127.0.0.1:1633 \
    -e BEE_SWAP_ENABLE=false -e BEE_FULL_NODE=false "$BEE_IMAGE" start "$@" >/dev/null
}
addresses() {  # addresses <container>; prints the /addresses JSON or fails after 60 s
  local i
  for i in $(seq 1 30); do
    docker run --rm --network "container:$1" "$CURL" -sf http://127.0.0.1:1633/addresses 2>/dev/null && return 0
    [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = true ] || return 1
    sleep 2
  done
  return 1
}
field() { sed -n "s/.*\"$1\":\"\\([0-9a-fA-Fx]*\\)\".*/\\1/p" | tr 'A-F' 'a-f'; }

step "1. Create a throwaway identity (Bee $BEE_IMAGE, --network none)"
# A mainnet node mines its overlay nonce at first start; a networkless node cannot
# fetch the suggested neighbourhood, so give it a random 10-bit target instead.
# This reproduces the non-zero nonce production nodes have. First start once with
# an empty suggester to learn the nonce-0 overlay (Bee stores no nonce then), and
# reroll the target until the nonce-0 overlay is NOT in it, so mining always runs.
docker volume create "$SRC_VOL" >/dev/null
start_bee "$ID-zero" "$SRC_VOL" --neighborhood-suggester= || die "could not start the source node"
json="$(addresses "$ID-zero")" || { docker logs --tail 5 "$ID-zero"; die "source node did not answer /addresses"; }
ZERO_OVERLAY="$(printf '%s' "$json" | field overlay)"
docker rm -f "$ID-zero" >/dev/null
z=$(( 16#${ZERO_OVERLAY:0:3} >> 2 ))     # first 10 bits of the nonce-0 overlay
n=$z; while [ "$n" -eq "$z" ]; do n=$((RANDOM % 1024)); done
target=""; for i in 9 8 7 6 5 4 3 2 1 0; do target="$target$(( (n >> i) & 1 ))"; done
start_bee "$ID-src" "$SRC_VOL" --target-neighborhood="$target" || die "could not start the source node"
json="$(addresses "$ID-src")" || { docker logs --tail 5 "$ID-src"; die "source node did not answer /addresses"; }
ETH="$(printf '%s' "$json" | field ethereum)"; OVERLAY="$(printf '%s' "$json" | field overlay)"
echo "original ethereum address: $ETH"
echo "original overlay:          $OVERLAY"
echo "nonce-0 overlay:           $ZERO_OVERLAY"
# Read the logs first: with pipefail, `docker logs | grep -q` can fail when grep
# exits early and docker logs gets SIGPIPE.
src_logs="$(docker logs "$ID-src" 2>&1)"
case "$src_logs" in *'mining a new overlay'*) ;; *) die "the source node did not mine a nonce" ;; esac
[ "$OVERLAY" != "$ZERO_OVERLAY" ] || die "the mined overlay equals the nonce-0 overlay"
echo "original node mined a non-zero overlay nonce (target $target)"

step "2. Back up the keys with backup_bee_keys.sh while the node runs"
[ "$(docker inspect -f '{{.State.Running}}' "$ID-src")" = true ] || die "source node is not running"
age-keygen -o "$WORK/age-identity.txt" 2>/dev/null
export BEE_KEYS_AGE_RECIPIENT="$(age-keygen -y "$WORK/age-identity.txt")"
# Never read a real host's /etc/swarm_connect/bee_keys.env: the drill's own values only.
export BEE_KEYS_ENV_FILE=""
export BEE_KEYS_NODES="drill:$SRC_VOL" BEE_KEYS_DIR="$WORK/keys-local" BEE_KEYS_REMOTE="$WORK/keys-remote"
export BACKUP_DIR="$WORK/state-local" BACKUP_REMOTE="$WORK/state-remote"
mkdir -p "$BEE_KEYS_REMOTE"
# The backup script never needs the keystore password; keep it out of its environment.
env -u BEE_PASSWORD "$HERE/backup_bee_keys.sh" || die "backup run failed"
[ "$(docker inspect -f '{{.State.Running}}' "$ID-src")" = true ] || die "the node stopped during the backup"
echo "node still running after the backup"
ls "$BEE_KEYS_REMOTE"
if grep -rlq '"crypto"' "$BEE_KEYS_DIR" "$BEE_KEYS_REMOTE"; then die "a plaintext keystore was found on disk"; fi
echo "no plaintext keystore in the local or remote backup directories"
no_daemon_log ':/src:ro' "snapshot (keys/ read)"

step "3. Weekly --verify against the remote copy"
env -u BEE_PASSWORD "$HERE/backup_bee_keys.sh" --verify || die "--verify failed"

step "4. A second run with unchanged keys uploads nothing"
before="$(ls "$BEE_KEYS_REMOTE" | wc -l)"
env -u BEE_PASSWORD "$HERE/backup_bee_keys.sh" | grep unchanged || die "second run did not report unchanged"
[ "$(ls "$BEE_KEYS_REMOTE" | wc -l)" = "$before" ] || die "second run uploaded something"

step "5. Wipe the node and its volume"
docker rm -f "$ID-src" >/dev/null && docker volume rm "$SRC_VOL" >/dev/null || die "wipe failed"
docker volume inspect "$SRC_VOL" >/dev/null 2>&1 && die "volume still exists"
echo "removed container and volume $SRC_VOL"

step "6. Decrypt and restore into a fresh volume"
archive="$(ls "$BEE_KEYS_REMOTE"/*.tar.age)"; manifest="${archive%.tar.age}.sha256"
age -d -i "$WORK/age-identity.txt" "$archive" | "$HERE/restore_bee_keys.sh" restore "$DST_VOL" "$manifest" || die "restore failed"
# A truncated stream must fail, leave the volume empty, and allow a retry.
if age -d -i "$WORK/age-identity.txt" "$archive" | head -c 1024 | "$HERE/restore_bee_keys.sh" restore "$NEG_VOL" "$manifest" 2> "$WORK/trunc.err"; then
  die "a truncated restore succeeded"
fi
grep FAILED "$WORK/trunc.err"
[ -z "$(docker run --rm -v "$NEG_VOL:/dst" alpine:3.20 ls -A /dst)" ] || die "a truncated restore left files behind"
echo "truncated restore failed and left $NEG_VOL empty"
age -d -i "$WORK/age-identity.txt" "$archive" | "$HERE/restore_bee_keys.sh" restore "$NEG_VOL" "$manifest" || die "retry after the truncated restore failed"
age -d -i "$WORK/age-identity.txt" "$archive" | "$HERE/restore_bee_keys.sh" restore "$ZERO_VOL" "$manifest" >/dev/null || die "third restore failed"

no_daemon_log 'run -i ' "restore (decrypted tar on stdin)"
no_daemon_log ':/src:ro' "snapshot (keys/ read, all backup and verify runs so far)"

step "7. Control: a networkless start without the overlay hint"
start_bee "$ID-neg" "$NEG_VOL"
if addresses "$ID-neg" >/dev/null; then die "expected the unhinted networkless start to fail"; fi
docker logs "$ID-neg" 2>&1 | grep -o '"msg"="failed to build bee node" "error"="neighborhood suggestion[^:]*' | head -1
echo "as expected: without the hint Bee 2.8.1 needs the network to choose an overlay"

step "8. Identity check on the restored volume (restore_bee_keys.sh check)"
"$HERE/restore_bee_keys.sh" check "$DST_VOL" "$OVERLAY" "$ETH" | tee "$WORK/check.out" || die "restored identity does not match"
grep -q '^nonce: non-zero' "$WORK/check.out" || die "check did not report a non-zero nonce"

step "8b. check flags a nonce-0 identity (same keys, checked against the nonce-0 overlay)"
"$HERE/restore_bee_keys.sh" check "$ZERO_VOL" "$ZERO_OVERLAY" "$ETH" | tee "$WORK/check0.out" || die "nonce-0 check failed"
grep -q '^WARNING: this identity uses overlay nonce 0' "$WORK/check0.out" || die "check did not flag nonce 0"

step "9. Restart without the hint: the nonce now lives in the restored statestore"
start_bee "$ID-again" "$DST_VOL"
json="$(addresses "$ID-again")" || { docker logs --tail 5 "$ID-again"; die "restart did not answer"; }
[ "$(printf '%s' "$json" | field overlay)" = "$OVERLAY" ] || die "overlay changed after restart"
[ "$(printf '%s' "$json" | field ethereum)" = "$ETH" ] || die "address changed after restart"
echo "overlay and address unchanged after restart"

step "10. What --log-driver none prevents, measured on this daemon"
marker="KEYSTORE-LOG-PROBE-$$"
c1="$(docker run -d alpine:3.20 sh -c "echo $marker")"
c2="$(docker run -d --log-driver none alpine:3.20 sh -c "echo $marker")"
docker wait "$c1" "$c2" >/dev/null
docker logs "$c1" 2>&1 | grep -q "$marker" || die "expected the default log driver to keep the output"
echo "default driver ($(docker inspect -f '{{.HostConfig.LogConfig.Type}}' "$c1")): docker logs returns the container's stdout"
out2="$(docker logs "$c2" 2>&1)"
case "$out2" in *"$marker"*) die "--log-driver none kept the output" ;; esac
echo "--log-driver none: docker logs -> ${out2:-<nothing>}"
docker rm -f "$c1" "$c2" >/dev/null

echo
echo "DRILL PASSED: restored node reports ethereum $ETH and overlay $OVERLAY, same as the original"
