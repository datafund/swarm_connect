#!/usr/bin/env bash
# Restore a Bee node identity backed up by scripts/backup_bee_keys.sh (#494), and
# prove it with a networkless Bee container. docs/mainnet-runbook.md §4 has the
# full procedure.
#
#   restore_bee_keys.sh restore <volume> [<manifest.sha256>]  < keys.tar
#       Reads the DECRYPTED tar on stdin and writes only its keys/ directory into
#       <volume>. Decrypt where the age identity lives and pipe it in, e.g.
#         age -d -i <age-identity> bee-keys-prod-<stamp>.tar.age \
#           | ssh root@<new-host> restore_bee_keys.sh restore swarm_connect_bee-data
#       so neither the identity nor a plaintext keystore file touches the host's
#       disk outside the volume. Refuses a volume that already has keys/. With a
#       manifest, the restored files must match its checksums or they are removed.
#
#   BEE_PASSWORD=... restore_bee_keys.sh check <volume> <overlay> [<ethereum-address>]
#       Starts Bee on <volume> with --network none and no blockchain endpoint
#       (ultra-light mode), reads /addresses through a helper container that
#       shares the node's network namespace, and exits 0 only if the overlay (and
#       the address, if given) match. The keystore password comes from the
#       environment and is passed by name, never on a command line.
#
#       The overlay depends on a nonce kept in the statestore, not in keys/
#       (bee v2.8.1 pkg/node/node.go:315-370). A mainnet node mines that nonce at
#       first start to land in a suggested neighbourhood, and a networkless node
#       with no nonce cannot fetch a suggestion and refuses to start. So `check`
#       passes --target-neighborhood with the first 31 bits of the recorded
#       overlay: mining starts at nonce 0 and stops at the first match
#       (pkg/util/nbhdutil/miner.go), which is the original nonce, and Bee stores
#       it in the volume's statestore. After a passing check the volume keeps the
#       original overlay without the flag.
#
# Environment: BEE_IMAGE (default ethersphere/bee:${BEE_VERSION:-2.8.1}),
# BEE_KEYS_HELPER_IMAGE (default alpine:3.20), BEE_KEYS_CURL_IMAGE (default
# curlimages/curl:8.10.1), BEE_MAINNET (default true), BEE_KEYS_CHECK_TIMEOUT
# seconds (default 90).
set -u
set -o pipefail

BEE_IMAGE="${BEE_IMAGE:-ethersphere/bee:${BEE_VERSION:-2.8.1}}"
HELPER="${BEE_KEYS_HELPER_IMAGE:-alpine:3.20}"
CURL="${BEE_KEYS_CURL_IMAGE:-curlimages/curl:8.10.1}"
usage() { sed -n '5,23p' "$0" >&2; exit 2; }

