"""The gateway's facilitator client against x402 1.0.0's, on the wire (#604).

A stub facilitator runs on a loopback port and records every request exactly
as received (request line, headers in order, body bytes). Each scenario sends
the same verify or settle through x402 1.0.0's FacilitatorClient (the oracle,
installed for tests only) and through app.x402.facilitator_client, and
asserts that the two sent identical requests and came back with identical
results: the same parsed answer (dumped by alias), or the same exception type.

supported() has no 1.0.0 counterpart: its request shape and parsing are
tested on their own at the end.
"""
import asyncio
import json
import socket
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from pydantic import ValidationError

import x402.facilitator as lib_facilitator
import x402.types as lib_types
from app.core.config import settings
from app.x402 import facilitator as gateway_facilitator
from app.x402 import models_v1 as own_types
from app.x402.facilitator_client import DEFAULT_TIMEOUT, FacilitatorClient

PAYER = "0x" + "a1" * 20
TX = "0x" + "ab" * 32

PAYLOAD = {
    "x402Version": 1, "scheme": "exact", "network": "base-sepolia",
    "payload": {
        "signature": "0x" + "ab" * 65,
        "authorization": {
            "from": PAYER, "to": "0x" + "22" * 20, "value": "20000",
            "validAfter": "0", "validBefore": "9999999999", "nonce": "0x" + "01" * 32,
            "unknownInAuthorization": "dropped by both",
        },
        "unknownInPayload": "dropped by both",
    },
    "unknownTopLevel": "dropped by both",
}
REQUIREMENTS = {
    "scheme": "exact", "network": "base-sepolia", "maxAmountRequired": "20000",
    "resource": "https://gateway.example/api/v1/stamps/", "description": "Stamp",
    "mimeType": "application/json", "payTo": "0x" + "22" * 20, "maxTimeoutSeconds": 300,
    "asset": "0x" + "33" * 20, "extra": {"name": "USDC", "version": "2"},
}


class Stub:
    """A facilitator on 127.0.0.1. `answers[path]` is (status, body bytes, delay s[, headers])."""

    def __init__(self):
        self.requests = []
        self.answers = {}
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                stub.requests.append({"line": self.requestline, "headers": list(self.headers.items()),
                                      "body": body})
                path = self.path.rsplit("/", 1)[-1]
                status, payload, delay, *extra = stub.answers.get(path, (404, b'{"error":"not found"}', 0))
                if delay:
                    time.sleep(delay)
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    for name, value in (extra[0] if extra else {}).items():
                        self.send_header(name, value)
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            do_POST = _serve
            do_GET = _serve

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/facilitator"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01},
                                       daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stub():
    s = Stub()
    yield s
    s.close()


def _answer(data, status=200, delay=0):
    raw = data if isinstance(data, bytes) else json.dumps(data).encode()
    return (status, raw, delay)


def _outcome(coro_fn):
    try:
        result = asyncio.run(coro_fn())
    except Exception as e:
        return ("raised", type(e))
    return ("result", type(result).__name__, json.dumps(result.model_dump(by_alias=True)))


def run_both(stub, path, config_extra=None, timeout=None, monkeypatch=None):
    """(oracle outcome, own outcome, oracle requests, own requests) for one call."""
    config = {"url": stub.url, **(config_extra or {})}
    if timeout is not None:
        # x402 1.0.0 cannot take a timeout; give its bare AsyncClient() the same one.
        real = httpx.AsyncClient
        monkeypatch.setattr(lib_facilitator, "httpx",
                            types.SimpleNamespace(AsyncClient=lambda: real(timeout=timeout)))
    lib_client = lib_facilitator.FacilitatorClient(config=dict(config))
    lib_payment = lib_types.PaymentPayload.model_validate(PAYLOAD)
    lib_req = lib_types.PaymentRequirements.model_validate(REQUIREMENTS)
    own_kwargs = {} if timeout is None else {"timeout": httpx.Timeout(timeout)}
    own_client = FacilitatorClient(config=dict(config), **own_kwargs)
    own_payment = own_types.PaymentPayload.model_validate(PAYLOAD)
    own_req = own_types.PaymentRequirements.model_validate(REQUIREMENTS)

    stub.requests.clear()
    lib_out = _outcome(lambda: getattr(lib_client, path)(payment=lib_payment, payment_requirements=lib_req))
    lib_requests = list(stub.requests)
    stub.requests.clear()
    own_out = _outcome(lambda: getattr(own_client, path)(payment=own_payment, payment_requirements=own_req))
    own_requests = list(stub.requests)
    return lib_out, own_out, lib_requests, own_requests


VALID = {"isValid": True, "payer": PAYER}
SETTLED = {"success": True, "transaction": TX, "network": "base-sepolia", "payer": PAYER}

