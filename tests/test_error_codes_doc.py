"""Every error `code` raised in app/ is documented in docs/error-codes.md (#381).

Grep-based on purpose: codes are string literals next to "code", so a new one
added without a line in the reference fails here.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CODE_PATTERNS = [
    re.compile(r"""["']code["']\s*:\s*["']([A-Z][A-Z0-9_]+)["']"""),  # {"code": "X"}
    re.compile(r"""\bcode\s*=\s*["']([A-Z][A-Z0-9_]+)["']"""),          # code="X"
]


def _codes_in_app():
    found = {}
    for path in (ROOT / "app").rglob("*.py"):
        text = path.read_text()
        for pattern in CODE_PATTERNS:
            for code in pattern.findall(text):
                found.setdefault(code, path.relative_to(ROOT))
    return found


def test_scan_finds_the_codes():
    # Guards the scan itself: an empty result would make the test below pass vacuously.
    assert len(_codes_in_app()) >= 30


def test_every_code_is_documented():
    doc = (ROOT / "docs" / "error-codes.md").read_text()
    missing = {c: str(p) for c, p in _codes_in_app().items() if f"`{c}`" not in doc}
    assert not missing, f"Add these codes to docs/error-codes.md: {missing}"
