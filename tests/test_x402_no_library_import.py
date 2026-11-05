"""app/ does not import the x402 library (#604).

The gateway owns its v1 models, base64 helpers and facilitator client
(app/x402/models_v1.py, encoding.py, facilitator_client.py). x402==1.0.0 stays
installed only as a test oracle; importing it from app/ again would quietly
make it a runtime dependency.
"""
import ast
import subprocess
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "app"


def test_app_never_imports_the_x402_library():
    """app/ owns its x402 code (#604); x402==1.0.0 is installed for tests only."""
    offenders = []
    for path in sorted(APP_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name == "x402" or name.startswith("x402."):
                    offenders.append(f"{path.relative_to(APP_DIR.parent)}:{node.lineno} {name}")
    assert offenders == []
    assert any(APP_DIR.rglob("*.py"))


def test_the_app_imports_with_the_library_blocked():
    """The whole app (every router, the x402 modules) loads without x402 at all."""
    code = ("import sys; sys.modules['x402'] = None; "
            "import app.main, app.x402.dependency, app.x402.middleware, app.x402.settlement, "
            "app.x402.facilitator; print('ok')")
    result = subprocess.run([sys.executable, "-c", code], cwd=APP_DIR.parent,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().endswith("ok")
