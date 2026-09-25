# app/core/atomic_io.py
"""Atomic file write helpers.

Writing JSON state with a plain ``open(path, 'w')`` truncates the target file
immediately, so a crash mid-write (OOM, deploy restart, SIGKILL) leaves a
truncated/corrupt file on disk. These helpers write to a temporary file in the
same directory, fsync it, then atomically rename it into place. Readers always
see either the previous complete file or the new complete file, never a partial
one.

See GitHub Issue #212.
"""
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from typing import Any, Optional

__all__ = ["atomic_write_json", "load_json_state", "StateLoadError"]


class StateLoadError(RuntimeError):
    """A state file exists but cannot be read as the expected JSON object."""


def atomic_write_json(path: str, data: Any) -> None:
    """Atomically write ``data`` as JSON to ``path``.

    Writes to a temporary file in the same directory as the target, flushes and
    fsyncs it, then atomically replaces the target via ``os.replace()``. Parent
    directories are created if missing. On any failure the temporary file is
    removed and the original target is left untouched.

    Args:
        path: Destination file path.
        data: JSON-serializable object to persist.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)

    # The temp file must live on the same filesystem as the target (i.e. in the
    # same directory) for os.replace() to be an atomic rename rather than a
    # cross-device copy.
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        # Never leave a partial temp file behind on failure.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_json_state(path: str) -> Optional[dict]:
    """Read a JSON object from ``path`` for a store that must not lose data.

    Returns None when the file does not exist, which is the only case in which
    starting empty is correct.

    Any other failure (unparseable JSON, a non-object at the top level, a
    permission or I/O error) raises StateLoadError after copying the file to
    ``<path>.corrupt-<UTC timestamp>``. The original is left in place. Callers
    holding money-bearing state (the stamp ownership registry, the prepaid
    bandwidth ledger) let this propagate so the gateway refuses to start:
    starting empty would lock owners out and the next save would overwrite the
    only copy of their records.
    """
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        raise StateLoadError(_unreadable(path, e)) from e
    if not isinstance(data, dict):
        raise StateLoadError(_unreadable(path, f"expected a JSON object, got {type(data).__name__}"))
    return data


def _unreadable(path: str, reason: Any) -> str:
    backup = f"{path}.corrupt-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    try:
        shutil.copy2(path, backup)
        kept = f"a copy was saved to {backup}"
    except Exception as copy_error:
        kept = f"could not save a copy ({copy_error})"
    return (
        f"State file {path} could not be read ({reason}); {kept}. Refusing to start "
        f"with empty state, which would overwrite it. Repair or restore the file, "
        f"or move it away deliberately to start fresh."
    )