SCENARIOS = {
    "verify": {
        "valid": _answer(VALID),
        "invalid": _answer({"isValid": False, "invalidReason": "insufficient_funds", "payer": PAYER}),
        "unknown_fields": _answer({**VALID, "invalidMessage": "x", "extensions": {}}),
        "payer_missing": _answer({"isValid": True}),
        "non_200_valid_body": _answer(VALID, status=500),
        "non_200_error_body": _answer({"error": "internal"}, status=500),
        "401_text": _answer(b"Unauthorized", status=401),
        "malformed_json": _answer(b"{not json"),
        "empty_body": _answer(b""),
        "json_array": _answer([VALID]),
    },
    "settle": {
        "success": _answer(SETTLED),
        "refused": _answer({"success": False, "errorReason": "invalid_transaction_state",
                            "transaction": "", "network": "base-sepolia", "payer": PAYER}),
        "refused_without_transaction": _answer({"success": False, "errorReason": "invalid_signature"}),
        "unknown_fields": _answer({**SETTLED, "errorMessage": "x", "amount": "1", "extensions": {}}),
        "non_200_refusal_body": _answer({"success": False, "errorReason": "x"}, status=400),
        "non_200_error_body": _answer({"error": "internal"}, status=502),
        "503_html": _answer(b"<html>busy</html>", status=503),
        "malformed_json": _answer(b'{"success": tru'),
        "json_string": _answer("ok"),
    },
}

CASES = [(path, name) for path, cases in SCENARIOS.items() for name in cases]


@pytest.mark.parametrize("path,name", CASES, ids=[f"{p}-{n}" for p, n in CASES])
def test_same_request_and_same_result(stub, path, name):
    stub.answers[path] = SCENARIOS[path][name]
    lib_out, own_out, lib_requests, own_requests = run_both(stub, path)
    assert own_out == lib_out
    assert len(own_requests) == len(lib_requests) == 1
    assert own_requests == lib_requests


@pytest.mark.parametrize("path", ["verify", "settle"])
def test_redirect_is_followed_the_same(stub, path):
    stub.answers[path] = (307, b"", 0, {"Location": f"/facilitator/moved-{path}"})
    stub.answers[f"moved-{path}"] = _answer(VALID if path == "verify" else SETTLED)
    lib_out, own_out, lib_requests, own_requests = run_both(stub, path)
    assert own_out == lib_out and own_out[0] == "result"
    assert len(own_requests) == 2
    assert own_requests == lib_requests


def test_classification_is_what_settlement_relies_on(stub):
    """Spot-check the outcomes above, so the differential cannot pass vacuously."""
    stub.answers["settle"] = SCENARIOS["settle"]["refused_without_transaction"]
    _, own_out, _, reqs = run_both(stub, "settle")
    assert own_out[0] == "result" and json.loads(own_out[2])["success"] is False
    stub.answers["settle"] = SCENARIOS["settle"]["non_200_error_body"]
    assert run_both(stub, "settle")[1][0] == "raised"
    stub.answers["verify"] = SCENARIOS["verify"]["malformed_json"]
    assert run_both(stub, "verify")[1] == ("raised", json.JSONDecodeError)
    body = json.loads(reqs[0]["body"])
    assert list(body) == ["x402Version", "paymentPayload", "paymentRequirements"]
    assert "unknownInPayload" not in json.dumps(body)


@pytest.mark.parametrize("path", ["verify", "settle"])
def test_timeout(stub, path, monkeypatch):
    stub.answers[path] = _answer(VALID if path == "verify" else SETTLED, delay=0.6)
    lib_out, own_out, lib_requests, own_requests = run_both(stub, path, timeout=0.1, monkeypatch=monkeypatch)
    assert own_out == lib_out == ("raised", httpx.ReadTimeout)
    assert own_requests == lib_requests


def test_default_timeout_is_what_1_0_0_used():
    """x402 1.0.0 used a bare httpx.AsyncClient(): its default timeout."""
    assert DEFAULT_TIMEOUT == httpx.AsyncClient().timeout == httpx.Timeout(5.0)
    assert FacilitatorClient(config={"url": "https://f.example"}).timeout == DEFAULT_TIMEOUT
    assert FacilitatorClient(config={"url": "https://f.example"})._client().timeout == httpx.Timeout(5.0)


@pytest.mark.parametrize("path", ["verify", "settle"])
def test_connection_refused(path):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    dead = types.SimpleNamespace(url=f"http://127.0.0.1:{port}/facilitator", requests=[])
    lib_out, own_out, _, _ = run_both(dead, path)
    assert own_out == lib_out == ("raised", httpx.ConnectError)


def _bearer_hook():
    async def create_headers():
        auth = {"Authorization": "Bearer static-token"}
        return {"verify": auth, "settle": auth, "list": auth}
    return create_headers


def _cdp_shaped_hook(monkeypatch):
    """The gateway's own CDP hook (facilitator._config) around a cdp-sdk-shaped
    synchronous create_headers with fixed values, so both clients get the same."""
    def create_headers():
        def entry(path):
            return {"Authorization": f"Bearer jwt-for-{path}", "Content-Type": "application/json",
                    "Correlation-Context": "sdk_version=1.51.0,sdk_language=python,source=x402"}
        return {p: entry(p) for p in ("verify", "settle", "supported", "list")}

    fake = types.ModuleType("cdp.x402")
    fake.create_facilitator_config = lambda key_id, secret: {
        "url": "https://api.cdp.coinbase.com/platform/v2/x402", "create_headers": create_headers}
    monkeypatch.setitem(sys.modules, "cdp", types.ModuleType("cdp"))
    monkeypatch.setitem(sys.modules, "cdp.x402", fake)
    monkeypatch.setattr(settings, "X402_FACILITATOR_CDP_API_KEY_ID", "id")
    monkeypatch.setattr(settings, "X402_FACILITATOR_CDP_API_KEY_SECRET", "secret")
    return gateway_facilitator._config()["create_headers"]


