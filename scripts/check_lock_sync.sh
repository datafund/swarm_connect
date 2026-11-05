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
# Run: scripts/check_lock_sync.sh     Exit 0 = in sync, 1 = out of sync, 2 = error.
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

# Compare the hashes of two locks, package by package. Exit 1 on any difference.
same_hashes() {
  python3 - "$1" "$2" <<'PY'
import re, sys

def parse(path):
    pkgs = {}
    for block in re.split(r"\n(?=[A-Za-z0-9])", open(path).read()):
        m = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)", block)
        if m:
            key = (re.sub(r"[-_.]+", "-", m.group(1)).lower(), m.group(2))
            pkgs.setdefault(key, set()).update(re.findall(r"--hash=(sha256:[0-9a-f]+)", block))
    return pkgs

committed, fresh = parse(sys.argv[1]), parse(sys.argv[2])
bad = []
for key in sorted(set(committed) | set(fresh)):
    name = "==".join(key)
    if key not in fresh:
        bad.append(f"{name}: in the lock, but not resolved from its input")
    elif key not in committed:
        bad.append(f"{name}: resolved from the input, but not in the lock")
    else:
        for h in sorted(committed[key] - fresh[key]):
            bad.append(f"{name}: hash {h} is not a file of this version on the index")
        for h in sorted(fresh[key] - committed[key]):
            bad.append(f"{name}: hash {h} of this version is missing from the lock")
for line in bad:
    print("  " + line)
sys.exit(1 if bad else 0)
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
  if ! diff -u "$HERE/$lock" "$WORK/$lock"; then
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
  if ! same_hashes "$HERE/$lock" "$FRESH/out/$lock"; then
    echo "::error file=$lock::$lock lists hashes that differ from the index (see above). Find out why first: a hand edit, or a file PyPI added to a pinned version. Then regenerate it with the command in its header (see requirements.txt)."
    status=1
  fi
done
[ "$status" -eq 0 ] && echo "OK: both locks match their inputs ($UV_VERSION)"
exit "$status"
