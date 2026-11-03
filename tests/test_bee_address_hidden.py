# tests/test_bee_address_hidden.py
"""Unauthenticated responses must not reveal the internal Bee API address (#549).

httpx's HTTPStatusError text names the request URL, and Go network errors name
the address they dialled or the host they failed to resolve. /health x402
warnings, stamp purchase and extend errors, and /pool/status used to copy such
text into the response. Each response now names the failure by HTTP status or
exception class, and Bee's own message keeps its wording with the addresses
replaced. The full text is still logged for the operator.

All hosts and addresses here are made up (bee.internal, 10.0.0.1, 192.0.2.x,
fd00::/8).
"""
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.services.bee_errors import without_addresses

BEE = "http://bee.internal:1633"
SECRETS = ("bee.internal", "1633", "10.0.0.1", "192.0.2.7", "127.0.0.11",
           "fd00::1", "rpc.internal", "8545")
STAMP_ID = "a" * 64

client = TestClient(app)

# Hosts the gateway is configured with in these tests. The scrubber replaces
# a bare word (or a hyphenated one without a dot) only when it is one of
# these, so "timeout:30" and "chain-id:100" stay (#549).
CONFIGURED = {
    "SWARM_BEE_API_URL": "http://bee:1633",
    "GNOSIS_RPC_URL": "http://bee-dev:1633/",
    "BASE_RPC_URL": "https://rpc.internal/v2/not-a-real-key",
    "X402_FACILITATOR_URL": "https://bee.internal/facilitator",
}


@pytest.fixture(autouse=True)
def configured_hosts():
    patches = [patch.object(settings, k, v) for k, v in CONFIGURED.items()]
    for p in patches:
        p.start()
    yield
    for p in reversed(patches):
        p.stop()


def _status_error(path, status=503, method="GET", **response_kwargs):
    """An HTTPStatusError exactly as httpx raises it for a Bee answer."""
    request = httpx.Request(method, f"{BEE}/{path}")
    response = httpx.Response(status, request=request, **response_kwargs)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        assert BEE in str(e)  # the premise: httpx names the URL
        return e
    raise AssertionError("raise_for_status did not raise")


def _connect_error(path):
    request = httpx.Request("GET", f"{BEE}/{path}")
    return httpx.ConnectError(
        "dial tcp 10.0.0.1:1633: connect: connection refused", request=request)


def _assert_hidden(text):
    leaks = [s for s in SECRETS if s in text]
    assert leaks == [], f"internal address exposed: {leaks} in {text}"


# --------------------------------------------------------------------------- #
# /health x402 warnings
# --------------------------------------------------------------------------- #
class TestHealthWarningsHideBeeAddress:

    def _health(self, exc):
        with patch.object(settings, "X402_ENABLED", True), \
             patch("app.services.swarm_api.get_node_status_summary",
                   new=AsyncMock(return_value={})), \
             patch("app.x402.preflight.get_wallet_info", new=AsyncMock(side_effect=exc)), \
             patch("app.x402.preflight.get_chequebook_info", new=AsyncMock(side_effect=exc)), \
             patch("app.x402.preflight.get_chequebook_balance", new=AsyncMock(side_effect=exc)), \
             patch("app.x402.preflight.logger") as log:
            response = client.get("/health")
        assert response.status_code == 200, response.text
        logged = " ".join(str(c) for c in log.error.call_args_list)
        return response.json(), logged

    def test_bee_status_error_hides_url(self):
        body, logged = self._health(_status_error("wallet", 503))

        _assert_hidden(json.dumps(body))
        warnings = body["x402"]["warnings"]
        fetch = [w for w in warnings if w.startswith("Failed to fetch")]
        assert len(fetch) == 3, warnings
        assert all("HTTP 503" in w for w in fetch), fetch
        # The 'Failed to fetch' prefix still triggers the reachability hints.
        assert any("Bee node may be unreachable" in w for w in warnings), warnings
        assert BEE in logged, "the log must keep the full text"

    @pytest.mark.parametrize("exc, name", [
        (_connect_error("wallet"), "ConnectError"),
        (httpx.ReadTimeout(f"timed out reading {BEE}/wallet",
                           request=httpx.Request("GET", f"{BEE}/wallet")), "ReadTimeout"),
    ])
    def test_bee_transport_error_names_class(self, exc, name):
        body, logged = self._health(exc)

        _assert_hidden(json.dumps(body))
        fetch = [w for w in body["x402"]["warnings"] if w.startswith("Failed to fetch")]
        assert fetch and all(f"({name})" in w for w in fetch), fetch
        assert "bee.internal" in logged or "10.0.0.1" in logged