@pytest.mark.parametrize("path", ["verify", "settle"])
@pytest.mark.parametrize("hook", ["bearer", "cdp"])
def test_auth_headers_are_sent_the_same(stub, monkeypatch, path, hook):
    create_headers = _bearer_hook() if hook == "bearer" else _cdp_shaped_hook(monkeypatch)
    stub.answers[path] = _answer(VALID if path == "verify" else SETTLED)
    lib_out, own_out, lib_requests, own_requests = run_both(stub, path, {"create_headers": create_headers})
    assert own_out == lib_out
    assert own_requests == lib_requests
    sent = dict((k.lower(), v) for k, v in own_requests[0]["headers"])
    assert sent["authorization"] == ("Bearer static-token" if hook == "bearer" else f"Bearer jwt-for-{path}")


def test_a_synchronous_hook_works_too(stub):
    """cdp-sdk's own hook is synchronous; the client accepts it unwrapped (1.0.0 did not)."""
    stub.answers["verify"] = _answer(VALID)
    client = FacilitatorClient(config={"url": stub.url,
                                       "create_headers": lambda: {"verify": {"Authorization": "Bearer s"}}})
    asyncio.run(client.verify(payment=own_types.PaymentPayload.model_validate(PAYLOAD),
                              payment_requirements=own_types.PaymentRequirements.model_validate(REQUIREMENTS)))
    assert ("Authorization", "Bearer s") in stub.requests[0]["headers"]


@pytest.mark.parametrize("url", ["", "ftp://f.example", "f.example"])
def test_url_check_matches(url):
    with pytest.raises(ValueError) as lib_err:
        lib_facilitator.FacilitatorClient(config={"url": url})
    with pytest.raises(ValueError) as own_err:
        FacilitatorClient(config={"url": url})
    assert str(own_err.value) == str(lib_err.value)


@pytest.mark.parametrize("url", ["https://f.example/x402", "https://f.example/x402/", "https://f.example/x402//"])
def test_trailing_slash_rule_matches(url):
    assert (FacilitatorClient(config={"url": url}).config["url"]
            == lib_facilitator.FacilitatorClient(config={"url": url}).config["url"])


def test_no_config_defaults_like_1_0_0():
    assert FacilitatorClient().config == lib_facilitator.FacilitatorClient().config


# --- supported(): no 1.0.0 counterpart ------------------------------------------

SUPPORTED = {"kinds": [{"x402Version": 1, "scheme": "exact", "network": "base-sepolia"},
                       {"x402Version": 2, "scheme": "exact", "network": "eip155:84532"}],
             "extensions": [], "signers": {"eip155:*": ["0x" + "44" * 20]}}


def test_supported_request_and_parse(stub):
    stub.answers["supported"] = _answer(SUPPORTED)
    got = asyncio.run(FacilitatorClient(config={"url": stub.url + "/"}).supported())
    assert isinstance(got, own_types.SupportedResponse)
    assert [(k.x402_version, k.network) for k in got.kinds] == [(1, "base-sepolia"), (2, "eip155:84532")]
    (req,) = stub.requests
    assert req["line"] == "GET /facilitator/supported HTTP/1.1"
    assert req["body"] == b""
    assert ("Content-Type", "application/json") in req["headers"]


def test_supported_sends_the_cdp_supported_entry(stub, monkeypatch):
    stub.answers["supported"] = _answer(SUPPORTED)
    client = FacilitatorClient(config={"url": stub.url, "create_headers": _cdp_shaped_hook(monkeypatch)})
    asyncio.run(client.supported())
    sent = dict(stub.requests[0]["headers"])
    assert sent["Authorization"] == "Bearer jwt-for-supported"
    assert sent["Correlation-Context"].startswith("sdk_version=")


def test_supported_parses_whatever_the_status(stub):
    client = FacilitatorClient(config={"url": stub.url})
    stub.answers["supported"] = _answer(SUPPORTED, status=500)
    assert len(asyncio.run(client.supported()).kinds) == 2
    for answer, exc in ((_answer({"error": "unauthorized"}, status=401), ValidationError),
                        (_answer(b"not json"), json.JSONDecodeError),
                        (_answer([1]), TypeError)):
        stub.answers["supported"] = answer
        with pytest.raises(exc):
            asyncio.run(client.supported())


def test_supported_timeout(stub):
    stub.answers["supported"] = _answer(SUPPORTED, delay=0.6)
    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(FacilitatorClient(config={"url": stub.url}, timeout=httpx.Timeout(0.1)).supported())
