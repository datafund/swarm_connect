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

# Strings are stripped one chunk at a time: re.sub over the whole document
# keeps one piece per gap between strings, which for a document dense with
# short strings ("","",...) cost about 60 times its size in memory.
_CHUNK = 64 * 1024


def _walk(part: bytes, depth: int, max_depth: int):
    """Depth after the brackets of `part` (strings already removed), and
    whether it stayed within `max_depth` on the way."""
    steps = part.translate(_BRACKETS, _NOT_BRACKETS)
    if not steps:
        return depth, True
    deepest = max(accumulate(memoryview(steps).cast("b"), initial=depth))
    opens = steps.count(1)
    return depth + opens - (len(steps) - opens), deepest <= max_depth


def json_depth_within(data: bytes, max_depth: int, _chunk: int = _CHUNK) -> bool:
    """True if no object or array in `data` is nested deeper than `max_depth`.

    Brackets inside JSON strings are not counted, escaped quotes and
    backslashes included. Each step is a C-level bytes or re operation, on
    the whole input or on one chunk of it, so a 10 MB document takes a
    fraction of a second, and memory stays at a few copies of the input.

    Meant for valid JSON. On malformed input (an unterminated string, a
    backslash outside a string) the answer may be either, and the parser
    refuses the document afterwards anyway. `_chunk` is for tests only.
    """
    # Remove escaped backslashes first, then escaped quotes. bytes.replace
    # works left to right without overlaps, which pairs a run of backslashes
    # the same way a JSON parser does, so a quote left after this ends or
    # starts a string.
    data = data.replace(b"\\\\", b"").replace(b'\\"', b"")

    # Quotes now pair up in order: each one opens or closes a string. A
    # string left open at the end of a chunk continues into the next one.
    depth = 0
    pos, size = 0, len(data)
    opened_at = -1  # where the string still open began, or -1 outside one
    while pos < size:
        if opened_at >= 0:
            close = data.find(b'"', pos, pos + _chunk)
            if close < 0:
                pos += _chunk
            else:
                opened_at, pos = -1, close + 1
            continue
        part = data[pos:pos + _chunk]
        stripped = _STRING.sub(b"", part)
        unpaired = stripped.find(b'"')  # at most one, and the last quote
        if unpaired >= 0:
            stripped = stripped[:unpaired]
            opened_at = pos + part.rfind(b'"')
            pos = opened_at + 1
        else:
            pos += _chunk
        depth, within = _walk(stripped, depth, max_depth)
        if not within:
            return False

    if opened_at >= 0:
        # A string never closed. As before chunking, the brackets after its
        # opening quote count: none of them is followed by another quote.
        for start in range(opened_at + 1, size, _chunk):
            depth, within = _walk(data[start:start + _chunk], depth, max_depth)
            if not within:
                return False
    return True
