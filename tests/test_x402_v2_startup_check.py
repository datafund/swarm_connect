"""The startup /supported check for x402 v2 (#608, D3 in #596).

With x402 enabled and 2 in X402_PROTOCOL_VERSIONS, the gateway starts only if
the facilitator's GET /supported lists x402Version 2, scheme exact, on the
configured network as its CAIP-2 id. Anything else, an unreachable facilitator
or an unreadable answer included, refuses to start (fail closed), with the
same prefix as validate_x402_config's refusals. The facilitator is a mock
transport here; nothing is sent anywhere.
"""
import asyncio
import json

import httpx
import pytest

# Imported with the default settings, not under the fixture's x402 settings:
# app.main reads them at import time.
from app import main
from app.core.config import settings
from app.x402 import check_config, facilitator
from app.x402.facilitator_client import FacilitatorClient

PAY_TO = "0x" + "ab" * 20
URL = "https://facilitator.example/x402"
PREFIX = "Refusing to start with x402 enabled: X402_PROTOCOL_VERSIONS includes 2, but "

V1_EXACT = {"x402Version": 1, "scheme": "exact", "network": "base-sepolia"}
V2_EXACT = {"x402Version": 2, "scheme": "exact", "network": "eip155:84532"}


@pytest.fixture
def x402(monkeypatch):
    monkeypatch.setattr(settings, "X402_ENABLED", True)
    monkeypatch.setattr(settings, "X402_NETWORK", "base-sepolia")
    monkeypatch.setattr(settings, "X402_PAY_TO_ADDRESS", PAY_TO)
    monkeypatch.setattr(settings, "X402_FACILITATOR_URL", URL)
    monkeypatch.setattr(settings, "X402_FACILITATOR_CDP_API_KEY_ID", None)
    monkeypatch.setattr(settings, "X402_FACILITATOR_CDP_API_KEY_SECRET", None)
    monkeypatch.setattr(settings, "X402_FACILITATOR_BEARER_TOKEN", None)
    monkeypatch.setattr(settings, "X402_PROTOCOL_VERSIONS", "1,2")
    facilitator.reset_facilitator_client()
    yield monkeypatch
    facilitator.reset_facilitator_client()


class _Raw(httpx.AsyncByteStream):
    def __init__(self, data: bytes):
        self.data = data

    async def __aiter__(self):
        yield self.data


