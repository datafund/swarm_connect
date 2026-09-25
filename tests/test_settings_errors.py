"""Settings validation errors must not echo secret values (#409 review).

The deploy workflow prints the container's log tail when a new gateway fails
to start, and a settings error is the most likely reason it fails. pydantic
includes the input in its error text by default, which for a missing required
field is the whole settings dict.
"""
import pytest
from pydantic import ValidationError

from app.core.config import Settings

# 64 hex chars each, shaped like private keys, with distinct heads and tails.
NOTARY_SECRET = "a1" * 8 + "c3" * 16 + "d4" * 8
GNOSIS_SECRET = "e5" * 8 + "f6" * 16 + "b2" * 8


def assert_no_secret(text, secret):
    """pydantic truncates long reprs in the middle, so a leak may show only the
    head or the tail of a value: check both ends, not just the whole."""
    assert secret not in text
    assert secret[:12] not in text
    assert secret[-12:] not in text


def test_missing_required_setting_does_not_echo_secrets(monkeypatch):
    monkeypatch.delenv("SWARM_BEE_API_URL", raising=False)
    monkeypatch.setenv("NOTARY_PRIVATE_KEY", NOTARY_SECRET)
    monkeypatch.setenv("GNOSIS_PRIVATE_KEY", GNOSIS_SECRET)

    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None)

    text = str(exc.value)
    assert "SWARM_BEE_API_URL" in text  # still says what is wrong
    assert "input_value" not in text
    assert_no_secret(text, NOTARY_SECRET)
    assert_no_secret(text, GNOSIS_SECRET)


def test_invalid_secret_value_is_not_echoed(monkeypatch):
    monkeypatch.setenv("SWARM_BEE_API_URL", "http://localhost:1")
    # A value pasted into the wrong variable is echoed back by that field's error.
    monkeypatch.setenv("NOTARY_ENABLED", NOTARY_SECRET)

    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None)

    assert_no_secret(str(exc.value), NOTARY_SECRET)
