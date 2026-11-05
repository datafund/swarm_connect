#!/usr/bin/env bash
# Fail if requirements.lock or requirements-dev.lock is out of sync with the
# file it is compiled from (#430).
#
# Each lock's header names the command that made it. This runs that command,
# verbatim, on copies of the inputs and the locks in a temporary directory, and
# compares the result with the committed lock. The working tree is not touched.
#
# Running on a copy of the lock matters: uv pip compile keeps the pins of an
# existing output file wherever they still satisfy the input, so the check
# moves nothing that is already valid. It fails when the input asks for
# something the lock does not have: a package added or removed, or a floor
# raised above the locked version. A floor raised to a version the lock
# already satisfies is in sync, and passes.
#
# Needs uv 0.11.x (another version can format the lock differently) and access
# to PyPI. CI (tests.yml, job lock-sync) runs it on every pull request.
# Run: scripts/check_lock_sync.sh     Exit 0 = in sync, 1 = out of sync, 2 = error.
set -u
set -o pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
LOCKS=(requirements.lock requirements-dev.lock)   # runtime first: the dev lock is constrained by it
INPUTS=(requirements.txt requirements-dev.txt)

command -v uv >/dev/null || { echo "check_lock_sync: uv is not on PATH" >&2; exit 2; }
UV_VERSION="$(uv --version)"
case "$UV_VERSION" in
  "uv 0.11."*) ;;
  *) echo "check_lock_sync: needs uv 0.11.x, found: $UV_VERSION" >&2; exit 2 ;;
esac

# A user-level uv config or UV_* variable (another index, a custom header
# command) would change the output, so neither is used. UV_NO_CONFIG, not
# --no-config: a flag would be written into the regenerated header.
for v in $(compgen -e | grep '^UV_' || true); do unset "$v"; done
export UV_NO_CONFIG=1

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
for f in "${INPUTS[@]}" "${LOCKS[@]}"; do
  cp "$HERE/$f" "$WORK/$f" || { echo "check_lock_sync: cannot copy $f" >&2; exit 2; }
done

status=0
for lock in "${LOCKS[@]}"; do
  # Line 2 of the header: "#    uv pip compile ... -o <lock>"
  cmd="$(sed -n '2s/^#[[:space:]]*//p' "$HERE/$lock")"
  if ! [[ "$cmd" =~ ^uv\ pip\ compile\ [A-Za-z0-9._=\ /-]+$ ]] \
     || [[ " $cmd " != *" --generate-hashes "* ]] \
     || [[ " $cmd " != *" --universal "* ]] \
     || [[ " $cmd " != *" -o $lock "* ]]; then
    echo "check_lock_sync: $lock header has no usable compile command: '$cmd'" >&2
    exit 2
  fi
  read -ra argv <<< "$cmd"
  echo "== $lock: $cmd"
  if ! (cd "$WORK" && "${argv[@]}" --quiet); then
    echo "check_lock_sync: the compile command for $lock failed" >&2
    exit 2
  fi
  if ! diff -u "$HERE/$lock" "$WORK/$lock"; then
    echo "::error file=$lock::$lock is out of sync with its input. Regenerate it with the command in its header (see requirements.txt) and commit the result."
    status=1
  fi
done
[ "$status" -eq 0 ] && echo "OK: both locks match their inputs ($UV_VERSION)"
exit "$status"