class Supported:
    """A /supported that answers `answer` (a response, or an exception to raise)."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def handler(self, request):
        self.calls.append((request.method, request.url.path))
        if isinstance(self.answer, Exception):
            raise self.answer
        # A fresh, unread answer each time, as a real transport gives: the
        # startup check reads the raw stream (aiter_raw).
        a = self.answer
        if isinstance(a, httpx.Response) and not isinstance(a.stream, _Raw):
            return httpx.Response(a.status_code, headers=a.headers, stream=_Raw(a.content))
        return a

    def client(self):
        return FacilitatorClient({"url": URL}, transport=httpx.MockTransport(self.handler))


def kinds(*k):
    return httpx.Response(200, json={"kinds": list(k)})


def check(sup):
    asyncio.run(facilitator.check_facilitator_v2_support(sup.client()))


def refused(sup) -> str:
    with pytest.raises(RuntimeError) as exc:
        check(sup)
    msg = str(exc.value)
    assert msg.startswith(PREFIX), msg
    assert msg.endswith("set X402_PROTOCOL_VERSIONS=1")
    assert URL not in msg               # the host only, never the configured URL
    return msg


# --- v2 listed --------------------------------------------------------------

@pytest.mark.parametrize("network", ["base-sepolia", "eip155:84532"])
def test_v2_exact_on_the_network_starts(x402, network):
    x402.setattr(settings, "X402_NETWORK", network)
    sup = Supported(kinds(V1_EXACT, V2_EXACT))
    check(sup)
    assert sup.calls == [("GET", "/x402/supported")]


def test_v2_only_setting_is_checked_too(x402):
    x402.setattr(settings, "X402_PROTOCOL_VERSIONS", "2")
    check(Supported(kinds(V2_EXACT)))
    with pytest.raises(RuntimeError, match="does not list"):
        check(Supported(kinds(V1_EXACT)))


def test_mainnet_needs_v2_on_mainnet(x402):
    x402.setattr(settings, "X402_NETWORK", "base")
    check(Supported(kinds({"x402Version": 2, "scheme": "exact", "network": "eip155:8453"})))
    msg = refused(Supported(kinds(V2_EXACT)))
    assert "network eip155:8453" in msg and "[eip155:84532]" in msg


def test_extra_fields_in_the_answer_are_ignored(x402):
    body = {"kinds": [{**V2_EXACT, "extra": {"feePayer": "0x" + "00" * 20}}],
            "extensions": [], "signers": {"eip155:*": []}}
    check(Supported(httpx.Response(200, json=body)))


# --- not listed -------------------------------------------------------------

def test_v2_missing_refuses(x402):
    msg = refused(Supported(kinds(V1_EXACT)))
    assert "does not list x402Version 2, scheme exact, network eip155:84532" in msg
    assert "v2 exact for no network" in msg
    assert "v1 exact" in msg                 # what it does list for the network


def test_v2_on_the_wrong_network_refuses(x402):
    msg = refused(Supported(kinds({"x402Version": 2, "scheme": "exact", "network": "eip155:8453"})))
    assert "[eip155:8453]" in msg


def test_v2_under_the_v1_network_name_refuses(x402):
    # CAIP-2 form only: a v2 kind with "base-sepolia" is not v2 on eip155:84532.
    msg = refused(Supported(kinds({"x402Version": 2, "scheme": "exact", "network": "base-sepolia"})))
    assert "v2 exact" in msg


def test_v2_with_another_scheme_only_refuses(x402):
    refused(Supported(kinds({"x402Version": 2, "scheme": "upto", "network": "eip155:84532"})))


def test_an_empty_list_refuses(x402):
    refused(Supported(kinds()))


# --- unreachable ------------------------------------------------------------

@pytest.mark.parametrize("error", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"),
                                   httpx.ConnectTimeout("slow")])
def test_unreachable_refuses(x402, error):
    msg = refused(Supported(error))
    assert "could not be reached" in msg
    assert type(error).__name__ in msg
    assert "facilitator.example" in msg


# --- malformed --------------------------------------------------------------

@pytest.mark.parametrize("answer", [
    httpx.Response(200, text="<html>not json</html>"),
    httpx.Response(200, json=[V2_EXACT]),
    httpx.Response(200, json={"items": [V2_EXACT]}),
    httpx.Response(200, json={"kinds": [{"scheme": "exact"}]}),
    httpx.Response(401, json={"errorType": "unauthorized", "errorMessage": "missing token"}),
])
def test_malformed_answer_refuses(x402, answer):
    msg = refused(Supported(answer))
    assert "not a list of payment kinds" in msg


# --- when it does not run ---------------------------------------------------

@pytest.mark.parametrize("enabled,versions", [(True, "1"), (False, "1,2"), (False, "2")])
def test_not_called_unless_x402_and_v2_are_enabled(x402, enabled, versions):
    x402.setattr(settings, "X402_ENABLED", enabled)
    x402.setattr(settings, "X402_PROTOCOL_VERSIONS", versions)
    sup = Supported(httpx.ConnectError("must not be called"))
    check(sup)
    assert sup.calls == []


def test_uses_the_shared_authenticated_client(x402):
    x402.setattr(settings, "X402_FACILITATOR_BEARER_TOKEN", "tok")
    sup = Supported(kinds(V2_EXACT))
    seen = []

    def handler(request):
        seen.append(request.headers.get("Authorization"))
        return sup.handler(request)

    real = facilitator.get_facilitator_client()
    shared = FacilitatorClient(real.config, transport=httpx.MockTransport(handler))
    x402.setattr(facilitator, "_client", shared)
    asyncio.run(facilitator.check_facilitator_v2_support())
    assert seen == ["Bearer tok"]


# --- startup and the deploy check -------------------------------------------

def test_startup_refuses_before_anything_else_starts(x402):
    started = []

    async def refuse():
        raise RuntimeError(PREFIX + "test")

    async def init_client():
        started.append("http client")

    x402.setattr(facilitator, "validate_x402_config", lambda: started.append("validated"))
    x402.setattr(facilitator, "check_facilitator_v2_support", refuse)
    x402.setattr("app.services.http_client.init_client", init_client)

    async def run():
        async with main.lifespan(main.app):
            pass

    with pytest.raises(RuntimeError, match="includes 2"):
        asyncio.run(run())
    assert started == ["validated"]


def test_deploy_check_runs_the_v2_check(x402, capsys):
    x402.setattr(facilitator, "_client", Supported(kinds(V1_EXACT)).client())
    assert check_config.main() == check_config.EXIT_REFUSED
    assert "does not list x402Version 2" in capsys.readouterr().err
    x402.setattr(facilitator, "_client", Supported(kinds(V2_EXACT)).client())
    assert check_config.main() == check_config.EXIT_OK


def test_deploy_check_refuses_an_unreachable_facilitator(x402, capsys):
    x402.setattr(facilitator, "_client", Supported(httpx.ConnectError("down")).client())
    assert check_config.main() == check_config.EXIT_REFUSED
    assert "could not be reached" in capsys.readouterr().err


def test_deploy_check_does_not_call_supported_with_v2_off(x402, capsys):
    x402.setattr(settings, "X402_PROTOCOL_VERSIONS", "1")
    sup = Supported(httpx.ConnectError("must not be called"))
    x402.setattr(facilitator, "_client", sup.client())
    assert check_config.main() == check_config.EXIT_OK
    assert sup.calls == []


# --- review round 1: the HTTP status, and errors before any answer ----------

@pytest.mark.parametrize("status", [500, 401, 503, 302, 404])
def test_a_non_2xx_answer_is_refused_whatever_its_body(x402, status):
    # A valid kinds list in the body is not trusted on a non-2xx answer.
    msg = refused(Supported(httpx.Response(status, json={"kinds": [V2_EXACT]})))
    assert f"answered HTTP {status}" in msg


def test_cdp_text_401_names_the_status(x402):
    msg = refused(Supported(httpx.Response(401, text="Unauthorized")))
    assert "answered HTTP 401" in msg and "authentication" in msg


def test_a_2xx_answer_that_is_not_kinds_names_the_status(x402):
    msg = refused(Supported(httpx.Response(200, text="<html>")))
    assert "answered HTTP 200 with something that is not a list of payment kinds" in msg


def test_a_2xx_other_than_200_with_v2_is_accepted(x402):
    check(Supported(httpx.Response(203, json={"kinds": [V2_EXACT]})))


@pytest.mark.parametrize("status", [500, 401, 503])
def test_deploy_check_refuses_a_non_2xx_answer(x402, capsys, status):
    x402.setattr(facilitator, "_client",
                 Supported(httpx.Response(status, json={"kinds": [V2_EXACT]})).client())
    assert check_config.main() == check_config.EXIT_REFUSED
    assert f"answered HTTP {status}" in capsys.readouterr().err


def _hook_raises():
    raise RuntimeError("cdp-sdk could not sign: secret=do-not-print")


def test_a_raising_credential_hook_is_a_refusal(x402, capsys):
    sup = Supported(kinds(V2_EXACT))
    client = FacilitatorClient({"url": URL, "create_headers": _hook_raises},
                               transport=httpx.MockTransport(sup.handler))
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(facilitator.check_facilitator_v2_support(client))
    msg = str(exc.value)
    assert msg.startswith(PREFIX) and "could not be made (RuntimeError" in msg
    assert "do-not-print" not in msg and exc.value.__cause__ is None
    assert sup.calls == []
    x402.setattr(facilitator, "_client", client)
    assert check_config.main() == check_config.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "could not be made" in err and "do-not-print" not in err and "Traceback" not in err


@pytest.mark.parametrize("client", [
    "raised by the transport",
    "a NUL in the configured URL",
])
def test_an_httpx_error_outside_http_error_is_a_refusal(x402, capsys, client):
    if client == "raised by the transport":
        c = Supported(httpx.InvalidURL("bad")).client()
    else:
        c = FacilitatorClient({"url": "https://ho\x00st/x402"},
                              transport=httpx.MockTransport(Supported(kinds(V2_EXACT)).handler))
    assert not issubclass(httpx.InvalidURL, httpx.HTTPError)
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(facilitator.check_facilitator_v2_support(c))
    assert str(exc.value).startswith(PREFIX) and "could not be made (InvalidURL" in str(exc.value)
    x402.setattr(facilitator, "_client", c)
    assert check_config.main() == check_config.EXIT_REFUSED
    assert "could not be made" in capsys.readouterr().err


def test_supported_client_still_parses_whatever_the_status():
    # The client method keeps its documented rule; only the startup check
    # requires a 2xx.
    c = Supported(httpx.Response(500, json={"kinds": [V2_EXACT]})).client()
    assert [k.x402_version for k in asyncio.run(c.supported()).kinds] == [2]
    assert asyncio.run(c.supported_response()).status_code == 500


# --- review round 2: hostile or odd answers ---------------------------------

DEEP = '{"kinds": ' + "[" * 100_000 + "]" * 100_000 + "}"


DEEP_REFUSAL = "answered HTTP 200 with something that is not a list of payment kinds (nested deeper than 32 levels)"


def test_a_deeply_nested_answer_is_a_refusal(x402):
    # Refused before it is parsed: on Python 3.10 (the image's) parsing a body
    # this deep can crash the interpreter rather than raise RecursionError.
    msg = refused(Supported(httpx.Response(200, text=DEEP)))
    assert DEEP_REFUSAL in msg


@pytest.mark.parametrize("text,depth", [
    ('{"kinds": []}', 2),
    ('{"kinds": [{"extra": {"a": [1]}}]}', 5),
    ('{"s": "[[[[{{{{\\"[[[["}', 1),          # brackets in strings do not count
    ("[" * 40 + "]" * 40, 33),                  # stops counting past the limit
])
def test_nesting_depth_is_counted_outside_strings(text, depth):
    assert facilitator._nesting_depth(text) == depth


def test_an_answer_at_the_depth_limit_is_read(x402):
    nested = {"a": 1}
    for _ in range(28):
        nested = {"n": nested}
    check(Supported(kinds(dict(V2_EXACT, extra=nested))))


def test_a_recursion_error_while_parsing_is_a_refusal(x402, monkeypatch):
    # Any parse exception, RecursionError (a RuntimeError) included.
    def deep(*a, **kw):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(facilitator.json, "loads", deep)
    msg = refused(Supported(kinds(V2_EXACT)))
    assert "answered HTTP 200 with something that is not a list of payment kinds (RecursionError)" in msg


def test_a_deeply_nested_answer_stops_startup_cleanly(x402):
    x402.setattr(facilitator, "_client", Supported(httpx.Response(200, text=DEEP)).client())
    x402.setattr(facilitator, "validate_x402_config", lambda: None)

    async def run():
        async with main.lifespan(main.app):
            pass

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(run())
    assert str(exc.value).startswith(PREFIX) and "nested deeper than 32 levels" in str(exc.value)


def test_deploy_check_refuses_a_deeply_nested_answer(x402, capsys):
    x402.setattr(facilitator, "_client", Supported(httpx.Response(200, text=DEEP)).client())
    assert check_config.main() == check_config.EXIT_REFUSED
    err = capsys.readouterr().err
    assert DEEP_REFUSAL in err and "Traceback" not in err


def _safe(text):
    """What may reach the public deploy log: short, printable ASCII, one line."""
    assert len(text) < 2000, len(text)
    assert all(" " <= c <= "~" for c in text), [c for c in text if not " " <= c <= "~"][:5]


HOSTILE_NETWORKS = [
    "eip155:84532\r\nInjected: header",
    "eip155:8453\u00e9",
    "eip155:" + "1" * 200_000,
    "https://attacker.example/phish",
    "eip155:\u202e1",
]
HOSTILE_SCHEMES = ["exact\r\nX: 1", "\u00e9xact", "x" * 200_000, "https://a.example", "UPTO"]


def test_hostile_values_are_not_echoed(x402, capsys):
    hostile = [{"x402Version": 2, "scheme": "exact", "network": n} for n in HOSTILE_NETWORKS]
    hostile += [{"x402Version": 1, "scheme": s, "network": "eip155:84532"} for s in HOSTILE_SCHEMES]
    hostile += [{"x402Version": "1" * 4000, "scheme": "exact", "network": "base-sepolia"}]
    msg = refused(Supported(kinds(*hostile)))
    _safe(msg)
    assert "<invalid>" in msg and "v? exact" in msg
    for bad in ("Injected", "attacker", "\u00e9", "\u202e", "1" * 100, "x" * 100, "UPTO", "a.example"):
        assert bad not in msg
    x402.setattr(facilitator, "_client", Supported(kinds(*hostile)).client())
    assert check_config.main() == check_config.EXIT_REFUSED
    _safe(capsys.readouterr().err.strip())


@pytest.mark.parametrize("network", [["eip155:1"], {"a": 1}, None, 12])
def test_a_kind_of_the_wrong_type_is_unreadable_and_not_echoed(x402, network):
    msg = refused(Supported(kinds({"x402Version": 2, "scheme": "exact", "network": network})))
    _safe(msg)
    assert "not a list of payment kinds (ValidationError)" in msg


def test_at_most_ten_values_then_a_count(x402):
    # 20,000 kinds, 500 distinct v2 networks. That is over 1 MiB, so the cap
    # is raised here to reach the message.
    from app.x402 import facilitator_client
    x402.setattr(facilitator_client, "MAX_SUPPORTED_BYTES", 4 * 1024 * 1024)
    many = [{"x402Version": 2, "scheme": "exact", "network": f"eip155:{n % 500 + 1}"} for n in range(20_000)]
    many += [{"x402Version": 1, "scheme": f"s{n}", "network": "eip155:84532"} for n in range(30)]
    msg = refused(Supported(kinds(*many)))
    _safe(msg)
    assert "and 490 more" in msg and "and 20 more" in msg


def test_an_answer_over_one_mib_is_a_refusal(x402):
    from app.x402.facilitator_client import MAX_SUPPORTED_BYTES
    assert MAX_SUPPORTED_BYTES == 1024 * 1024
    big = {"kinds": [V2_EXACT], "pad": "x" * MAX_SUPPORTED_BYTES}
    msg = refused(Supported(httpx.Response(200, json=big)))
    assert "answered HTTP 200 with something that is not a list of payment kinds (larger than 1048576 bytes)" in msg


def test_a_large_answer_under_the_cap_is_read(x402):
    check(Supported(httpx.Response(200, json={"kinds": [V2_EXACT], "pad": "x" * 900_000})))


def _no_inflating(monkeypatch):
    """Fail the test if httpx ever inflates a /supported answer."""
    from httpx import _decoders

    def boom(self, data):
        raise AssertionError("the /supported answer was inflated")

    for cls in (_decoders.GZipDecoder, _decoders.DeflateDecoder):
        monkeypatch.setattr(cls, "decode", boom)


def _gzip_bomb(layers: int) -> bytes:
    import gzip
    data = json.dumps({"kinds": [V2_EXACT], "pad": "x" * (64 * 1024 * 1024)}).encode()
    for _ in range(layers):
        data = gzip.compress(data, compresslevel=9)
    return data


@pytest.mark.parametrize("encoding,body", [
    ("gzip", lambda: _gzip_bomb(1)),
    ("gzip, gzip", lambda: _gzip_bomb(2)),
    ("deflate", lambda: __import__("zlib").compress(json.dumps({"kinds": [V2_EXACT]}).encode())),
    ("br", lambda: b"\x00" * 16),
    ("identity, gzip", lambda: _gzip_bomb(1)),
])
def test_a_content_encoded_answer_is_refused_without_inflating(x402, monkeypatch, encoding, body):
    import tracemalloc
    _no_inflating(monkeypatch)
    packed = body()
    assert len(packed) < 128 * 1024
    seen = {}

    def handler(request):
        seen["accept"] = request.headers.get("accept-encoding")
        return httpx.Response(200, headers={"Content-Encoding": encoding}, stream=_Raw(packed))

    client = FacilitatorClient({"url": URL}, transport=httpx.MockTransport(handler))
    tracemalloc.start()
    try:
        with pytest.raises(RuntimeError) as exc:
            asyncio.run(facilitator.check_facilitator_v2_support(client))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    msg = str(exc.value)
    assert msg.startswith(PREFIX)
    assert "answered HTTP 200 with something that is not a list of payment kinds (content-encoded" in msg
    assert seen["accept"] == "identity"
    assert peak < 16 * 1024 * 1024, peak        # the bombs inflate to 64 MiB and more


def test_identity_encoding_is_read(x402, monkeypatch):
    _no_inflating(monkeypatch)
    check(Supported(httpx.Response(200, headers={"Content-Encoding": "identity"},
                                   json={"kinds": [V2_EXACT]})))


def test_the_raw_stream_is_capped_and_read_with_aiter_raw(x402, monkeypatch):
    calls = []
    real = httpx.Response.aiter_raw

    def spy(self, *a, **kw):
        calls.append(1)
        return real(self, *a, **kw)

    monkeypatch.setattr(httpx.Response, "aiter_raw", spy)
    monkeypatch.setattr(httpx.Response, "aiter_bytes",
                        lambda self, *a, **kw: (_ for _ in ()).throw(AssertionError("decoded read")))
    check(Supported(kinds(V2_EXACT)))
    assert calls


def test_a_deployment_check_refuses_a_gzip_answer(x402, monkeypatch, capsys):
    _no_inflating(monkeypatch)
    packed = _gzip_bomb(2)
    client = FacilitatorClient({"url": URL}, transport=httpx.MockTransport(
        lambda r: httpx.Response(200, headers={"Content-Encoding": "gzip, gzip"}, stream=_Raw(packed))))
    x402.setattr(facilitator, "_client", client)
    assert check_config.main() == check_config.EXIT_REFUSED
    assert "content-encoded" in capsys.readouterr().err


# --- review round 3: text encodings ---------------------------------------

def _deep_kinds_text(levels: int) -> str:
    # The reviewer's shape: kinds whose string holds U+2200, nested deep.
    return '{"kinds": [{"x402Version": 2, "scheme": "∀", "network": "eip155:84532", "extra": ' \
           + "[" * levels + "]" * levels + "}]}"


DEEP_TEXT = _deep_kinds_text(200_000)

ENCODED = {
    "utf-16-le": DEEP_TEXT.encode("utf-16-le"),
    "utf-16-be": DEEP_TEXT.encode("utf-16-be"),
    "utf-16 (BOM)": DEEP_TEXT.encode("utf-16"),
    "utf-32-le": DEEP_TEXT.encode("utf-32-le"),
    "utf-8 BOM": b"\xef\xbb\xbf" + DEEP_TEXT.encode("utf-8"),
}


def test_the_utf16_body_used_to_pass_a_byte_count():
    # Why the count runs on decoded UTF-8 text: in UTF-16LE, U+2200 is the
    # bytes 00 22, and 0x22 is a quote to a byte-level scanner.
    assert "∀".encode("utf-16-le") == b"\x00\x22"


@pytest.mark.parametrize("name", list(ENCODED))
def test_a_deep_body_in_any_encoding_is_refused(x402, name):
    body = ENCODED[name]
    assert len(body) <= 1024 * 1024 or name == "utf-32-le"
    from app.x402 import facilitator_client
    x402.setattr(facilitator_client, "MAX_SUPPORTED_BYTES", 4 * 1024 * 1024)   # reach the decode step
    msg = refused(Supported(httpx.Response(200, content=body)))
    if name == "utf-8 BOM":
        assert "nested deeper than 32 levels" in msg
    else:
        assert "not a list of payment kinds (not UTF-8" in msg


@pytest.mark.parametrize("name", list(ENCODED))
def test_deploy_check_refuses_a_deep_body_in_any_encoding(x402, capsys, name):
    from app.x402 import facilitator_client
    x402.setattr(facilitator_client, "MAX_SUPPORTED_BYTES", 4 * 1024 * 1024)
    x402.setattr(facilitator, "_client", Supported(httpx.Response(200, content=ENCODED[name])).client())
    assert check_config.main() == check_config.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "not a list of payment kinds" in err and "Traceback" not in err


LIFESPAN_DRIVER = r"""
import asyncio, sys
import httpx
from app.core.config import settings
from app.x402 import facilitator, facilitator_client
from app import main

