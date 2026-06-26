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
import tempfile
from typing import Any

__all__ = ["atomic_write_json"]


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