cmd="${1:-}"; [ $# -ge 2 ] || usage
vol="$2"

case "$cmd" in
restore)
  manifest="${3:-}"
  if [ -n "$manifest" ] && [ ! -f "$manifest" ]; then echo "FAILED: manifest $manifest not found" >&2; exit 2; fi
  docker volume inspect "$vol" >/dev/null 2>&1 || docker volume create "$vol" >/dev/null || exit 1
  if docker run --rm -v "$vol:/dst" "$HELPER" test -e /dst/keys; then
    echo "REFUSED: $vol already has a keys/ directory; restore only into an empty volume" >&2; exit 2
  fi
  owner="$(docker run --rm --entrypoint id "$BEE_IMAGE" -u):$(docker run --rm --entrypoint id "$BEE_IMAGE" -g)" || exit 1
  mount_m=(); check='true'
  if [ -n "$manifest" ]; then
    mount_m=(-v "$(cd "$(dirname "$manifest")" && pwd)/$(basename "$manifest"):/m.sha256:ro")
    check='(cd /dst/keys && sha256sum -c -s /m.sha256)'
  fi
  # Only keys/ is extracted, whatever else the tar holds.
  docker run -i --rm -v "$vol:/dst" ${mount_m[@]+"${mount_m[@]}"} "$HELPER" sh -c "
    tar -C /dst -xf - keys || exit 3
    if ! $check; then rm -rf /dst/keys; exit 4; fi
    chown -R $owner /dst && chmod 700 /dst/keys && chmod 600 /dst/keys/*"
  rc=$?
  case "$rc" in
    0) echo "restored keys into $vol (owner $owner)" ;;
    3) echo "FAILED: could not extract keys/ from the archive on stdin" >&2; exit 1 ;;
    4) echo "FAILED: restored files do not match $manifest; removed them" >&2; exit 1 ;;
    *) echo "FAILED: restore into $vol (exit $rc)" >&2; exit 1 ;;
  esac
  ;;
check)
  overlay="${3:-}"; address="${4:-}"
  overlay="$(printf '%s' "$overlay" | tr 'A-F' 'a-f')"; address="$(printf '%s' "$address" | tr 'A-F' 'a-f')"
  case "$overlay" in *[!0-9a-f]*|'') echo "REFUSED: overlay must be 64 hex characters" >&2; exit 2 ;; esac
  [ "${#overlay}" -eq 64 ] || { echo "REFUSED: overlay must be 64 hex characters" >&2; exit 2; }
  [ -n "${BEE_PASSWORD:-}" ] || { echo "REFUSED: BEE_PASSWORD must be set in the environment" >&2; exit 2; }
  docker volume inspect "$vol" >/dev/null 2>&1 || { echo "FAILED: volume $vol does not exist" >&2; exit 1; }
  n=$((16#${overlay:0:8})); bits=""
  for i in $(seq 31 -1 1); do bits="$bits$(( (n >> i) & 1 ))"; done
  name="bee-identity-check-$$"
  trap 'docker rm -f "$name" >/dev/null 2>&1' EXIT
  docker run -d --name "$name" --network none -v "$vol:/home/bee/.bee" \
    -e BEE_MAINNET="${BEE_MAINNET:-true}" -e BEE_PASSWORD -e BEE_DATA_DIR=/home/bee/.bee \
    -e BEE_API_ADDR=127.0.0.1:1633 -e BEE_SWAP_ENABLE=false -e BEE_FULL_NODE=false \
    "$BEE_IMAGE" start --target-neighborhood="$bits" >/dev/null || exit 1
  got=""; deadline=$(( $(date +%s) + ${BEE_KEYS_CHECK_TIMEOUT:-90} ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    got="$(docker run --rm --network "container:$name" "$CURL" -sf http://127.0.0.1:1633/addresses 2>/dev/null)" && break
    if [ "$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" != true ]; then
      echo "FAILED: Bee exited; last log lines:" >&2; docker logs --tail 5 "$name" >&2; exit 1
    fi
    sleep 2
  done
  [ -n "$got" ] || { echo "FAILED: no /addresses answer within the timeout" >&2; docker logs --tail 5 "$name" >&2; exit 1; }
  got_overlay="$(printf '%s' "$got" | sed -n 's/.*"overlay":"\([0-9a-f]*\)".*/\1/p')"
  got_address="$(printf '%s' "$got" | sed -n 's/.*"ethereum":"\(0x[0-9a-fA-F]*\)".*/\1/p' | tr 'A-F' 'a-f')"
  echo "ethereum address: $got_address"
  echo "overlay:          $got_overlay"
  ok=1
  [ "$got_overlay" = "$overlay" ] || { echo "MISMATCH: overlay, expected $overlay" >&2; ok=0; }
  if [ -n "$address" ] && [ "$got_address" != "$address" ]; then echo "MISMATCH: ethereum address, expected $address" >&2; ok=0; fi
  [ "$ok" -eq 1 ] && { echo "identity matches"; exit 0; }
  exit 1
  ;;
*) usage ;;
esac