body = open(sys.argv[1], "rb").read()

class Raw(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield body

settings.X402_ENABLED = True
settings.X402_PROTOCOL_VERSIONS = "1,2"
settings.X402_NETWORK = "base-sepolia"
facilitator_client.MAX_SUPPORTED_BYTES = 4 * 1024 * 1024
facilitator._client = facilitator_client.FacilitatorClient(
    {"url": "https://facilitator.example/x402"},
    transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Raw())))
facilitator.validate_x402_config = lambda: None

async def run():
    async with main.lifespan(main.app):
        pass

try:
    asyncio.run(run())
except RuntimeError as e:
    print("REFUSED:", e)
    sys.exit(3)
print("STARTED")
"""


@pytest.mark.parametrize("name", list(ENCODED))
def test_lifespan_refuses_a_deep_body_in_any_encoding(tmp_path, name):
    """In a subprocess, on the running Python (the 3.10 gate runs it on 3.10),
    so a crash would show as a return code rather than end the test run."""
    import os
    import subprocess
    import sys
    from pathlib import Path
    body = tmp_path / "body"
    body.write_bytes(ENCODED[name])
    repo = Path(__file__).resolve().parent.parent
    out = subprocess.run([sys.executable, "-c", LIFESPAN_DRIVER, str(body)], cwd=repo,
                         env={**os.environ, "SWARM_BEE_API_URL": "http://localhost:1"},
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 3, (out.returncode, out.stderr[-2000:])
    assert "REFUSED: " + PREFIX in out.stdout and "not a list of payment kinds" in out.stdout


def test_invalid_utf8_is_refused(x402):
    msg = refused(Supported(httpx.Response(200, content=b'{"kinds": ["\xff"]}')))
    assert "(not UTF-8)" in msg


@pytest.mark.parametrize("ch", ["\x00", "\x01", "\x1f", "\x0b"])
def test_a_raw_control_character_is_refused(x402, ch):
    body = ('{"kinds": [{"x402Version": 2, "scheme": "exact", "network": "eip155:84532"}]' + ch + "}").encode()
    msg = refused(Supported(httpx.Response(200, content=body)))
    assert "control character" in msg


def test_a_utf8_bom_is_stripped_and_the_answer_read(x402):
    check(Supported(httpx.Response(200, content=b"\xef\xbb\xbf" + json.dumps({"kinds": [V2_EXACT]}).encode())))


def test_non_ascii_utf8_is_read(x402):
    check(Supported(httpx.Response(200, content=json.dumps(
        {"kinds": [V2_EXACT], "note": "∀ é"}, ensure_ascii=False).encode("utf-8"))))


# --- review round 3: an overall deadline ----------------------------------

class _Drip(httpx.AsyncByteStream):
    """One byte, then another every `gap` seconds, forever."""

    def __init__(self, gap):
        self.gap = gap

    async def __aiter__(self):
        yield b'{"kinds": ['
        while True:
            await asyncio.sleep(self.gap)
            yield b" "


def test_a_slow_drip_answer_hits_the_overall_deadline(x402):
    import time
    x402.setattr(facilitator, "SUPPORTED_DEADLINE_SECONDS", 0.5)
    client = FacilitatorClient({"url": URL}, transport=httpx.MockTransport(
        lambda r: httpx.Response(200, stream=_Drip(0.05))))
    started = time.monotonic()
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(facilitator.check_facilitator_v2_support(client))
    assert time.monotonic() - started < 5
    msg = str(exc.value)
    assert msg.startswith(PREFIX)
    assert "could not be reached (timed out after 0.5 s)" in msg


def test_the_default_deadline_is_15_seconds():
    assert facilitator.SUPPORTED_DEADLINE_SECONDS == 15.0


def test_deploy_check_refuses_a_slow_drip(x402, capsys):
    x402.setattr(facilitator, "SUPPORTED_DEADLINE_SECONDS", 0.3)
    x402.setattr(facilitator, "_client", FacilitatorClient({"url": URL}, transport=httpx.MockTransport(
        lambda r: httpx.Response(200, stream=_Drip(0.05)))))
    assert check_config.main() == check_config.EXIT_REFUSED
    assert "timed out after 0.3 s" in capsys.readouterr().err


def test_an_oversized_non_2xx_answer_names_its_status(x402):
    msg = refused(Supported(httpx.Response(503, text="x" * (2 * 1024 * 1024))))
    assert "HTTP 503" in msg


@pytest.mark.parametrize("version", ["2", 2.0, True, [2], None])
def test_x402_version_must_be_the_int_2(x402, version):
    msg = refused(Supported(kinds({"x402Version": version, "scheme": "exact", "network": "eip155:84532"})))
    assert "does not list" in msg or "not a list of payment kinds" in msg


def test_scheme_and_network_must_be_exact_strings(x402):
    refused(Supported(kinds({"x402Version": 2, "scheme": "EXACT", "network": "eip155:84532"})))
    refused(Supported(kinds({"x402Version": 2, "scheme": "exact", "network": "EIP155:84532"})))