class TestBaseBalanceWarningHidesRpcUrl:
    """The Base ETH warning is not in /health today, but must not hold the RPC URL."""

    @pytest.mark.asyncio
    async def test_rpc_status_error_hides_url_and_key(self):
        from app.x402 import base_balance
        rpc = "https://base-rpc.internal/v2/not-a-real-key-123"
        request = httpx.Request("POST", rpc)
        exc = httpx.HTTPStatusError(
            f"Client error '401 Unauthorized' for url '{rpc}'", request=request,
            response=httpx.Response(401, request=request))
        base_balance.clear_balance_cache()

        with patch.object(base_balance.settings, "X402_PAY_TO_ADDRESS",
                          "0x" + "1" * 40), \
             patch.object(base_balance, "_get_eth_balance_from_rpc",
                          new=AsyncMock(side_effect=exc)), \
             patch.object(base_balance, "logger") as log:
            result = await base_balance.check_base_eth_balance()

        assert "base-rpc.internal" not in result["warning"]
        assert "not-a-real-key" not in result["warning"]
        assert "HTTP 401" in result["warning"], result["warning"]
        assert "not-a-real-key" in " ".join(str(c) for c in log.error.call_args_list)


# --------------------------------------------------------------------------- #
# Stamp purchase and extend
# --------------------------------------------------------------------------- #
_FUNDS = {"sufficient": True, "required_bzz": 0.01, "wallet_balance_bzz": 100.0, "shortfall_bzz": 0}


def _purchase(exc):
    with patch("app.services.swarm_api.check_sufficient_funds", return_value=_FUNDS), \
         patch("app.services.swarm_api.purchase_postage_stamp", new=AsyncMock(side_effect=exc)):
        return client.post("/api/v1/stamps/", json={"amount": 100, "depth": 17})


def _extend(exc):
    with patch("app.services.swarm_api.get_chainstate",
               return_value={"currentPrice": "1", "minimumValidityBlocks": 17280}), \
         patch("app.services.swarm_api.extend_postage_stamp", new=AsyncMock(side_effect=exc)), \
         patch("app.services.swarm_api.check_sufficient_funds", return_value=_FUNDS), \
         patch("app.services.swarm_api.calculate_stamp_total_cost", return_value=1000), \
         patch("app.services.swarm_api.get_all_stamps_processed",
               return_value=[{"batchID": STAMP_ID, "depth": 17, "local": True}]):
        return client.patch(f"/api/v1/stamps/{STAMP_ID}/extend", json={"amount": 50000})


RPC_MESSAGE = ('Post "http://rpc.internal:8545": dial tcp 10.0.0.1:8545: '
               'connect: connection refused')
LOOKUP_TEXT = "dial tcp: lookup bee.internal on 127.0.0.11:53: no such host"


