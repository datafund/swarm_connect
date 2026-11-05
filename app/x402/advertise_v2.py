# app/x402/advertise_v2.py
"""Advertising x402 v2 on a 402: the PAYMENT-REQUIRED header (#608, D2 and D10 in #596).

Every x402 Payment Required answer the gateway sends is an HTTPException(402)
whose detail holds the v1 body (x402Version 1, error, accepts), and every one
leaves through middleware._spec_shaped_402. That is where these are applied,
so the header is built from the body itself: its offers are the body's v1
offers in v2 form (v2_payment.v2_offer_from_v1, the builder #607 checks a v2
payment against), and they can never describe different payments.

With 2 not enabled nothing here does anything: the default 402 is unchanged
byte for byte. With 2 enabled:

- payment_required_header(detail): the PAYMENT-REQUIRED value, codec_v2 of a
  PaymentRequired with x402Version 2, the body's `error`, `resource` (url,
  description, mimeType of accepts[0]) and `accepts` (every v1 offer, in v2
  form). A body without offers gets none. If it cannot be built (the v2
  model refuses a value) or encoded (over codec_v2.MAX_HEADER_CHARS), the 402
  goes out without it: logged, counted in
  gateway_x402_payment_required_header_failures_total, never a 500.
- The v1 body stays (D2): v1 clients read it, and @x402/core reads the header.
"""
import logging
from typing import Optional

from pydantic import ValidationError

from app.x402 import codec_v2
from app.x402.models_v1 import PaymentRequirements
from app.x402.models_v2 import PaymentRequired, ResourceInfo
from app.x402.payment_headers import accepted_x402_versions

logger = logging.getLogger(__name__)

PAYMENT_REQUIRED_HEADER = codec_v2.PAYMENT_REQUIRED_HEADER


def _count_failure(reason: str) -> None:
    from app.services.metrics import x402_payment_required_header_failures_total
    x402_payment_required_header_failures_total.labels(reason=reason).inc()


def _v1_offers(detail) -> list:
    """The body's v1 offers, or [] when it is not an x402 Payment Required body."""
    if not isinstance(detail, dict) or detail.get("x402Version") != 1:
        return []
    accepts = detail.get("accepts")
    return accepts if isinstance(accepts, list) else []


def payment_required_v2(detail) -> Optional[PaymentRequired]:
    """The v2 PaymentRequired for a v1 402 body, or None (v2 off, no offer).

    Raises ValidationError or ValueError when an offer cannot be expressed in
    v2; the callers below count that."""
    if 2 not in accepted_x402_versions():
        return None
    offers = _v1_offers(detail)
    if not offers:
        return None
    from app.x402.v2_payment import v2_offer_from_v1
    v1 = [PaymentRequirements.model_validate(o) for o in offers]
    error = detail.get("error")
    return PaymentRequired(
        x402_version=2,
        error=error if isinstance(error, str) else None,
        resource=ResourceInfo(url=v1[0].resource, description=v1[0].description,
                              mime_type=v1[0].mime_type),
        accepts=[v2_offer_from_v1(r) for r in v1],
    )


def _build(detail):
    """(PaymentRequired or None, failure reason or None), logging a failure."""
    try:
        return payment_required_v2(detail), None
    except (ValidationError, ValueError, TypeError) as e:
        # Never the values: the error type only.
        logger.warning(f"x402: cannot express this 402's offer in x402 v2 ({type(e).__name__}); "
                       f"sending it without {PAYMENT_REQUIRED_HEADER}")
        return None, "invalid"


def payment_required_header(detail) -> Optional[str]:
    """The PAYMENT-REQUIRED value for a v1 402 body, or None (see the module docstring)."""
    message, failure = _build(detail)
    if failure is not None:
        _count_failure(failure)
        return None
    if message is None:
        return None
    try:
        return codec_v2.encode_payment_required(message)
    except codec_v2.PaymentHeaderTooLarge as e:
        logger.warning(f"x402: {PAYMENT_REQUIRED_HEADER} not sent: {e}")
        _count_failure("too_large")
    except (codec_v2.X402HeaderError, ValueError, TypeError) as e:
        logger.warning(f"x402: {PAYMENT_REQUIRED_HEADER} not sent ({type(e).__name__})")
        _count_failure("invalid")
    return None

