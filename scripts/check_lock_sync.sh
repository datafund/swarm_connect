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
# That pass cannot see a wrong hash: uv keeps an existing lock's hashes along
# with its pins, so a changed or deleted --hash line survives it. So each lock
# is also compiled a second time into a fresh file, with no lock to start from,
# constrained to the committed pins (-c, hashes stripped). uv then takes every
# hash from the index, and the check fails unless each package has exactly the
# hashes the committed lock lists.
#
# Because the hashes come from PyPI, the second pass also fails when a pinned
# version gains a file after the lock was made (a new wheel), although nothing
# is wrong with the lock. That is one reason CI runs this on pull requests only,
# not as the deploy gate; regenerating the lock clears it.
#
# Needs uv 0.11.x (another version can format the lock differently), python3
# and access to PyPI. CI (tests.yml, job lock-sync) runs it on every pull request.
# Run: scripts/check_lock_sync.sh
# Exit 0 = in sync, 1 = out of sync, 2 = a tool error (uv, diff, python3), never 1.
set -u
set -o pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
LOCKS=(requirements.lock requirements-dev.lock)   # runtime first: the dev lock is constrained by it
INPUTS=(requirements.txt requirements-dev.txt)

command -v uv >/dev/null || { echo "check_lock_sync: uv is not on PATH" >&2; exit 2; }
command -v python3 >/dev/null || { echo "check_lock_sync: python3 is not on PATH" >&2; exit 2; }
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
FRESH="$WORK/fresh"
mkdir -p "$FRESH/out"
for f in "${INPUTS[@]}" "${LOCKS[@]}"; do
  cp "$HERE/$f" "$WORK/$f" || { echo "check_lock_sync: cannot copy $f" >&2; exit 2; }
done
# The fresh pass gets the inputs and the committed runtime lock (the dev
# lock's header constrains it with -c requirements.lock), never an output.
for f in "${INPUTS[@]}" requirements.lock; do
  cp "$HERE/$f" "$FRESH/$f" || { echo "check_lock_sync: cannot copy $f" >&2; exit 2; }
done

# The pins of a lock, one "name==version[ ; marker]" per line, without hashes.
pins() {
  python3 - "$1" <<'PY'
import re, sys
for line in open(sys.argv[1]):
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*==[^\s;\\]+(?:\s*;[^\\]*)?)\s*\\\s*$", line)
    if m:
        print(m.group(1).strip())
PY
}

# Compare the hashes of two locks, block by block. A universal lock can pin one
# version more than once, under different markers, so a block is keyed by name,
# version and marker, and the hashes of two blocks are never merged.
# Exit 0 = same, 1 = different, 2 = the comparison itself failed.
same_hashes() {
  python3 - "$1" "$2" <<'PY'
import collections, re, sys

def parse(path):
    blocks = collections.defaultdict(list)
    with open(path) as f:
        text = f.read()
    for block in re.split(r"\n(?=[A-Za-z0-9])", text):
        m = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)(?:\s*;([^\\\n]*))?", block)
        if m:
            marker = " ".join((m.group(3) or "").split())
            key = (re.sub(r"[-_.]+", "-", m.group(1)).lower(), m.group(2), marker)
            blocks[key].append(frozenset(re.findall(r"--hash=(sha256:[0-9a-f]+)", block)))
    return blocks

def main():
    committed, fresh = parse(sys.argv[1]), parse(sys.argv[2])
    bad = []
    for key in sorted(set(committed) | set(fresh)):
        name = f"{key[0]}=={key[1]}" + (f" ; {key[2]}" if key[2] else "")
        if key not in fresh:
            bad.append(f"{name}: in the lock, but not resolved from its input")
        elif key not in committed:
            bad.append(f"{name}: resolved from the input, but not in the lock")
        elif collections.Counter(committed[key]) != collections.Counter(fresh[key]):
            old, new = frozenset().union(*committed[key]), frozenset().union(*fresh[key])
            for h in sorted(old - new):
                bad.append(f"{name}: hash {h} is not a file of this version on the index")
            for h in sorted(new - old):
                bad.append(f"{name}: hash {h} of this version is missing from the lock")
            if old == new:
                bad.append(f"{name}: listed {len(committed[key])} times with hashes that differ from the index")
    for line in bad:
        print("  " + line)
    return 1 if bad else 0

try:
    sys.exit(main())
except Exception as e:  # a tool error, not a lock difference
    print(f"check_lock_sync: the hash comparison failed: {e!r}", file=sys.stderr)
    sys.exit(2)
PY
}

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
  diff -u "$HERE/$lock" "$WORK/$lock"
  rc=$?
  if [ "$rc" -gt 1 ]; then
    echo "check_lock_sync: diff failed for $lock (rc $rc)" >&2
    exit 2
  fi
  if [ "$rc" -eq 1 ]; then
    echo "::error file=$lock::$lock is out of sync with its input. Regenerate it with the command in its header (see requirements.txt) and commit the result."
    status=1
    # It has to be regenerated anyway, and its pins may no longer satisfy
    # the input, which the constrained compile below could not resolve.
    continue
  fi

  # Second pass: the hashes, compiled fresh and constrained to the committed pins.
  pins "$HERE/$lock" > "$FRESH/$lock.pins" || { echo "check_lock_sync: cannot read the pins of $lock" >&2; exit 2; }
  fresh_argv=()
  prev=""
  for a in "${argv[@]}"; do
    if [ "$prev" = "-o" ]; then fresh_argv+=("out/$lock"); else fresh_argv+=("$a"); fi
    prev="$a"
  done
  if ! (cd "$FRESH" && "${fresh_argv[@]}" -c "$lock.pins" --quiet); then
    echo "check_lock_sync: the fresh compile for $lock failed" >&2
    exit 2
  fi
  echo "== $lock: hashes against a fresh compile"
  same_hashes "$HERE/$lock" "$FRESH/out/$lock"
  rc=$?
  if [ "$rc" -gt 1 ]; then
    echo "check_lock_sync: the hash comparison for $lock failed (rc $rc)" >&2
    exit 2
  fi
  if [ "$rc" -eq 1 ]; then
    echo "::error file=$lock::$lock lists hashes that differ from the index (see above). Find out why first: a hand edit, or a file PyPI added to a pinned version. Then regenerate it with the command in its header (see requirements.txt)."
    status=1
  fi
done
[ "$status" -eq 0 ] && echo "OK: both locks match their inputs ($UV_VERSION)"
exit "$status"
