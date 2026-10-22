#!/usr/bin/env bash
# Local restore drill for the Bee keystore backup (#494). Uses a THROWAWAY Bee
# identity in local Docker only: nothing here touches a real node, a real key or
# the network (every Bee container runs with --network none).
#
# Steps: create an identity -> back it up with backup_bee_keys.sh while the node
# runs (read-only mount) -> verify -> confirm a second run uploads nothing ->
# wipe the node and its volume -> decrypt and restore with restore_bee_keys.sh
# into a fresh volume -> show that a networkless start without the overlay hint
# fails -> check that a fresh networkless container reports the same Ethereum
# address and overlay -> restart without the hint and check the overlay again.
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
SRC_VOL="${ID}-src"; DST_VOL="${ID}-dst"; NEG_VOL="${ID}-neg"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/bee_keys_drill.XXXXXX")"
export BEE_PASSWORD="drill-$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')"
cleanup() {
  docker rm -f "$ID-src" "$ID-neg" "$ID-again" >/dev/null 2>&1
  docker volume rm -f "$SRC_VOL" "$DST_VOL" "$NEG_VOL" >/dev/null 2>&1
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
# This reproduces the non-zero nonce production nodes have.
n=$((RANDOM % 1024)); target=""
for i in 9 8 7 6 5 4 3 2 1 0; do target="$target$(( (n >> i) & 1 ))"; done
docker volume create "$SRC_VOL" >/dev/null
start_bee "$ID-src" "$SRC_VOL" --target-neighborhood="$target" || die "could not start the source node"
json="$(addresses "$ID-src")" || { docker logs --tail 5 "$ID-src"; die "source node did not answer /addresses"; }
ETH="$(printf '%s' "$json" | field ethereum)"; OVERLAY="$(printf '%s' "$json" | field overlay)"
echo "original ethereum address: $ETH"
echo "original overlay:          $OVERLAY"
docker logs "$ID-src" 2>&1 | grep -q 'mining a new overlay' && echo "original node mined a non-zero overlay nonce (target $target)"

step "2. Back up the keys with backup_bee_keys.sh while the node runs"
[ "$(docker inspect -f '{{.State.Running}}' "$ID-src")" = true ] || die "source node is not running"
age-keygen -o "$WORK/age-identity.txt" 2>/dev/null
export BEE_KEYS_AGE_RECIPIENT="$(age-keygen -y "$WORK/age-identity.txt")"
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
age -d -i "$WORK/age-identity.txt" "$archive" | "$HERE/restore_bee_keys.sh" restore "$NEG_VOL" "$manifest" >/dev/null || die "second restore failed"

step "7. Control: a networkless start without the overlay hint"
start_bee "$ID-neg" "$NEG_VOL"
if addresses "$ID-neg" >/dev/null; then die "expected the unhinted networkless start to fail"; fi
docker logs "$ID-neg" 2>&1 | grep -o '"msg"="failed to build bee node" "error"="neighborhood suggestion[^:]*' | head -1
echo "as expected: without the hint Bee 2.8.1 needs the network to choose an overlay"

step "8. Identity check on the restored volume (restore_bee_keys.sh check)"
"$HERE/restore_bee_keys.sh" check "$DST_VOL" "$OVERLAY" "$ETH" || die "restored identity does not match"

step "9. Restart without the hint: the nonce now lives in the restored statestore"
start_bee "$ID-again" "$DST_VOL"
json="$(addresses "$ID-again")" || { docker logs --tail 5 "$ID-again"; die "restart did not answer"; }
[ "$(printf '%s' "$json" | field overlay)" = "$OVERLAY" ] || die "overlay changed after restart"
[ "$(printf '%s' "$json" | field ethereum)" = "$ETH" ] || die "address changed after restart"
echo "overlay and address unchanged after restart"

echo
echo "DRILL PASSED: restored node reports ethereum $ETH and overlay $OVERLAY, same as the original"
