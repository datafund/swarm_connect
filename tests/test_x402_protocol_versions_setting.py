"""X402_PROTOCOL_VERSIONS: which x402 versions a payment is accepted in (#607).

Values "1" (default), "1,2" and "2", whitespace and order free. Anything else
stops the gateway at startup: Settings refuses it, whether or not x402 is on.
"""
import pytest
from pydantic import ValidationError

from app.core.config import Settings, parse_x402_protocol_versions


def _settings(value):
    return Settings(SWARM_BEE_API_URL="http://bee.invalid:1633", X402_PROTOCOL_VERSIONS=value)


@pytest.mark.parametrize("value,expected", [
    ("1", (1,)), ("1,2", (1, 2)), ("2", (2,)), ("2,1", (1, 2)), (" 1 , 2 ", (1, 2)),
])
def test_valid_values(value, expected):
    s = _settings(value)
    assert s.x402_protocol_versions() == expected
    assert s.X402_PROTOCOL_VERSIONS == ",".join(str(v) for v in expected)


@pytest.mark.parametrize("value", ["", " ", "3", "0", "1,1", "1,2,3", "v2", "1;2", "1,", "12"])
def test_unknown_values_stop_startup(value):
    with pytest.raises(ValidationError) as exc:
        _settings(value)
    assert "X402_PROTOCOL_VERSIONS" in str(exc.value)


def test_default_is_v1_only():
    assert Settings(SWARM_BEE_API_URL="http://bee.invalid:1633").x402_protocol_versions() == (1,)


def test_parse_refuses_non_text():
    with pytest.raises(ValueError):
        parse_x402_protocol_versions(2)