@pytest.mark.parametrize("call, verb", [(_purchase, "purchase"), (_extend, "extension")])
class TestStampErrorsHideBeeAddress:

    def test_empty_body_4xx_gives_status_not_url(self, call, verb):
        response = call(_status_error("stamps/100/17", 400, method="POST"))

        assert response.status_code == 400, response.text
        detail = response.json()["detail"]
        _assert_hidden(detail)
        assert detail == f"Bee rejected the stamp {verb}: HTTP 400", detail

    def test_bee_json_message_keeps_text_hides_addresses(self, call, verb):
        exc = _status_error("stamps/100/17", 400, method="POST",
                            json={"code": 400, "message": RPC_MESSAGE})
        response = call(exc)

        assert response.status_code == 400, response.text
        detail = response.json()["detail"]
        _assert_hidden(detail)
        assert "connect: connection refused" in detail, detail

    def test_bee_text_body_hides_resolver_host(self, call, verb):
        exc = _status_error("stamps/100/17", 404, method="POST", text=LOOKUP_TEXT)
        response = call(exc)

        assert response.status_code == 404, response.text
        detail = response.json()["detail"]
        _assert_hidden(detail)
        assert "lookup <host> on <address>: no such host" in detail, detail

    def test_long_bee_message_is_capped_and_fast(self, call, verb):
        # Bee's text is untrusted and of any length; it is capped at 200
        # characters before scrubbing, so a 100 KB message cannot stall the
        # request (#549, round-2 review).
        import time
        exc = _status_error("stamps/100/17", 400, method="POST",
                            json={"code": 400, "message": "a" * 100_000})
        start = time.perf_counter()
        response = call(exc)
        assert time.perf_counter() - start < 1.0

        assert response.status_code == 400, response.text
        detail = response.json()["detail"]
        assert detail == f"Bee rejected the stamp {verb}: " + "a" * 200

    def test_bee_message_kept_as_is(self, call, verb):
        exc = _status_error("stamps/100/17", 400, method="POST",
                            json={"code": 400, "message": "out of funds"})
        response = call(exc)

        assert response.json()["detail"] == f"Bee rejected the stamp {verb}: out of funds"

    def test_connect_error_is_generic_502(self, call, verb):
        response = call(_connect_error("stamps/100/17"))

        assert response.status_code == 502, response.text
        _assert_hidden(response.json()["detail"])


# --------------------------------------------------------------------------- #
# /pool/status
# --------------------------------------------------------------------------- #
class TestPoolStatusHidesAddresses:
    """#534 replaced scheme URLs; bare addresses and resolver hosts went through."""

    @pytest.mark.parametrize("message, kept", [
        (RPC_MESSAGE, "connect: connection refused"),
        (LOOKUP_TEXT, "no such host"),
        ("dial tcp [fd00::1]:1633: i/o timeout; peer 192.0.2.7 gone", "i/o timeout"),
        ("upstream bee.internal:1633 failed", "upstream <address> failed"),
        ("could not reach bee:1633. tried rpc.internal:8545/v2/KEY.", "could not reach <address>."),
    ])
    @pytest.mark.asyncio
    async def test_purchase_error_hides_bare_addresses(self, tmp_path, message, kept):
        from app.services.stamp_pool import StampPoolManager
        state_file = str(tmp_path / "pool_state.json")
        with open(state_file, "w") as f:
            json.dump([], f)
        manager = StampPoolManager(state_file=state_file)
        exc = _status_error("stamps/1000/17", 500, method="POST",
                            json={"code": 500, "message": message})

        with patch("app.services.stamp_pool.settings.STAMP_POOL_ENABLED", True), \
             patch("app.services.swarm_api.get_all_stamps_processed",
                   new=AsyncMock(return_value=[])), \
             patch.object(manager, "_update_stamp_ttls", new=AsyncMock()), \
             patch.object(manager, "_purchase_stamp", new=AsyncMock(side_effect=exc)):
            await manager.check_and_replenish()

        with patch("app.api.endpoints.pool.stamp_pool_manager", manager):
            response = client.get("/api/v1/pool/status")
        assert response.status_code == 200, response.text
        errors = response.json()["errors"]
        failed = [e for e in errors if e.startswith("Failed to purchase")]
        assert failed, errors
        _assert_hidden(json.dumps(errors))
        assert all("HTTP 500" in e and kept in e for e in failed), failed


