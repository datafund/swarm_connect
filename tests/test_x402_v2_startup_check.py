"""The startup /supported check for x402 v2 (#608, D3 in #596).

With x402 enabled and 2 in X402_PROTOCOL_VERSIONS, the gateway starts only if
the facilitator's GET /supported lists x402Version 2, scheme exact, on the
configured network as its CAIP-2 id. Anything else, an unreachable facilitator
or an unreadable answer included, refuses to start (fail closed), with the
same prefix as validate_x402_config's refusals. The facilitator is a mock
transport here; nothing is sent anywhere.
"""
import asyncio

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
    assert "network eip155:8453" in msg and "['eip155:84532']" in msg


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
    assert "['eip155:8453']" in msg


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
