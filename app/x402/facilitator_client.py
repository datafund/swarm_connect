# app/x402/facilitator_client.py
"""The gateway's own x402 facilitator client (#604, D1 in #596).

Replaces x402 1.0.0's FacilitatorClient, and behaves the same for v1:

- POST {url}/verify and {url}/settle with the JSON body
  {"x402Version", "paymentPayload", "paymentRequirements"}: the payload dumped
  by alias, the requirements by alias without None fields, serialized by
  json= exactly as 1.0.0's httpx did.
- Headers: Content-Type: application/json, then the entry for the path
  ("verify", "settle", "supported") of create_headers(), when configured.
  create_headers may be async (the gateway's own hooks) or sync (cdp-sdk's).
- A new httpx2.AsyncClient per call, following redirects, with the proxy
  environment honoured (trust_env, the default). httpx2 like the rest of app/
  (#595, #626), not encode's httpx that 1.0.0 used: the requests are the same
  except for the User-Agent, python-httpx2/<version> instead of
  python-httpx/<version>.
- Timeout: 5 seconds, set explicitly. 1.0.0 built a bare httpx.AsyncClient(),
  whose default is Timeout(5.0); httpx2's default is the same.
- The answer is parsed whatever its status code: Model(**response.json()).
  So a 200 or non-200 body that parses is a result, success:false included,
  and anything else raises as it did before: a pydantic ValidationError (a
  body without the required fields), a JSONDecodeError (not JSON), a
  TypeError (JSON that is not an object), or the error of a timeout or a
  failed connection (now httpx2's classes, not httpx's). Every caller catches
  these as Exception: app/x402/settlement.py and the middleware count a raise
  as an unknown outcome, and a parsed success:false as a definite refusal,
  except a reason meaning the transaction may have been sent
  (settlement.settle_outcome_unknown, #660), which is unknown too.

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

import httpx2
from typing_extensions import TypedDict

from app.x402.models_v1 import PaymentPayload, PaymentRequirements, SettleResponse, SupportedResponse, VerifyResponse
from app.x402.models_v2 import PaymentPayloadV2, PaymentRequirementsV2, SettleResponseV2, VerifyResponseV2

# What x402 1.0.0 got from a bare httpx.AsyncClient(); httpx2's default too.
DEFAULT_TIMEOUT = httpx2.Timeout(5.0)


class FacilitatorConfig(TypedDict, total=False):
    """url: the facilitator's base URL. create_headers: returns a dict keyed by
    path ("verify", "settle", "supported", ...) of extra headers."""

    url: str
    create_headers: Callable[[], Any]


class FacilitatorClient:
    def __init__(self, config: Optional[FacilitatorConfig] = None, *,
                 timeout: httpx2.Timeout = DEFAULT_TIMEOUT,
                 transport: Optional[httpx2.AsyncBaseTransport] = None):
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

    def _client(self) -> httpx2.AsyncClient:
        if self._transport is not None:
            return httpx2.AsyncClient(timeout=self.timeout, transport=self._transport)
        return httpx2.AsyncClient(timeout=self.timeout)

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
                       payment_requirements: PaymentRequirementsV2) -> httpx2.Response:
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

    async def supported_response(self) -> httpx2.Response:
        """GET {url}/supported for the v2 startup check, unparsed (#608).

        Same headers ("supported" entry) and per-read timeout as verify and
        settle, plus Accept-Encoding: identity, and no redirects followed. A
        non-2xx answer (a 3xx included) is returned with its status and an
        empty body, never read. A 2xx answer is never inflated: one that
        still carries a Content-Encoding other than identity raises
        SupportedAnswerEncoded, so a small compressed answer cannot make
        startup allocate a large one. The raw body is read up to
        MAX_SUPPORTED_BYTES; a longer one raises SupportedAnswerTooLarge.
        The returned Response holds the raw bytes and the status; the caller
        (facilitator.check_facilitator_v2_support) decodes them itself, and
        puts an overall deadline around this call."""
        headers = await self._headers("supported")
        headers["Accept-Encoding"] = "identity"
        async with self._client() as client:
            # Redirects are not followed (#608): httpx2 reads and decodes every
            # 3xx body in full while following, before the encoding check and
            # the byte cap below could see it, and a redirect could hand the
            # check to another origin. A 3xx is returned as is; the caller
            # refuses it like any other non-2xx answer.
            async with client.stream("GET", f"{self.config['url']}/supported", headers=headers,
                                     follow_redirects=False) as response:
                # The raw byte pairs: decoded as latin-1 and re-encoded, an
                # obs-text header (0x80-0xFF) would raise UnicodeEncodeError.
                kept = [(k, v) for k, v in response.headers.raw
                        if k.lower() not in (b"content-encoding", b"content-length", b"transfer-encoding")]
                if not 200 <= response.status_code < 300:
                    # Refused whatever it says (a 3xx included): its body is
                    # never read, so it cannot cost memory or time.
                    return httpx2.Response(response.status_code, headers=kept, content=b"",
                                           request=response.request)
                encodings = [e.strip().lower() for v in response.headers.get_list("content-encoding")
                             for e in v.split(",") if e.strip()]
                if any(e != "identity" for e in encodings):
                    raise SupportedAnswerEncoded(response.status_code)
                body = bytearray()
                async for chunk in response.aiter_raw():
                    body += chunk
                    if len(body) > MAX_SUPPORTED_BYTES:
                        raise SupportedAnswerTooLarge(response.status_code)
                return httpx2.Response(response.status_code, headers=kept, content=bytes(body),
                                      request=response.request)

    async def supported(self) -> SupportedResponse:
        """GET {url}/supported: the payment kinds the facilitator settles.

        Same headers, timeout and parsing rule as v1 verify and settle: the
        body is parsed whatever the HTTP status. The startup check does not
        use this; it reads supported_response() and requires a 2xx."""
        headers = await self._headers("supported")
        async with self._client() as client:
            response = await client.get(f"{self.config['url']}/supported", headers=headers,
                                        follow_redirects=True)
            data = response.json()
        return SupportedResponse(**data)


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


class SupportedAnswerEncoded(Exception):
    """A /supported answer with a Content-Encoding other than identity,
    refused without being inflated. Never carries the body."""

    def __init__(self, status: int):
        super().__init__(f"facilitator /supported answer is content-encoded (HTTP {status})")
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
