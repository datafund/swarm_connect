# app/core/json_depth.py
"""
Nesting depth of a JSON document, measured on its bytes before parsing.

json.loads recurses once per level and raises RecursionError on a deeply
nested document, at a depth that depends on the interpreter. Checking the
depth first refuses such a document with a clear answer instead (#695).

Shared by the JSON body limit (app/middleware/body_limit.py) and the notary
document check (app/services/provenance.py).
"""
import re
from itertools import accumulate

# A JSON string with no backslash left in it. Linear: with escapes removed
# first, the only string this can fail to close is the last one. The usual
# pattern that also skips escapes ("[^"\\]*(?:\\.[^"\\]*)*") is quadratic on
# a run of \" pairs with no closing quote, so it must not be used here.
_STRING = re.compile(rb'"[^"]*"')

# Opening brackets become +1 and closing ones -1 (0xFF as a signed byte);
# every other byte is deleted.
_BRACKETS = bytes.maketrans(b"[{]}", b"\x01\x01\xff\xff")
_NOT_BRACKETS = bytes(c for c in range(256) if c not in b"[]{}")


def json_depth_within(data: bytes, max_depth: int) -> bool:
    """True if no object or array in `data` is nested deeper than `max_depth`.

    Brackets inside JSON strings are not counted, escaped quotes and
    backslashes included. Every step is a single pass of a C-level bytes or
    re operation, so a 10 MB document takes a fraction of a second.

    Meant for valid JSON. On malformed input (an unterminated string, a
    backslash outside a string) the answer may be either, and the parser
    refuses the document afterwards anyway.
    """
    # Remove escaped backslashes first, then escaped quotes. bytes.replace
    # works left to right without overlaps, which pairs a run of backslashes
    # the same way a JSON parser does, so a quote left after this ends or
    # starts a string.
    data = data.replace(b"\\\\", b"").replace(b'\\"', b"")
    steps = _STRING.sub(b"", data).translate(_BRACKETS, _NOT_BRACKETS)
    return max(accumulate(memoryview(steps).cast("b")), default=0) <= max_depth
