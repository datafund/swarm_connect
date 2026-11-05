# app/x402/models_v1.py
"""The x402 v1 wire types the gateway uses (#604, D1 in #596).

These used to come from the x402==1.0.0 library (x402.types). The gateway owns
them now, so v1 and v2 can live side by side whatever the library does next.
They are copies of the 1.0.0 shapes, and must stay byte-identical on the wire:

- the same fields in the same order (model_dump decides the key order of every
  402 body and of the X-PAYMENT-RESPONSE header);
- the same camelCase aliases, with population by field name allowed;
- the same validators: amounts are integers encoded as strings, and
  PaymentRequirements.network is one of the four v1 names 1.0.0 knows;
- unknown fields are dropped (pydantic's default), so a client payload with
  extra fields reaches the facilitator without them, exactly as before;
- VerifyResponse.payer is required but may be null, as in 1.0.0: a verify
  answer without "payer" fails to parse.

tests/test_x402_own_models.py checks every point against x402 1.0.0, which
stays installed as a test oracle only. No v2 shapes here: those are #606.
"""
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

# The v1 network names x402 1.0.0 accepts in PaymentRequirements.
SupportedNetworks = Literal["base", "base-sepolia", "avalanche-fuji", "avalanche"]


def _camel() -> ConfigDict:
    return ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)


def _integer_string(v, what):
    try:
        int(v)
    except ValueError:
        raise ValueError(f"{what} must be an integer encoded as a string")
    return v


class PaymentRequirements(BaseModel):
    scheme: str
    network: SupportedNetworks
    max_amount_required: str
    resource: str
    description: str
    mime_type: str
    output_schema: Optional[Any] = None
    pay_to: str
    max_timeout_seconds: int
    asset: str
    extra: Optional[dict[str, Any]] = None

    model_config = _camel()

    @field_validator("max_amount_required")
    def validate_max_amount_required(cls, v):
        return _integer_string(v, "max_amount_required")


class EIP3009Authorization(BaseModel):
    from_: str = Field(alias="from")
    to: str
    value: str
    valid_after: str
    valid_before: str
    nonce: str

    model_config = _camel()

    @field_validator("value")
    def validate_value(cls, v):
        return _integer_string(v, "value")


class ExactPaymentPayload(BaseModel):
    signature: str
    authorization: EIP3009Authorization


class PaymentPayload(BaseModel):
    x402_version: int
    scheme: str
    network: str
    payload: ExactPaymentPayload

    model_config = _camel()


class VerifyResponse(BaseModel):
    is_valid: bool = Field(alias="isValid")
    invalid_reason: Optional[str] = Field(None, alias="invalidReason")
    payer: Optional[str]

    model_config = _camel()


class SettleResponse(BaseModel):
    success: bool
    error_reason: Optional[str] = None
    transaction: Optional[str] = None
    network: Optional[str] = None
    payer: Optional[str] = None

    model_config = _camel()


class SupportedKind(BaseModel):
    """One entry of a facilitator's GET /supported answer."""
    x402_version: int
    scheme: str
    network: str
    extra: Optional[dict[str, Any]] = None

    model_config = _camel()


class SupportedResponse(BaseModel):
    """A facilitator's GET /supported answer: the payment kinds it settles."""
    kinds: list[SupportedKind]

    model_config = _camel()
