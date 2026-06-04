# tests/test_atomic_io.py
"""Tests for atomic JSON file writes (app/core/atomic_io.py).

Covers GitHub Issue #212: state files must be written atomically so a crash
mid-write cannot leave a truncated/corrupt file on disk.
"""
import json
import os
from unittest.mock import patch

import pytest

from app.core.atomic_io import atomic_write_json


def _list_temp_files(directory):
    """Return any leftover temp files created by atomic_write_json."""
    return [name for name in os.listdir(directory) if name.startswith(".tmp-")]


class TestAtomicWriteJson:
    """Behaviour of atomic_write_json()."""

    def test_round_trip_dict(self, tmp_path):
        target = str(tmp_path / "state.json")
        data = {"a": 1, "b": [2, 3], "c": "x"}

        atomic_write_json(target, data)

        with open(target) as f:
            assert json.load(f) == data

    def test_round_trip_list(self, tmp_path):
        target = str(tmp_path / "state.json")
        atomic_write_json(target, ["batch_a", "batch_b"])

        with open(target) as f:
            assert json.load(f) == ["batch_a", "batch_b"]

    def test_creates_parent_directories(self, tmp_path):
        target = str(tmp_path / "nested" / "deeper" / "state.json")
        atomic_write_json(target, {"ok": True})

        assert os.path.exists(target)

    def test_overwrites_existing_file(self, tmp_path):
        target = str(tmp_path / "state.json")
        atomic_write_json(target, {"version": 1})
        atomic_write_json(target, {"version": 2})

        with open(target) as f:
            assert json.load(f) == {"version": 2}

    def test_no_temp_file_left_after_success(self, tmp_path):
        target = str(tmp_path / "state.json")
        atomic_write_json(target, {"ok": True})

        assert _list_temp_files(str(tmp_path)) == []

    def test_existing_file_preserved_on_write_failure(self, tmp_path):
        """If the rename fails mid-write, the original file stays intact.

        This is the core crash-safety property: a failed write must never
        truncate or corrupt the previously-persisted state.
        """
        target = str(tmp_path / "state.json")
        atomic_write_json(target, {"version": "original"})

        # Simulate a failure at the final atomic-rename step.
        with patch("app.core.atomic_io.os.replace", side_effect=OSError("boom")):
            with pytest.raises(OSError):
                atomic_write_json(target, {"version": "new"})

        # Original content must be untouched...
        with open(target) as f:
            assert json.load(f) == {"version": "original"}
        # ...and no temp file should be left behind.
        assert _list_temp_files(str(tmp_path)) == []

    def test_no_temp_file_left_on_serialization_failure(self, tmp_path):
        """A non-serializable payload raises but leaves no litter."""
        target = str(tmp_path / "state.json")

        with pytest.raises(TypeError):
            atomic_write_json(target, {"bad": object()})

        assert _list_temp_files(str(tmp_path)) == []
        assert not os.path.exists(target)
