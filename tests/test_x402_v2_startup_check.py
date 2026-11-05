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


class Supported:
    """A /supported that answers `answer` (a response, or an exception to raise)."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def handler(self, request):
        self.calls.append((request.method, request.url.path))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

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


def test_a_deeply_nested_answer_is_a_refusal(x402):
    msg = refused(Supported(httpx.Response(200, text=DEEP)))
    assert "answered HTTP 200 with something that is not a list of payment kinds (RecursionError)" in msg


def test_a_deeply_nested_answer_stops_startup_cleanly(x402):
    x402.setattr(facilitator, "_client", Supported(httpx.Response(200, text=DEEP)).client())
    x402.setattr(facilitator, "validate_x402_config", lambda: None)

    async def run():
        async with main.lifespan(main.app):
            pass

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(run())
    assert str(exc.value).startswith(PREFIX) and "RecursionError" in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


def test_deploy_check_refuses_a_deeply_nested_answer(x402, capsys):
    x402.setattr(facilitator, "_client", Supported(httpx.Response(200, text=DEEP)).client())
    assert check_config.main() == check_config.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "not a list of payment kinds (RecursionError)" in err and "Traceback" not in err


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


def test_the_cap_applies_to_the_decoded_body(x402):
    import gzip
    body = json.dumps({"kinds": [V2_EXACT], "pad": "x" * (4 * 1024 * 1024)}).encode()
    packed = gzip.compress(body)
    assert len(packed) < 64 * 1024
    msg = refused(Supported(httpx.Response(200, content=packed, headers={"Content-Encoding": "gzip"})))
    assert "larger than 1048576 bytes" in msg


def test_a_gzip_answer_under_the_cap_is_read(x402):
    import gzip
    packed = gzip.compress(json.dumps({"kinds": [V2_EXACT]}).encode())
    check(Supported(httpx.Response(200, content=packed, headers={"Content-Encoding": "gzip"})))


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
