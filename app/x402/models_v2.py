# app/x402/models_v2.py
"""The x402 v2 wire types (#606, part of #596).

Shapes and limits follow the @x402/core 2.30.0 zod schemas
(PaymentRequiredV2Schema, PaymentPayloadV2Schema, PaymentRequirementsV2Schema,
ResourceInfoSchema) and its VerifyResponse / SettleResponse types; Python
x402 2.27.0 agrees. docs/adr/x402-v2.md (Question 6) lists them.

Nothing in a request path uses these yet: #607 does.

Decisions that differ from, or pin down, the reference:

- Unknown fields are DROPPED at every level (envelope, `accepted`,
  `resource`, the `exact` payload and its authorization). The gateway
  re-serialises a client's payload and sends it to the facilitator under its
  own credentials; forwarding fields it never validated would let a client
  pass facilitator-side switches the gateway did not price or check. The
  reference parser does the same (zod `z.object()` strips unknown keys), and
  so did v1 (x402 1.0.0). Python x402 2.x forwards them; we do not. The
  free-form records `extra`, `extensions` and `extensionResponses` keep their
  contents: the spec defines them as open objects.
- Amounts are decimal integer strings without sign (`^[0-9]+$`): `amount`,
  and the EIP-3009 `value`, `validAfter`, `validBefore`. The reference only
  asks for a non-empty string, but the `exact` scheme moves uint256 values.
- `isValid` (VerifyResponseV2) and `success` (SettleResponseV2) are strict
  booleans: pydantic's lax mode would read "yes", "1" or 1 as true, and these
  fields decide whether a payment counts as made. (v1 keeps lax booleans, as
  x402 1.0.0 had them.)
- `maxTimeoutSeconds` is a positive integer (the reference allows any positive
  number). A client copies the gateway's offer, so a legitimate one never
  sends a fraction.
- `extra` is absent or null, never defaulted to `{}` (Python 2.27 dumps `{}`;
  TypeScript leaves it out). Dump with `by_alias=True, exclude_none=True`.
- When `accepted.scheme` is "exact", `payload` is parsed into
  ExactEvmPayloadV2 (so unknown keys are dropped and a malformed exact payload
  is a validation error). Any other scheme keeps `payload` as a plain object,
  so #607 can answer "scheme not accepted" rather than "malformed"; it never
  forwards such a payload.
"""
import re
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel

# CAIP-2, exactly as @x402/core 2.30.0 NetworkSchemaV2. \Z, not $: Python's $
# also matches before a trailing newline.
CAIP2_RE = re.compile(r"^[-a-z0-9]{3,8}:[-_a-zA-Z0-9]{1,32}\Z")
PRINTABLE_ASCII_RE = re.compile(r"^[\x20-\x7e]+\Z")
UINT_STRING_RE = re.compile(r"^[0-9]+\Z")

MAX_PAYMENT_ACCEPTS = 1024  # @x402/core maxPaymentAccepts
MAX_SERVICE_NAME = 32
MAX_TAGS = 5
MAX_TAG = 32
MAX_ICON_URL = 2048


def _config() -> ConfigDict:
    return ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


def _non_empty(v: str) -> str:
    if not v:
        raise ValueError("must be a non-empty string")
    return v


def _uint_string(v: str) -> str:
    if not UINT_STRING_RE.match(v):
        raise ValueError("must be a non-negative integer encoded as a decimal string")
    return v


def _printable(v: str, limit: int) -> str:
    if not v or len(v) > limit or not PRINTABLE_ASCII_RE.match(v):
        raise ValueError(f"must be 1 to {limit} printable ASCII characters")
    return v


class ResourceInfo(BaseModel):
    """The resource a payment is for. A null optional field counts as absent."""

    url: str
    description: Optional[str] = None
    mime_type: Optional[str] = None
    service_name: Optional[str] = None
    tags: Optional[List[str]] = None
    icon_url: Optional[str] = None

    model_config = _config()

    @field_validator("url")
    @classmethod
    def _url(cls, v):
        return _non_empty(v)

    @field_validator("service_name")
    @classmethod
    def _service_name(cls, v):
        return None if v is None else _printable(v, MAX_SERVICE_NAME)

    @field_validator("tags")
    @classmethod
    def _tags(cls, v):
        if v is None:
            return None
        if len(v) > MAX_TAGS:
            raise ValueError(f"at most {MAX_TAGS} tags")
        return [_printable(t, MAX_TAG) for t in v]

    @field_validator("icon_url")
    @classmethod
    def _icon_url(cls, v):
        if v is not None and len(v) > MAX_ICON_URL:
            raise ValueError(f"at most {MAX_ICON_URL} characters")
        return v


