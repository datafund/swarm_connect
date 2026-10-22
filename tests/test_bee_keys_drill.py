"""#494: the Bee keystore restore drill, against real Docker, age and Bee.

Opt-in: skipped unless RUN_BEE_DRILL=1, because it needs a local Docker daemon,
the `age` CLI and the pinned Bee image. It uses a throwaway identity and runs
every Bee container with --network none. See scripts/bee_keys_drill.sh.

    RUN_BEE_DRILL=1 python -m pytest tests/test_bee_keys_drill.py -s
"""
import os
import subprocess
from pathlib import Path

import pytest

DRILL = Path(__file__).resolve().parent.parent / "scripts" / "bee_keys_drill.sh"

pytestmark = pytest.mark.skipif(os.environ.get("RUN_BEE_DRILL") != "1",
                                reason="Docker drill; set RUN_BEE_DRILL=1 to run")


def test_restored_node_has_the_original_address_and_overlay(tmp_path):
    env = dict(os.environ, TMPDIR=str(tmp_path))
    r = subprocess.run(["bash", str(DRILL)], env=env, capture_output=True, text=True, timeout=900)
    print(r.stdout)
    print(r.stderr)
    assert r.returncode == 0
    assert "DRILL PASSED" in r.stdout
    assert "identity matches" in r.stdout
