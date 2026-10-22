#!/usr/bin/env bash
# Restore a Bee node identity backed up by scripts/backup_bee_keys.sh (#494), and
# prove it with a networkless Bee container. docs/mainnet-runbook.md §4 has the
# full procedure.
#
#   restore_bee_keys.sh restore <volume> <manifest.sha256>  < keys.tar
#       Reads the DECRYPTED tar on stdin and writes only its keys/ directory into
#       <volume>. Decrypt where the age identity lives and pipe it in, e.g.
#         age -d -i <age-identity> bee-keys-prod-<stamp>.tar.age \
#           | ssh root@<new-host> restore_bee_keys.sh restore swarm_connect_bee-data <manifest>
#       so neither the identity nor a plaintext keystore file touches the host's
#       disk outside the volume. Refuses any volume that is not empty. The
#       restored files must match the manifest exactly (same names, same
#       checksums, nothing extra; every regular file counts, dotfiles included), be owned by the Bee image's user and be
#       readable by it alone; on any failure, including a truncated or corrupt
#       input or an interrupt (Ctrl-C, a dropped ssh session), the restored keys/
#       is removed so the restore can be retried. If a restore was killed so hard
#       that neither trap ran (kill -9 of both sides), clear the volume with
#         docker run --rm -v <volume>:/dst alpine:3.20 rm -rf /dst/keys
#
#   BEE_PASSWORD=... restore_bee_keys.sh check <volume> <overlay> [<ethereum-address>]
#       Starts Bee on <volume> with --network none and no blockchain endpoint
#       (ultra-light mode), reads /addresses through a helper container that
#       shares the node's network namespace, and exits 0 only if the overlay (and
#       the address, if given) match. The keystore password comes from the
#       environment and is passed by name, never on a command line.
#
#       The overlay depends on a nonce kept in the statestore, not in keys/
#       (bee v2.8.1 pkg/node/node.go:315-320). A mainnet node mines that nonce at
#       first start to land in a suggested neighbourhood, and a networkless node
#       with no nonce cannot fetch a suggestion and refuses to start. So `check`
#       passes --target-neighborhood with the first 31 bits of the recorded
#       overlay: mining starts at nonce 0 and stops at the first match
#       (pkg/util/nbhdutil/miner.go), which is the original nonce, and Bee stores
#       it in the volume's statestore (pkg/node/node.go:335-370).
#
#       Nonce 0: Bee stores a nonce ONLY when it mines one. If the identity's
#       overlay uses nonce 0, nothing is stored, and a later start WITH network
#       and without the hint fetches a new suggestion and may mine a different
#       overlay. `check` therefore also starts Bee on a temporary copy of the keys
#       with an empty --neighborhood-suggester, which gives the nonce-0 overlay,
#       and prints whether this identity is a nonce-0 one. For a nonce-0 node,
#       every start must carry the hint: BEE_TARGET_NEIGHBORHOOD=<bits> (Bee
#       2.8.1 reads it from the environment).
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
usage() { sed -n '5,/^# Environment/p' "$0" >&2; exit 2; }