class PaymentRequirementsV2(BaseModel):
    """One offer in `accepts`, and the `accepted` copy in a payment."""

    scheme: str
    network: str
    asset: str
    amount: str
    pay_to: str
    max_timeout_seconds: StrictInt
    extra: Optional[Dict[str, Any]] = None

    model_config = _config()

    @field_validator("scheme", "asset", "pay_to")
    @classmethod
    def _required_text(cls, v):
        return _non_empty(v)

    @field_validator("network")
    @classmethod
    def _network(cls, v):
        if not CAIP2_RE.match(v):
            raise ValueError("must be a CAIP-2 network id, for example eip155:84532")
        return v

    @field_validator("amount")
    @classmethod
    def _amount(cls, v):
        return _uint_string(v)

    @field_validator("max_timeout_seconds")
    @classmethod
    def _timeout(cls, v):
        if v <= 0:
            raise ValueError("must be a positive integer")
        return v


class PaymentRequired(BaseModel):
    """The x402 v2 Payment Required message, sent in the PAYMENT-REQUIRED header."""

    x402_version: Literal[2]
    error: Optional[str] = None
    resource: ResourceInfo
    accepts: List[PaymentRequirementsV2] = Field(min_length=1, max_length=MAX_PAYMENT_ACCEPTS)
    extensions: Optional[Dict[str, Any]] = None

    model_config = _config()


class EIP3009Authorization(BaseModel):
    """The EIP-3009 transferWithAuthorization message the payer signed."""

    from_: str = Field(alias="from")
    to: str
    value: str
    valid_after: str
    valid_before: str
    nonce: str

    model_config = _config()

    @field_validator("from_", "to", "nonce")
    @classmethod
    def _required_text(cls, v):
        return _non_empty(v)

    @field_validator("value", "valid_after", "valid_before")
    @classmethod
    def _uint(cls, v):
        return _uint_string(v)


class ExactEvmPayloadV2(BaseModel):
    """The `payload` of an `exact` EVM payment signed with EIP-3009."""

    signature: str
    authorization: EIP3009Authorization

    model_config = _config()

    @field_validator("signature")
    @classmethod
    def _signature(cls, v):
        return _non_empty(v)


class PaymentPayloadV2(BaseModel):
    """The x402 v2 payment, sent in the PAYMENT-SIGNATURE header."""

    x402_version: Literal[2]
    resource: Optional[ResourceInfo] = None
    accepted: PaymentRequirementsV2
    # left_to_right: a plain object stays a dict; only the validator below
    # turns an "exact" payload into ExactEvmPayloadV2.
    payload: Union[Dict[str, Any], ExactEvmPayloadV2] = Field(union_mode="left_to_right")
    extensions: Optional[Dict[str, Any]] = None

    model_config = _config()

    @model_validator(mode="before")
    @classmethod
    def _typed_exact_payload(cls, data):
        # Pick the payload type from the scheme, not by trying a union: an
        # "exact" payload must parse as ExactEvmPayloadV2 or the message is
        # invalid; anything else stays a plain object.
        if isinstance(data, dict):
            accepted = data.get("accepted")
            scheme = accepted.get("scheme") if isinstance(accepted, dict) else getattr(accepted, "scheme", None)
            payload = data.get("payload")
            if scheme == "exact" and isinstance(payload, dict):
                data = dict(data)
                try:
                    data["payload"] = ExactEvmPayloadV2.model_validate(payload)
                except ValidationError as exc:
                    # Report the errors under "payload", as if the field had
                    # been validated in place.
                    raise ValidationError.from_exception_data(
                        cls.__name__,
                        [
                            {
                                "type": e["type"],
                                "loc": ("payload",) + tuple(e["loc"]),
                                "input": e["input"],
                                **({"ctx": e["ctx"]} if "ctx" in e else {}),
                            }
                            for e in exc.errors()
                        ],
                    ) from None
        return data

    @property
    def exact_payload(self) -> Optional[ExactEvmPayloadV2]:
        """The typed `exact` payload, or None for another scheme."""
        return self.payload if isinstance(self.payload, ExactEvmPayloadV2) else None


class VerifyResponseV2(BaseModel):
    """A facilitator's answer to POST /verify."""

    is_valid: StrictBool  # JSON true/false only; "yes", "1" and 1 are refused
    invalid_reason: Optional[str] = None
    invalid_message: Optional[str] = None
    payer: Optional[str] = None
    extensions: Optional[Dict[str, Any]] = None
    extension_responses: Optional[Dict[str, Any]] = None
    extra: Optional[Dict[str, Any]] = None

    model_config = _config()


class SettleResponseV2(BaseModel):
    """A facilitator's answer to POST /settle, and the PAYMENT-RESPONSE header.

    `transaction` is "" on a refusal.
    """

    success: StrictBool  # JSON true/false only
    transaction: str
    network: str
    error_reason: Optional[str] = None
    error_message: Optional[str] = None
    payer: Optional[str] = None
    amount: Optional[str] = None
    extensions: Optional[Dict[str, Any]] = None
    extension_responses: Optional[Dict[str, Any]] = None
    extra: Optional[Dict[str, Any]] = None

    model_config = _config()
