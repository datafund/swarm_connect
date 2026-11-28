"""
Tests for the shared JSON depth check (app/core/json_depth.py, #695).
"""
import json
import random
import time

import pytest

from app.core.json_depth import json_depth_within
from app.middleware.body_limit import _check_nesting_depth


def nested_arrays(depth: int) -> bytes:
    return b"[" * depth + b"]" * depth


def nested_objects(depth: int) -> bytes:
    return b'{"a":' * depth + b"1" + b"}" * depth


def reference_depth_within(data: bytes, max_depth: int) -> bool:
    """The byte loop the body-limit middleware used before #695, kept as the
    reference the shared helper must agree with on valid JSON."""
    depth = 0
    in_string = False
    escape = False
    for char in data:
        if escape:
            escape = False
            continue
        if char == 0x5C and in_string:
            escape = True
            continue
        if char == 0x22:
            in_string = not in_string
            continue
        if in_string:
            continue
        if char in (0x7B, 0x5B):
            depth += 1
            if depth > max_depth:
                return False
        elif char in (0x7D, 0x5D):
            depth -= 1
    return True


class TestLimits:
    @pytest.mark.parametrize("build", [nested_arrays, nested_objects])
    def test_just_under_at_and_over(self, build):
        assert json_depth_within(build(63), 64) is True
        assert json_depth_within(build(64), 64) is True
        assert json_depth_within(build(65), 64) is False

    def test_mixed_arrays_and_objects(self):
        data = b'{"a":[{"b":[1]}]}'  # depth 4
        assert json_depth_within(data, 4) is True
        assert json_depth_within(data, 3) is False

    def test_siblings_do_not_add_up(self):
        data = b'[[1],[2],{"a":[3]},[[4]]]'  # depth 3
        assert json_depth_within(data, 3) is True
        assert json_depth_within(data, 2) is False

    @pytest.mark.parametrize("data", [b"", b"1", b'"x"', b"null", b"{}", b"[]"])
    def test_flat_inputs(self, data):
        assert json_depth_within(data, 1) is True


class TestStrings:
    def test_brackets_inside_strings_do_not_count(self):
        data = json.dumps({"a": "[[[[{{{{" * 100}).encode()
        assert json_depth_within(data, 1) is True

    def test_escaped_quote_does_not_end_the_string(self):
        # The string is  ab"[[[[cd  : the brackets are still inside it.
        data = b'{"a":"ab\\"[[[[cd"}'
        assert json.loads(data) == {"a": 'ab"[[[[cd'}
        assert json_depth_within(data, 1) is True

    def test_escaped_backslash_before_closing_quote(self):
        # The string is  x\  and closes; the brackets after it count.
        data = b'{"a":"x\\\\","b":[[1]]}'
        assert json.loads(data) == {"a": "x\\", "b": [[1]]}
        assert json_depth_within(data, 3) is True
        assert json_depth_within(data, 2) is False

    def test_backslash_runs(self):
        # \\\" inside a string: an escaped backslash, then an escaped quote.
        data = b'["a\\\\\\"[[[", [[1]]]'
        assert json.loads(data) == ['a\\"[[[', [[1]]]
        assert json_depth_within(data, 3) is True
        assert json_depth_within(data, 2) is False

    def test_unicode_and_unicode_escapes(self):
        data = json.dumps({"a": "\u017e[\u0022]", "b": ["\u00e9"]}, ensure_ascii=False).encode()
        assert json_depth_within(data, 2) is True
        assert json_depth_within(data, 1) is False


class TestMatchesPreviousCheck:
    """The helper replaced the middleware's byte loop; on valid JSON the two
    must give the same answer at every limit."""

    ALPHABET = '[]{}"\\ a,:\u017e'

    def _value(self, rng, depth):
        kind = rng.random()
        if depth > 0 and kind < 0.35:
            return [self._value(rng, depth - 1) for _ in range(rng.randint(0, 3))]
        if depth > 0 and kind < 0.7:
            return {self._string(rng): self._value(rng, depth - 1) for _ in range(rng.randint(0, 3))}
        if kind < 0.85:
            return self._string(rng)
        return rng.choice([1, 2.5, None, True, False])

    def _string(self, rng):
        return "".join(rng.choice(self.ALPHABET) for _ in range(rng.randint(0, 8)))

    def test_random_documents(self):
        rng = random.Random(695)
        for _ in range(500):
            doc = self._value(rng, rng.randint(0, 12))
            data = json.dumps(doc, ensure_ascii=rng.random() < 0.5).encode()
            for limit in range(0, 14):
                assert json_depth_within(data, limit) == reference_depth_within(data, limit), data

    def test_middleware_wrapper_uses_the_helper(self):
        assert _check_nesting_depth(nested_arrays(20), 20) is True
        assert _check_nesting_depth(nested_arrays(21), 20) is False


class TestLinearTime:
    """Each input below is one that a quadratic implementation is slow on: the
    regex "[^"\\]*(?:\\.[^"\\]*)*" took 12 s on 80 KB of \\" pairs. The
    linear helper takes milliseconds, so a 5 s bound leaves a slow CI runner
    plenty of room and still fails a quadratic version."""

    SIZE = 200_000
    INPUTS = {
        "escaped quotes, no closing quote": lambda n: b'"' + b'\\"' * (n // 2),
        "backslashes": lambda n: b'"' + b"\\" * n,
        "quotes": lambda n: b'"' * (n + 1),
        "unterminated string then brackets": lambda n: b'"' + b"[" * n,
        "deep arrays": lambda n: nested_arrays(n // 2),
        "many shallow arrays": lambda n: b"[" + b"[]," * (n // 3) + b"[]]",
    }

    @pytest.mark.parametrize("name", list(INPUTS))
    def test_bounded_time(self, name):
        data = self.INPUTS[name](self.SIZE)
        start = time.perf_counter()
        json_depth_within(data, 64)
        assert time.perf_counter() - start < 5.0, name
