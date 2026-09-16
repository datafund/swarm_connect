"""Test runs must not write to the state files a real gateway uses (#335).

Five services keep state in module-level singletons: the stamp pool inventory,
the pool daily allowance, the stamp ownership registry, the bandwidth credit
ledger and the daily spend budget. All five default to paths under `data/`, and
a test run wrote to every one of them.

On a machine also running a local gateway that means a test run overwrites live
state. The ownership registry is the damaging one — `check_access` denies
batches it has no record of, so a clobbered registry makes uploads that worked a
minute ago start failing with nothing to indicate why. It also let state leak
between runs, so a suite passing on a clean checkout could behave differently the
second time.

`data/` is gitignored, which is why this went unnoticed: the files appear, are
invisible to git, and nothing fails.
"""
import os

import pytest

from app.core.config import settings

STATE_SETTINGS = [
    "STAMP_POOL_STATE_FILE",
    "POOL_ALLOWANCE_STATE_FILE",
    "STAMP_OWNERSHIP_FILE",
    "BANDWIDTH_CREDIT_STATE_FILE",
    "STAMP_SPEND_BUDGET_STATE_FILE",
]

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.mark.parametrize("name", STATE_SETTINGS)
def test_state_files_are_redirected_away_from_the_repository(name):
    """Every persisted path must be outside the working tree during a test run.

    conftest sets these before the app is imported, because the singletons are
    constructed at import time and a fixture would run too late.
    """
    path = os.path.abspath(getattr(settings, name))
    assert not path.startswith(REPO_ROOT + os.sep), (
        f"{name} points inside the repository at {path}; a test run would write "
        "to the state a local gateway is using"
    )


@pytest.mark.parametrize("name", STATE_SETTINGS)
def test_state_files_are_not_the_production_default(name):
    """Catches a setting being added to the list but not to conftest, which would
    otherwise look correct here while still writing to data/."""
    assert "data/" not in getattr(settings, name).replace(os.sep, "/").rsplit("/", 2)[0] + "/", (
        f"{name} still resolves under data/"
    )


def test_every_persisted_service_is_covered():
    """A new service with its own state file must be added to conftest.

    Without this, the next persisted singleton silently starts writing to the
    working tree again and nothing says so — which is exactly how the five above
    accumulated.
    """
    import re

    config_src = open(os.path.join(REPO_ROOT, "app", "core", "config.py")).read()
    declared = set(re.findall(r"^\s{4}([A-Z0-9_]*(?:STATE_FILE|OWNERSHIP_FILE)):", config_src, re.M))

    missing = declared - set(STATE_SETTINGS)
    assert not missing, (
        f"settings {sorted(missing)} persist state but are not redirected in "
        "tests/conftest.py, so a test run writes to the real path"
    )


def test_the_repository_data_directory_is_untouched():
    """The direct statement of the defect: running tests must not create data/."""
    data_dir = os.path.join(REPO_ROOT, "data")
    if not os.path.exists(data_dir):
        return
    # If it exists it belongs to a local gateway, not to this run — it must not
    # contain anything this run put there.
    for name in os.listdir(data_dir):
        path = os.path.join(data_dir, name)
        for setting in STATE_SETTINGS:
            assert os.path.abspath(getattr(settings, setting)) != os.path.abspath(path), (
                f"{setting} resolves to {path}, inside the repository data directory"
            )
