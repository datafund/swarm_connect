# app/core/version.py
"""Dynamic version generation from git metadata or VERSION file."""
import subprocess
from functools import lru_cache
from pathlib import Path


VERSION_FILE = Path(__file__).parent.parent.parent / "VERSION"


@lru_cache()
def get_version() -> str:
    """Generate version string from git or VERSION file.

    Format: 0.<commit_count>.<short_hash>
    Example: 0.71.840eba4

    Priority:
    1. VERSION file (for Docker/production)
    2. Git commands (for local development)
    3. Fallback to 0.0.0-unknown
    """
    # Try VERSION file first (used in Docker builds)
    if VERSION_FILE.exists():
        version = VERSION_FILE.read_text().strip()
        if version:
            return version

    # Try git commands (local development)
    try:
        commit_count = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            capture_output=True,
            text=True,
            check=True
        ).stdout.strip()

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