cmd="${1:-}"; [ $# -ge 2 ] || usage
vol="$2"

# Runs inside the helper container, with the volume at /dst, the manifest at
# /m.sha256 and the owner in $U:$G. Exit 3: bad input, 4: manifest mismatch,
# 5: ownership or modes. Every failure removes the restored keys/.
read -r -d '' RESTORE_SH <<'SH'
set -u
fail() { rm -rf /dst/keys; exit "$1"; }
trap 'fail 130' INT TERM HUP PIPE
tar -C /dst -xf - keys || fail 3
[ -d /dst/keys ] || fail 3
cd /dst/keys || fail 3
# Same enumeration as the backup's manifest: every regular file, dotfiles
# included; anything else (link, directory) is refused.
[ -z "$(find . ! -type f ! -path .)" ] || fail 4
got="$(find . -type f | sed 's|^\./||' | LC_ALL=C sort | while IFS= read -r f; do sha256sum -- "$f"; done | LC_ALL=C sort)"
want="$(LC_ALL=C sort /m.sha256)"
[ -n "$want" ] && [ "$got" = "$want" ] || fail 4
cd / || fail 5
chown -R "$U:$G" /dst && chmod 700 /dst/keys && find /dst/keys -type f -exec chmod 600 {} + || fail 5
ls -lnd /dst/keys | awk -v u="$U" -v g="$G" 'substr($1,1,10) != "drwx------" || $3 != u || $4 != g { bad = 1 } END { exit bad }' || fail 5
ls -lnA /dst/keys | awk -v u="$U" -v g="$G" '$1 == "total" { next } substr($1,1,10) != "-rw-------" || $3 != u || $4 != g { bad = 1 } END { exit bad }' || fail 5
SH

case "$cmd" in
restore)
  manifest="${3:-}"
  [ -n "$manifest" ] || { echo "REFUSED: a manifest (.sha256 from the backup) is required" >&2; exit 2; }
  [ -f "$manifest" ] || { echo "FAILED: manifest $manifest not found" >&2; exit 2; }
  docker volume inspect "$vol" >/dev/null 2>&1 || docker volume create "$vol" >/dev/null || exit 1
  first="$(docker run --rm -v "$vol:/dst" "$HELPER" sh -c 'ls -A /dst | head -n 1')" || {
    echo "FAILED: could not read volume $vol" >&2; exit 1; }
  if [ -n "$first" ]; then
    echo "REFUSED: $vol is not empty (it holds $first); restore only into an empty volume" >&2; exit 2
  fi
  uid="$(docker run --rm --entrypoint id "$BEE_IMAGE" -u)" || { echo "FAILED: could not read the uid of $BEE_IMAGE" >&2; exit 1; }
  gid="$(docker run --rm --entrypoint id "$BEE_IMAGE" -g)" || { echo "FAILED: could not read the gid of $BEE_IMAGE" >&2; exit 1; }
  case "$uid:$gid" in *[!0-9:]*|:*|*:) echo "FAILED: unexpected uid:gid '$uid:$gid' from $BEE_IMAGE" >&2; exit 1 ;; esac
  m_abs="$(cd "$(dirname "$manifest")" && pwd)/$(basename "$manifest")"
  # Only keys/ is extracted, whatever else the tar holds. If this run is
  # interrupted (Ctrl-C on the ssh pipe, a dropped session), the container's own
  # trap removes keys/; this trap repeats that from outside in case the container
  # was killed first. The volume was empty before, so keys/ can only be ours.
  trap 'docker run --rm -v "$vol:/dst" "$HELPER" rm -rf /dst/keys >/dev/null 2>&1; echo "FAILED: interrupted; removed the partial keys/ from $vol" >&2; exit 1' INT TERM HUP
  docker run -i --rm -v "$vol:/dst" -v "$m_abs:/m.sha256:ro" -e U="$uid" -e G="$gid" "$HELPER" sh -c "$RESTORE_SH"
  rc=$?
  trap - INT TERM HUP
  case "$rc" in
    0) echo "restored keys into $vol (owner $uid:$gid)" ;;
    3) echo "FAILED: could not extract keys/ from the archive on stdin (truncated or corrupt?); removed the partial keys/" >&2; exit 1 ;;
    4) echo "FAILED: restored files do not match $manifest (checksum, missing or extra file); removed them" >&2; exit 1 ;;
    5) echo "FAILED: could not set or confirm owner $uid:$gid and modes 700/600; removed the restored keys/" >&2; exit 1 ;;
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
  name="bee-identity-check-$$"; probe="bee-identity-probe-$$"
  trap 'docker rm -f "$name" "$probe" >/dev/null 2>&1; docker volume rm -f "$probe" >/dev/null 2>&1' EXIT

  # bee_addresses <container> <volume> [start flags...]: start a networkless
  # ultra-light Bee and print its /addresses JSON.
  bee_addresses() {
    local c="$1" v="$2" got deadline; shift 2
    docker run -d --name "$c" --network none -v "$v:/home/bee/.bee" \
      -e BEE_MAINNET="${BEE_MAINNET:-true}" -e BEE_PASSWORD -e BEE_DATA_DIR=/home/bee/.bee \
      -e BEE_API_ADDR=127.0.0.1:1633 -e BEE_SWAP_ENABLE=false -e BEE_FULL_NODE=false \
      "$BEE_IMAGE" start "$@" >/dev/null || return 1
    deadline=$(( $(date +%s) + ${BEE_KEYS_CHECK_TIMEOUT:-90} ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
      got="$(docker run --rm --network "container:$c" "$CURL" -sf http://127.0.0.1:1633/addresses 2>/dev/null)" && { printf '%s' "$got"; return 0; }
      if [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" != true ]; then
        echo "FAILED: Bee exited; last log lines:" >&2; docker logs --tail 5 "$c" >&2; return 1
      fi
      sleep 2
    done
    echo "FAILED: no /addresses answer within the timeout" >&2; docker logs --tail 5 "$c" >&2; return 1
  }
  field() { sed -n "s/.*\"$1\":\"\\([0-9a-fA-Fx]*\\)\".*/\\1/p" | tr 'A-F' 'a-f'; }

  got="$(bee_addresses "$name" "$vol" --target-neighborhood="$bits")" || exit 1
  got_overlay="$(printf '%s' "$got" | field overlay)"; got_address="$(printf '%s' "$got" | field ethereum)"
  echo "ethereum address: $got_address"
  echo "overlay:          $got_overlay"
  ok=1
  [ "$got_overlay" = "$overlay" ] || { echo "MISMATCH: overlay, expected $overlay" >&2; ok=0; }
  if [ -n "$address" ] && [ "$got_address" != "$address" ]; then echo "MISMATCH: ethereum address, expected $address" >&2; ok=0; fi
  [ "$ok" -eq 1 ] || exit 1
  echo "identity matches"

  # Nonce-0 probe on a throwaway copy of keys/ only, so the volume under test is untouched.
  docker volume create "$probe" >/dev/null || exit 1
  docker run --rm -v "$vol:/src:ro" -v "$probe:/dst" "$HELPER" sh -c 'cp -a /src/keys /dst/ && chown "$(stat -c %u:%g /src/keys)" /dst' || {
    echo "FAILED: could not copy keys/ for the nonce probe" >&2; exit 1; }
  zero="$(bee_addresses "$probe" "$probe" --neighborhood-suggester= | field overlay)" || exit 1
  [ -n "$zero" ] || { echo "FAILED: nonce probe gave no overlay" >&2; exit 1; }
  if [ "$zero" = "$overlay" ]; then
    echo "WARNING: this identity uses overlay nonce 0. Bee 2.8.1 stores a nonce only when it mines one, so none was stored, and a start WITH network and without the hint may mine a different overlay. Start this node every time with BEE_TARGET_NEIGHBORHOOD=$bits."
  else
    echo "nonce: non-zero; Bee keeps it in the statestore of $vol after this check, so later starts keep this overlay"
  fi
  exit 0
  ;;
*) usage ;;
esac
