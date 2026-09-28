"""docs/error-codes.md lists exactly the error `code`s raised in app/ (#381).

Grep-based on purpose: codes are string literals next to "code", or the first
argument of the body-limit middleware's _error(), so a new one added without a
line in the reference fails here, and so does a documented code that is no
longer raised. A code built in a constant or an f-string is invisible to the
scan; keep codes as literals at the raise site.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "error-codes.md"
CODE_PATTERNS = [
    re.compile(r"""["']code["']\s*:\s*["']([A-Z][A-Z0-9_]+)["']"""),  # {"code": "X"}
    re.compile(r"""\bcode\s*=\s*["']([A-Z][A-Z0-9_]+)["']"""),          # code="X"
    re.compile(r"""\b_error\(\s*["']([A-Z][A-Z0-9_]+)["']"""),          # _error("X", ...)
]
# A documented row: | `CODE` | ...
DOC_ROW = re.compile(r"^\|\s*`([A-Z][A-Z0-9_]+)`\s*\|", re.M)


def _codes_in_app():
    found = {}
    for path in (ROOT / "app").rglob("*.py"):
        text = path.read_text()
        for pattern in CODE_PATTERNS:
            for code in pattern.findall(text):
                found.setdefault(code, path.relative_to(ROOT))
    return found


def test_scan_finds_the_codes():
    # Guards the scan itself: an empty result would make the tests below pass vacuously.
    codes = _codes_in_app()
    assert len(codes) >= 30
    assert "BODY_TOO_LARGE" in codes  # positional _error("...") form


def test_every_code_is_documented():
    doc = DOC.read_text()
    missing = {c: str(p) for c, p in _codes_in_app().items() if f"`{c}`" not in doc}
    assert not missing, f"Add these codes to docs/error-codes.md: {missing}"


def test_every_documented_code_is_raised():
    documented = set(DOC_ROW.findall(DOC.read_text()))
    # HTTP_<status> codes are derived by the app's exception handler, not raised.
    documented = {c for c in documented if not re.fullmatch(r"HTTP_\d{3}", c)}
    stale = documented - set(_codes_in_app())
    assert not stale, f"Documented in docs/error-codes.md but not raised in app/: {sorted(stale)}"
