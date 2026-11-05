# app/x402/facilitator_client.py
"""The gateway's own x402 facilitator client (#604, D1 in #596).

Replaces x402 1.0.0's FacilitatorClient, and behaves the same for v1:

- POST {url}/verify and {url}/settle with the JSON body
  {"x402Version", "paymentPayload", "paymentRequirements"}: the payload dumped
  by alias, the requirements by alias without None fields, serialized by
  httpx's json= exactly as 1.0.0 did.
- Headers: Content-Type: application/json, then the entry for the path
  ("verify", "settle", "supported") of create_headers(), when configured.
  create_headers may be async (the gateway's own hooks) or sync (cdp-sdk's).
- A new httpx.AsyncClient per call, following redirects, with the proxy
  environment honoured (httpx's default).
- Timeout: 5 seconds, set explicitly. 1.0.0 built a bare httpx.AsyncClient(),
  whose default is httpx.Timeout(5.0).
- The answer is parsed whatever its status code: Model(**response.json()).
  So a 200 or non-200 body that parses is a result, success:false included,
  and anything else raises as it did before: a pydantic ValidationError (a
  body without the required fields), a JSONDecodeError (not JSON), a
  TypeError (JSON that is not an object), or the httpx error of a timeout or
  a failed connection. app/x402/settlement.py counts a raise as an unknown
  outcome and a parsed success:false as a definite refusal.

x402 v2 (#607): verify and settle take a PaymentPayloadV2 with
PaymentRequirementsV2 (mixing the versions raises TypeError). The body is
{"x402Version": 2, "paymentPayload", "paymentRequirements"}, both dumped with
by_alias and exclude_none, so fields the models do not know are never sent.
The payload's `extensions` are left out: the gateway offers none, and they are
facilitator-side switches it never priced. Answers are classified by status
(ADR open question 3, decided for v2 only; v1 keeps the rule above):
- verify: a 2xx body that parses as VerifyResponseV2 is the answer. A 4xx
  body that parses with isValid:false is a refusal. Everything else raises
  (the dependency answers 502): a 5xx, a non-2xx isValid:true, a body that
  does not parse. A 5xx saying isValid:true is never taken as verified.
- settle: a 2xx body that parses is the answer. A 4xx body that parses with
  success:false is a definite refusal. Everything else raises, so the
  settlement code counts it as an unknown outcome: a 5xx (a facilitator or
  proxy in trouble may still have submitted the transfer), a non-2xx
  success:true, or a body that does not parse.

tests/test_x402_facilitator_differential.py runs verify and settle through
this client and through x402 1.0.0 against a stub server and compares the
requests and results. supported() and supported_response() have no 1.0.0
counterpart; the startup check for x402 v2 calls supported_response()
(facilitator.check_facilitator_v2_support, #608) and requires a 2xx.
"""
import inspect
from typing import Any, Callable, Optional

import httpx
from typing_extensions import TypedDict

from app.x402.models_v1 import PaymentPayload, PaymentRequirements, SettleResponse, SupportedResponse, VerifyResponse
from app.x402.models_v2 import PaymentPayloadV2, PaymentRequirementsV2, SettleResponseV2, VerifyResponseV2

# What x402 1.0.0 got from httpx.AsyncClient() without arguments.
DEFAULT_TIMEOUT = httpx.Timeout(5.0)


class FacilitatorConfig(TypedDict, total=False):
    """url: the facilitator's base URL. create_headers: returns a dict keyed by
    path ("verify", "settle", "supported", ...) of extra headers."""

    url: str
    create_headers: Callable[[], Any]


