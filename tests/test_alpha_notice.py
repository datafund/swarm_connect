"""Alpha notice, feedback contact and real-money warnings (#685, #686, #687).

The hosted gateway takes real USDC on Base mainnet and is alpha software. By
maintainer decision (2026-11-06) there are no full terms, only an alpha notice
(ALPHA_NOTICE.md): no guarantees, refunds should not be expected, spend small
amounts, test on staging or the free tier first. SUPPORT_CONTACT is a feedback
contact, not a refund channel. Messages to a caller who may have paid for
nothing name it when it is set, link the notice when TERMS_URL is set, and
never promise a refund. With either setting empty they leave no placeholder.
"""
import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from fastapi.testclient import TestClient

from app.core.config import settings

REPO = Path(__file__).resolve().parent.parent
CONTACT = "info@datafund.io"
NOTICE = "https://github.com/datafund/swarm_connect/blob/main/ALPHA_NOTICE.md"

# (SUPPORT_CONTACT, TERMS_URL): each set, unset, and both.
SETTING_CASES = [("", ""), (CONTACT, ""), ("", NOTICE), (CONTACT, NOTICE)]
SETTING_IDS = ["neither", "contact", "notice", "both"]


def configure(monkeypatch, contact, terms):
    monkeypatch.setattr(settings, "SUPPORT_CONTACT", contact)
    monkeypatch.setattr(settings, "TERMS_URL", terms)


def check_message(message, contact, terms):
    """A message for a caller who may have paid for nothing."""
    assert "refund" not in message.lower(), message
    if contact:
        assert contact in message, message
    if terms:
        assert terms in message, message
        assert "alpha" in message, message
    # No empty or unrendered placeholder.
    for bad in ("None", "{", "}", "  ", " .", "to .", "at .", ", ,"):
        assert bad not in message, (bad, message)


# --- the message helper ---

@pytest.mark.parametrize("contact,terms", SETTING_CASES, ids=SETTING_IDS)
def test_report_advice(monkeypatch, contact, terms):
    from app.core.support import report_advice
    configure(monkeypatch, contact, terms)
    text = report_advice("transaction")
    check_message(text, contact, terms)
    assert "transaction" in text
    if not contact:
        assert "gateway operator" in text


@pytest.mark.parametrize("contact,terms", SETTING_CASES, ids=SETTING_IDS)
def test_outcome_unknown_advice(monkeypatch, contact, terms):
    from app.x402.settlement import outcome_unknown_advice
    configure(monkeypatch, contact, terms)
    check_message(outcome_unknown_advice(), contact, terms)


def test_outcome_unknown_advice_unchanged_when_nothing_is_set(monkeypatch):
    """Byte-identical to the text before #686, which tests/golden pins."""
    from app.x402.settlement import outcome_unknown_advice
    configure(monkeypatch, "", "")
    assert outcome_unknown_advice() == (
        "Do not pay again from this response. Check on-chain whether this authorization's nonce "
        "was used. If it was, contact the operator with the nonce. If it was not, the facilitator "
        "may still submit it, so before signing a new payment wait until its validBefore has "
        "passed, or cancel it on-chain with EIP-3009 cancelAuthorization."
    )


@pytest.mark.parametrize("contact,terms", SETTING_CASES, ids=SETTING_IDS)
def test_idempotency_settlement_unknown_message(monkeypatch, contact, terms):
    from app.x402.idempotency import _settlement_unknown_409
    configure(monkeypatch, contact, terms)
    detail = _settlement_unknown_409({"nonce": "0x" + "11" * 32}).detail
    assert detail["code"] == "IDEMPOTENCY_KEY_SETTLEMENT_UNKNOWN"
    assert detail["x402_status"] == "settlement_unknown"
    check_message(detail["message"], contact, terms)


# --- GET / ---

def _root():
    from app.main import app
    with patch("app.services.swarm_api.get_node_status_summary",
               new=AsyncMock(return_value={"healthy": True, "warnings": []})):
        return TestClient(app).get("/").json()


def test_root_has_no_contact_until_configured(monkeypatch):
    configure(monkeypatch, "", "")
    body = _root()
    assert body["support_contact"] is None
    assert body["terms_url"] is None


def test_root_treats_whitespace_only_settings_as_unset(monkeypatch):
    configure(monkeypatch, "  ", " \t")
    body = _root()
    assert body["support_contact"] is None
    assert body["terms_url"] is None


def test_root_strips_the_settings(monkeypatch):
    configure(monkeypatch, f" {CONTACT} ", f"{NOTICE}\n")
    body = _root()
    assert body["support_contact"] == CONTACT
    assert body["terms_url"] == NOTICE


def test_root_shows_the_contact_and_the_notice(monkeypatch):
    configure(monkeypatch, CONTACT, NOTICE)
    body = _root()
    assert body["support_contact"] == CONTACT
    assert body["terms_url"] == NOTICE


# --- deploy.yml ---

DEPLOY_YML = REPO / ".github" / "workflows" / "deploy.yml"
ENV_FILES = {
    "write env file for dev": "/opt/swarm_connect_dev.env",
    "write env file for main": "/opt/swarm_connect.env",
}


def _heredocs():
    workflow = yaml.safe_load(DEPLOY_YML.read_text())
    steps = {s["name"]: s for job in workflow["jobs"].values()
             for s in job.get("steps", []) if "name" in s}
    for name, env_file in ENV_FILES.items():
        m = re.search(r"cat > " + re.escape(env_file) + r" << 'EOF'\n(.*?)\nEOF\n",
                      steps[name]["run"], re.S)
        assert m, f"no heredoc writing {env_file}"
        yield name, [line.strip() for line in m.group(1).splitlines()]


@pytest.mark.parametrize("setting", ["TERMS_URL", "SUPPORT_CONTACT"])
def test_both_environments_write_the_setting_empty_by_default(setting):
    line = f"{setting}=${{{{ vars.{setting} || '' }}}}"
    for name, lines in _heredocs():
        assert lines.count(line) == 1, (name, line)
        assert sum(1 for x in lines if x.startswith(f"{setting}=")) == 1, name
