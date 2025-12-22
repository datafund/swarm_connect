# app/core/version.py
"""Dynamic version generation from git metadata."""
import subprocess
from functools import lru_cache


@lru_cache()
def get_version() -> str:
    """Generate version string from git: 0.<commit_count>.<short_hash>

    Example: 0.71.840eba4
    """
    try:
        # Get commit count
        commit_count = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            capture_output=True,
            text=True,
            check=True
        ).stdout.strip()

        # Get short hash
        short_hash = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True
        ).stdout.strip()

        return f"0.{commit_count}.{short_hash}"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "0.0.0-unknown"


VERSION = get_version()
