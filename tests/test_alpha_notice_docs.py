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


@pytest.mark.parametrize("doc", ["README.md", "ALPHA_NOTICE.md", "docs/x402-client-integration.md",
                                 "docs/error-codes.md", "docs/x402-operator-guide.md"])
def test_docs_promise_no_refund(doc):
    text = (REPO / doc).read_text().lower()
    assert "for a refund" not in text and "or a refund" not in text, doc


@pytest.mark.parametrize("doc", ["README.md", "docs/x402-client-integration.md"])
def test_docs_warn_of_real_money_and_link_the_notice(doc):
    text = (REPO / doc).read_text()
    assert "ALPHA_NOTICE.md" in text
    assert "real USDC on Base mainnet" in text
    assert "provenance-gateway.dev.datafund.io" in text
    assert CONTACT in text