# --------------------------------------------------------------------------- #
# without_addresses
# --------------------------------------------------------------------------- #
class TestWithoutAddresses:

    @pytest.mark.parametrize("text, expected", [
        ("Get http://bee:1633/x: EOF", "Get <url>: EOF"),
        ("(see http://bee.internal/x).", "(see <url>)."),
        ("failed at http://bee.internal:1633/stamps; retry", "failed at <url>; retry"),
        ("dial tcp 10.0.0.1:8545: connect: refused", "dial tcp <address>: connect: refused"),
        ("peer 192.0.2.7 gone", "peer <address> gone"),
        ("lookup bee.internal on 127.0.0.11:53: no such host",
         "lookup <host> on <address>: no such host"),
        ("dial tcp [fd00::1]:1633: refused", "dial tcp <address>: refused"),
        ("peer fd00::1 gone", "peer <address> gone"),
        ("bee.internal:1633 down", "<address> down"),
        ("dial tcp bee:1633: refused", "dial tcp <address>: refused"),
        ("dial tcp bee-dev:1633: refused", "dial tcp <address>: refused"),
        ("upstream bee:80 down", "upstream <address> down"),
        # An address at the end of a sentence (round-1 review of #563).
        ("connection refused by 10.0.0.1.", "connection refused by <address>."),
        ("connection to bee.internal:1633.", "connection to <address>."),
        ("could not reach bee:1633.", "could not reach <address>."),
        ("dial fd00::1: unreachable", "dial <address>: unreachable"),
        ("dial tcp 10.0.0.1:1633.", "dial tcp <address>."),
        # Credentials and paths without a scheme.
        ("Post user:pass@rpc.internal:8545 x", "Post <address> x"),
        ("rpc.internal:8545/v2/KEY", "<address>"),
        ("call 10.0.0.1:8545/v2/KEY.", "call <address>."),
        ("call [fd00::1]:1633/v2/KEY failed", "call <address> failed"),
        # Percent-encoded URLs and case.
        ("redirect=http%3A%2F%2Fbee.internal%3A1633%2Fx end", "redirect=<url> end"),
        ("HTTP://BEE.INTERNAL:1633/x failed", "<url> failed"),
        ("Lookup bee.internal on 127.0.0.11:53", "Lookup <host> on <address>"),
        ("dial tcp: LOOKUP bee: no such host", "dial tcp: LOOKUP <host>: no such host"),
        # IPv4-mapped IPv6 as a whole.
        ("peer ::ffff:10.0.0.1 gone", "peer <address> gone"),
        ("peer ::FFFF:192.0.2.7.", "peer <address>."),
        # Bare host names: Go's x509 message and internal-only suffixes.
        ("x509: certificate is valid for rpc.internal, other.internal, not bee.example",
         "x509: certificate is valid for <host>, not <host>"),
        ("x509: certificate is not valid for any names, but wanted to match bee.example.",
         "x509: certificate is not valid for any names, but wanted to match <host>."),
        ("no route to bee.svc.cluster.local", "no route to <host>"),
        ("gateway gw.site.lan, node n1.house.home and bee.cluster.local failed",
         "gateway <host>, node <host> and <host> failed"),
        ("service bee.ns.svc down", "service <host> down"),
        # Configured hosts, bare or hyphenated, with any port (round 2).
        ("dial bee:1000", "dial <address>"),
        ("bee:8", "<address>"),
        ("bee:99999", "<address>"),
        ("address bee:99999: invalid port", "address <address>: invalid port"),
        ("lookup bee-dev.", "lookup <host>."),
        # The cost of matching configured names as whole words in any case:
        # with the Bee host named "bee", the word itself is replaced too.
        ("Bee is syncing", "<host> is syncing"),
        ("dial BEE-DEV:1633 failed", "dial <address> failed"),
        ("lookup x on 10.0.0.1:53", "lookup <host> on <address>"),
        # Invalid ports, dotted host with a path, '@' in a password.
        ("dial 10.0.0.1:99999", "dial <address>"),
        ("rpc.example.com/v2/KEY failed", "<address> failed"),
        ("user:p@ss@rpc.example.com:8545 x", "<address> x"),
        # Partial and double percent-encoding.
        ("bee%2Einternal%3A1633", "<address>"),
        ("peer 10%2E0%2E0%2E1", "peer <address>"),
        ("http%253A%252F%252Fbee.internal%253A1633%252Fx end", "<url> end"),
        # Leading zeros, port fragments, multiaddrs.
        ("peer 010.000.000.001 gone", "peer <address> gone"),
        ("peer ::ffff:10.0.0.1:1633 gone", "peer <address> gone"),
        ("dial 10.0.0.1:1633_x", "dial <address>_x"),
        ("/dns4/bee.example.com/tcp/1634", "/dns4/<host>/tcp/<port>"),
        ("/ip4/10.0.0.1/tcp/1634/p2p/QmPeerId", "/ip4/<address>/tcp/<port>/p2p/QmPeerId"),
        ("/ip6/fd00::1/udp/1634/quic", "/ip6/<address>/udp/<port>/quic"),
        ("resolve bee.internal.", "resolve <host>."),
    ])
    def test_replaces_addresses(self, text, expected):
        assert without_addresses(text) == expected

    @pytest.mark.parametrize("text", [
        # Bee messages this gateway already handles (see the existing tests).
        "out of funds",
        "insufficient amount for 24h minimum validity",
        "insufficient funds",
        "internal error",
        "not found",
        # Go and JSON-ish "key:number" forms that are not addresses.
        "status:500",
        "code:400",
        "unexpected response status:500 code:400",
        "error:1 line:12 depth:17",
        # Numbers that look like addresses but are not.
        "at 12:34:56",
        "slice [0:10]",
        "node version 2.8.1",
        "x 1.2.300.4",
        "batch 0xabc: not found",
        "HTTP 400",
        # "key:number" fields with a short number or a known field name.
        "want min:17",
        "timeout:30",
        "amount:100",
        "batch:17",
        "amount:10000",
        "status:443",
        # RFC 3339 timestamps.
        "expires 2026-11-03T10:00:00Z",
        "at 2026-11-03T10:00:00.123+01:00.",
        "between 2026-11-03 10:00:00 and 2026-11-04 10:00:00",
        # Go source positions and ordinary words.
        "panic at main.go:1633",
        "lookup failed",
        "e.g. this, i.e. that.",
        # Fields that look like word:number (round 2); none is a configured host.
        "CurrentPrice:24000",
        "gasLimit:21000",
        "nonce:1234",
        "expected:2048 actual:1024",
        "neighborhood:1633",
        "pageSize:1024",
        "chain-id:100",
        "retry-after:120",
        "content-length:4096",
        "pool-17-abcd:1",
        "lookup failed: no such host",
        # Short IPv6-like forms and ordinary .local/.home words.
        "::",
        "a::b",
        "cafe::",
        "file.local",
        "my.home",
        "see app/services/x.py/ and/or",
    ])
    def test_keeps_text_that_is_not_an_address(self, text):
        assert without_addresses(text) == text

    @pytest.mark.parametrize("unit", [
        "a", "1", ".", ":", "-", "%3A", "@", "a.", "a:", "1.", "1.1.1.1:", "a-",
        "a@", "a:a@", "::", "a::", "/", "a.b/", "http://", "a://", "%25",
        "/ip4/", "lookup a.", "certificate is valid for a, ", "[1:", "a.a:1",
        "bee:", "a.internal.", "::ffff:1.1.1.1:",
    ])
    def test_100kb_adversarial_input_is_fast(self, unit):
        import time
        text = (unit * (100_000 // len(unit) + 1))[:100_000]
        start = time.perf_counter()
        without_addresses(text)
        assert time.perf_counter() - start < 0.5, f"slow on {unit!r} x 100 KB"
