"""Where a caller who may have paid for nothing is pointed (#686).

The hosted gateway is alpha software: by maintainer decision there are no full
terms, only an alpha notice (ALPHA_NOTICE.md, published at TERMS_URL), and
refunds should not be expected. SUPPORT_CONTACT is where problems and feedback
go to the developers and the organisation; it is not a refund channel, so no
message built here promises one.

Read at call time, not import time, so the text follows the settings. With a
setting empty, its part is left out rather than rendered as a placeholder.
"""
from app.core.config import settings


def support_contact() -> str:
    """SUPPORT_CONTACT, stripped; '' when unset."""
    return (settings.SUPPORT_CONTACT or "").strip()


def notice_sentence() -> str:
    """' The gateway is alpha software; see the notice at <TERMS_URL>.', or '' when unset."""
    terms = (settings.TERMS_URL or "").strip()
    return f" The gateway is alpha software; see the notice at {terms}." if terms else ""


def report_advice(item: str) -> str:
    """'Report this, with the <item>, to <contact>.' plus the notice sentence.

    Falls back to "the gateway operator" when SUPPORT_CONTACT is empty.
    """
    contact = support_contact() or "the gateway operator"
    return f"Report this, with the {item}, to {contact}." + notice_sentence()


def report_nonce_clause() -> str:
    """How a caller whose nonce was used reports it, completing 'If it was, ...'.

    With SUPPORT_CONTACT empty it is the wording from before #686, which the
    golden settle-refusal responses pin byte for byte.
    """
    contact = support_contact()
    return f"report it, with the nonce, to {contact}" if contact else "contact the operator with the nonce"
