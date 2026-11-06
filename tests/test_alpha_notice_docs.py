"""The alpha notice and the real-money warnings in the docs (#685, #687).

By maintainer decision (2026-11-06) there are no full terms: ALPHA_NOTICE.md
stands in for them, and the draft TERMS.md is gone (git history keeps it).
"""
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CONTACT = "info@datafund.io"


def _tracked_markdown():
    out = subprocess.run(["git", "ls-files", "*.md"], cwd=REPO, capture_output=True, text=True,
                         check=True).stdout.split()
    return [REPO / p for p in out]


def test_alpha_notice_says_what_was_decided():
    text = (REPO / "ALPHA_NOTICE.md").read_text()
    assert "alpha" in text.lower()
    assert "no guarantees" in text.lower()
    assert "Refunds should not be expected" in text
    assert "small amounts" in text
    assert CONTACT in text
    assert "not a refund contact" in text
    assert "provenance-gateway.dev.datafund.io" in text
    assert "free tier" in text.lower()


def test_draft_terms_are_gone():
    assert not (REPO / "TERMS.md").exists()
    for path in _tracked_markdown():
        assert "TERMS.md" not in path.read_text(), path


def test_no_markdown_calls_the_software_beta():
    for path in _tracked_markdown():
        assert not re.search(r"\bbeta\b", path.read_text(), re.I), path


# Docs where "refund" is not a promise to a paying caller, and why.
NOT_FOR_PAYING_CALLERS = {
    # Operator-only: section 3 "Refunds and reconciliation" is what an operator
    # may choose to do, by hand, from the audit log.
    "docs/mainnet-runbook.md": "operator action",
    "docs/x402-operator-guide.md": "operator action (\"the usual refund path\")",
    "docs/deployment-guide.md": "operator action (a purchase recorded for a refund)",
    # Developer notes on internal code paths ("refund path", a free-tier or
    # bandwidth-credit counter given back), not a promise of money back.
    "CLAUDE.md": "internal code paths",
    # Prepaid bandwidth bytes debited and given back when Bee refuses a chunk:
    # gateway-internal credit, not a payment refund.
    "docs/chunk-forwarding-guide.md": "bandwidth credit, not money",
}
# Wording that says the opposite of a promise.
NOT_A_PROMISE = (
    "refunds should not be expected",
    "not a refund contact",
    "not a refund channel",
)


def test_no_public_doc_promises_a_refund():
    """#685: outside the docs listed above, "refund" only says not to expect one."""
    offending = []
    for path in _tracked_markdown():
        rel = str(path.relative_to(REPO))
        if rel in NOT_FOR_PAYING_CALLERS:
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            text = line.lower()
            for phrase in NOT_A_PROMISE:
                text = text.replace(phrase, "")
            if "refund" in text:
                offending.append(f"{rel}:{number}: {line.strip()[:120]}")
    assert not offending, "\n".join(offending)


def test_refund_allowlist_is_not_stale():
    tracked = {str(p.relative_to(REPO)) for p in _tracked_markdown()}
    for rel in NOT_FOR_PAYING_CALLERS:
        assert rel in tracked, rel
        assert "refund" in (REPO / rel).read_text().lower(), f"{rel} no longer needs the exemption"


@pytest.mark.parametrize("doc", ["README.md", "docs/x402-client-integration.md"])
def test_docs_warn_of_real_money_and_link_the_notice(doc):
    text = (REPO / doc).read_text()
    assert "ALPHA_NOTICE.md" in text
    assert "real USDC on Base mainnet" in text
    assert "provenance-gateway.dev.datafund.io" in text
    assert CONTACT in text