class FacilitatorClient:
    def __init__(self, config: Optional[FacilitatorConfig] = None, *,
                 timeout: httpx.Timeout = DEFAULT_TIMEOUT,
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        if config is None:
            config = {"url": "https://x402.org/facilitator"}
        url = config.get("url", "")
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"Invalid URL {url}, must start with http:// or https://")
        if url.endswith("/"):
            url = url[:-1]
        self.config = {"url": url, "create_headers": config.get("create_headers")}
        self.timeout = timeout
        self._transport = transport     # tests only

    async def _headers(self, path: str) -> dict:
        headers = {"Content-Type": "application/json"}
        create_headers = self.config.get("create_headers")
        if create_headers:
            custom = create_headers()
            if inspect.isawaitable(custom):
                custom = await custom
            headers.update(custom.get(path, {}))
        return headers

    def _client(self) -> httpx.AsyncClient:
        if self._transport is not None:
            return httpx.AsyncClient(timeout=self.timeout, transport=self._transport)
        return httpx.AsyncClient(timeout=self.timeout)

    async def _post(self, path: str, payment: PaymentPayload, payment_requirements: PaymentRequirements):
        headers = await self._headers(path)
        async with self._client() as client:
            response = await client.post(
                f"{self.config['url']}/{path}",
                json={
                    "x402Version": payment.x402_version,
                    "paymentPayload": payment.model_dump(by_alias=True),
                    "paymentRequirements": payment_requirements.model_dump(by_alias=True, exclude_none=True),
                },
                headers=headers,
                follow_redirects=True,
            )
            return response.json()

    async def _post_v2(self, path: str, payment: PaymentPayloadV2,
                       payment_requirements: PaymentRequirementsV2) -> httpx.Response:
        if not isinstance(payment_requirements, PaymentRequirementsV2):
            raise TypeError("an x402 v2 payment needs PaymentRequirementsV2")
        headers = await self._headers(path)
        async with self._client() as client:
            return await client.post(
                f"{self.config['url']}/{path}",
                json=v2_request_body(payment, payment_requirements),
                headers=headers,
                follow_redirects=True,
            )

    @staticmethod
    def _check_v1(payment, payment_requirements) -> None:
        if isinstance(payment_requirements, PaymentRequirementsV2):
            raise TypeError("an x402 v1 payment needs v1 PaymentRequirements")

    async def verify(self, payment, payment_requirements):
        """Ask the facilitator whether the signed payment is valid for the requirements.

        A PaymentPayload (v1) returns VerifyResponse; a PaymentPayloadV2
        returns VerifyResponseV2."""
        if isinstance(payment, PaymentPayloadV2):
            response = await self._post_v2("verify", payment, payment_requirements)
            return classify_v2_verify(response.status_code, response.json())
        self._check_v1(payment, payment_requirements)
        data = await self._post("verify", payment, payment_requirements)
        return VerifyResponse(**data)

    async def settle(self, payment, payment_requirements):
        """Ask the facilitator to submit the payment on-chain.

        A PaymentPayload (v1) returns SettleResponse; a PaymentPayloadV2
        returns SettleResponseV2, classified as the module docstring says."""
        if isinstance(payment, PaymentPayloadV2):
            response = await self._post_v2("settle", payment, payment_requirements)
            return classify_v2_settle(response.status_code, response.json())
        self._check_v1(payment, payment_requirements)
        data = await self._post("settle", payment, payment_requirements)
        return SettleResponse(**data)

    async def supported_response(self) -> httpx.Response:
        """GET {url}/supported, unparsed: the HTTP answer with its status.

        Same headers ("supported" entry) and timeout as verify and settle. The
        v2 startup check (facilitator.check_facilitator_v2_support, #608) uses
        this, so it can refuse a non-2xx answer before reading any kinds.

        The body is read up to MAX_SUPPORTED_BYTES (after any content
        decoding); a longer one raises SupportedAnswerTooLarge, so a
        facilitator cannot make startup read an unbounded answer."""
        headers = await self._headers("supported")
        async with self._client() as client:
            async with client.stream("GET", f"{self.config['url']}/supported", headers=headers,
                                     follow_redirects=True) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_SUPPORTED_BYTES:
                        raise SupportedAnswerTooLarge(response.status_code)
                # The decoded body, without the headers that described the encoded one.
                kept = [(k, v) for k, v in response.headers.multi_items()
                        if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")]
                return httpx.Response(response.status_code, headers=kept, content=bytes(body),
                                      request=response.request)

    async def supported(self) -> SupportedResponse:
        """GET {url}/supported: the payment kinds the facilitator settles.

        Same headers, timeout and parsing rule as v1 verify and settle: the
        body is parsed whatever the HTTP status. The startup check does not
        rely on this; it reads supported_response() and requires a 2xx."""
        response = await self.supported_response()
        return SupportedResponse(**response.json())


def v2_request_body(payment: PaymentPayloadV2, payment_requirements: PaymentRequirementsV2) -> dict:
    """The /verify and /settle JSON body for an x402 v2 payment (#607)."""
    return {
        "x402Version": 2,
        "paymentPayload": payment.model_dump(mode="json", by_alias=True, exclude_none=True,
                                             exclude={"extensions"}),
        "paymentRequirements": payment_requirements.model_dump(mode="json", by_alias=True,
                                                               exclude_none=True),
    }


# The largest GET /supported answer read (#608). Real answers are a few KB.
MAX_SUPPORTED_BYTES = 1024 * 1024


class SupportedAnswerTooLarge(Exception):
    """A /supported answer over MAX_SUPPORTED_BYTES. Never carries the body."""

    def __init__(self, status: int):
        super().__init__(f"facilitator /supported answer over {MAX_SUPPORTED_BYTES} bytes (HTTP {status})")
        self.status = status


class FacilitatorAnswerUnusable(Exception):
    """A verify or settle answer that is neither a result nor a definite
    refusal (for settle: the outcome is unknown). Never carries the body."""


def classify_v2_verify(status: int, data: Any) -> VerifyResponseV2:
    """The v2 verify answer, or raise FacilitatorAnswerUnusable (the
    dependency answers 502, nothing charged)."""
    result = VerifyResponseV2.model_validate(data)
    if 200 <= status < 300:
        return result
    if 400 <= status < 500 and result.is_valid is False:
        return result
    raise FacilitatorAnswerUnusable(f"facilitator answered {status} to verify")


def classify_v2_settle(status: int, data: Any) -> SettleResponseV2:
    """The v2 settle answer, or raise FacilitatorAnswerUnusable (unknown outcome)."""
    result = SettleResponseV2.model_validate(data)    # ValidationError: unknown outcome too
    if 200 <= status < 300:
        return result
    if 400 <= status < 500 and result.success is False:
        return result
    raise FacilitatorAnswerUnusable(f"facilitator answered {status} to settle")
