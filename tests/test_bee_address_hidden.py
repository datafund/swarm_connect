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
from app.services.bee_errors import public_bee_text, without_addresses

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
    "X402_BZZ_PRICE_FEED_URL": "http://bee-dev:1633/",
    "GNOSIS_RPC_URL": "https://gnosis.rpc.example.com/v2/0123abcdKEY4567",
    "BASE_RPC_URL": "https://user:hunter22@rpc.internal/v2/not-a-real-key?apikey=QUERYKEY123",
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
        # Bee's text is untrusted and of any length; it is scrubbed at 2 KB
        # and cut to 200 characters, so a 100 KB message cannot stall the
        # request (#549, round-2 and round-3 review).
        import time
        exc = _status_error("stamps/100/17", 400, method="POST",
                            json={"code": 400, "message": "out of funds " * 8000})
        start = time.perf_counter()
        response = call(exc)
        assert time.perf_counter() - start < 1.0

        assert response.status_code == 400, response.text
        detail = response.json()["detail"]
        prefix = f"Bee rejected the stamp {verb}: "
        assert detail.startswith(prefix + "out of funds out of funds"), detail
        assert detail.endswith("…") and len(detail) <= len(prefix) + 201, detail

    # The address starts at character filler + 10; these fillers put the
    # 200-character cut inside each address.
    @pytest.mark.parametrize("filler", [172, 176, 180, 183])
    def test_address_at_the_cut_is_not_shown_in_part(self, call, verb, filler):
        # Measured in round 3: cutting before scrubbing showed
        # "rpc.internal.example.com" without its port, or "10.0.0.".
        for address in ("rpc.internal.example.com:8545", "10.0.0.1:8545",
                        "fd00:1234:5678::1"):
            message = "x" * filler + " dial tcp " + address + " refused"
            exc = _status_error("stamps/100/17", 400, method="POST",
                                json={"code": 400, "message": message})
            detail = call(exc).json()["detail"]
            for part in ("rpc.internal", "example.com", "10.0.0", "fd00", "1234:5678"):
                assert part not in detail, (address, detail)

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
        # An address across the 200-character cut is not shown in part.
        ("y" * 190 + " dial tcp 10.0.0.1:8545 refused", "…"),
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
        # A dotless configured host with a port, in a URL, after "lookup",
        # after credentials, or in a multiaddr.
        ("dial tcp bee:1633", "dial tcp <address>"),
        ("Get http://BEE:1633/x", "Get <url>"),
        ("lookup bee on 127.0.0.11:53", "lookup <host> on <address>"),
        ("Post user:pass@bee:1633 x", "Post <address> x"),
        ("Post user:pass@bee x", "Post <host> x"),
        ("fetch BEE/stamps failed", "fetch <address> failed"),
        ("/dns4/bee/tcp/1634", "/dns4/<host>/tcp/<port>"),
        # A dotted configured host is replaced even without a port.
        ("reached rpc.internal then failed", "reached <host> then failed"),
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
        "bee version 2.8.1",
        # A dotless configured host as a plain word: a compose service name
        # (public in docker-compose.yml) and a word in Bee's own text.
        "Bee is syncing",
        "bee node not ready, retry",
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
        "<a.b:1", "@a.b/", "%3Ca", "a:/@", "bee/", "SECRETKEY",
        "(a:", "a:(", "[", "@a", ":@a", "a:a:a@b", "x=a:", "日1.",
    ])
    def test_100kb_adversarial_input_is_fast(self, unit):
        import time
        text = (unit * (100_000 // len(unit) + 1))[:100_000]
        start = time.perf_counter()
        without_addresses(text)
        assert time.perf_counter() - start < 0.5, f"slow on {unit!r} x 100 KB"


# --------------------------------------------------------------------------- #
# Round 3: hosts after "<", "@" and "/", queries, configured secrets, the cut
# --------------------------------------------------------------------------- #
class TestRoundThree:

    @pytest.mark.parametrize("text, expected", [
        # A host directly after "<", "@" or "/".
        ("<gnosis.rpc.example.com/v2/SECRETKEY>", "<<address>>"),
        ("admin@gnosis.rpc.example.com/v2/SECRETKEY", "admin@<address>"),
        ("<bee.internal:1633>", "<<address>>"),
        ("/bee.internal:1633", "/<address>"),
        ("admin@bee.internal:1633", "admin@<address>"),
        ("<host.docker.internal>", "<<host>>"),
        ("%3Cbee.internal:1633%3E", "<<address>>"),
        # The multiaddr form is unchanged.
        ("/dns4/bee.example.com/tcp/1634", "/dns4/<host>/tcp/<port>"),
        # A query or fragment after a schemeless host.
        ("bee.internal:1633?token=SECRET", "<address>"),
        ("gnosis.rpc.example.com?apikey=SECRET", "<address>"),
        ("10.0.0.1:8545?key=SECRET", "<address>"),
        ("call rpc.example.org#frag failed", "call <address> failed"),
        # Secrets of the configured URLs, wherever they appear.
        ("invalid project id 0123abcdKEY4567", "invalid project id <secret>"),
        ("apikey QUERYKEY123 expired", "apikey <secret> expired"),
        ("auth hunter22 rejected", "auth <secret> rejected"),
        # A password holding "/".
        ("Post user:pa/ss@bee:1633 x", "Post <address> x"),
        # A dotless configured host followed by a path that is not a version.
        ("GET BEE/stamps failed", "GET <address> failed"),
        ("GET bee?x=1 failed", "GET <address> failed"),
    ])
    def test_replaces(self, text, expected):
        assert without_addresses(text) == expected

    @pytest.mark.parametrize("text", [
        # User agents and versions after a dotless configured host.
        "bee/2.8.1-abcdef",
        "Bee/2.8.1 user agent",
        # A question about bee is not a query.
        "is it bee? yes",
        # Percent signs that are not an encoded address.
        "100%ff",
        "50%25 done",
        # The facilitator route is a lowercase word, not a secret; nor are
        # single-case or hyphenated words in a configured URL's path.
        "facilitator refused",
        "invalid project id SECRETKEY",
        "bad key not-a-real-key",
        # Kept by design: unconfigured dotless hosts, bare dotted names,
        # short IPv6 forms.
        "nethermind:8545",
        "localhost:8545",
        "rpc.example.com",
        "::1",
        "2001:db8::",
    ])
    def test_keeps(self, text):
        assert without_addresses(text) == text

    def test_schemeless_setting_is_a_configured_host(self):
        with patch.object(settings, "X402_BZZ_PRICE_FEED_URL", "pricefeed:8080"):
            assert without_addresses("dial pricefeed:8080 failed") == "dial <address> failed"
            assert without_addresses("GET pricefeed/v1 failed") == "GET <address> failed"

    def test_placeholder_characters_in_input_are_dropped(self):
        assert without_addresses("a\ue001b 10.0.0.1") == "ab <address>"

    @pytest.mark.parametrize("text, expected", [
        ("short", "short"),
        # Longer than 200 before scrubbing, within 200 after it: kept whole.
        ("x" * 185 + " fd00:1234:5678::1 end", "x" * 185 + " <address> end"),
        # The cut lands inside the placeholder: the word is dropped.
        ("x" * 195 + " fd00:1234:5678::1 end", "x" * 195 + "…"),
        ("y" * 190 + " dial tcp rpc.internal.example.com:8545 refused",
         "y" * 190 + " dial tcp…"),
        # No whitespace before the cut: nothing is shown (the caller shows
        # the HTTP status), not a bare "…".
        ("z" * 199 + "a" * 10, None),
    ])
    def test_public_bee_text_cut(self, text, expected):
        assert public_bee_text(text) == expected

    def test_address_at_the_2kb_scrub_cap_is_dropped(self):
        text = "<url> " + "http://" + "a" * 2030 + "/x 10.0.0.1:8545 more"
        assert "10.0" not in public_bee_text(text)
        long = "w " * 1020 + "10.0.0.1:8545 end"  # the address straddles 2048
        shown = public_bee_text(long, limit=5000)
        assert "10.0" not in shown and shown.endswith("…"), shown[-40:]


# --------------------------------------------------------------------------- #
# Round 4
# --------------------------------------------------------------------------- #
class TestRoundFour:

    @pytest.mark.parametrize("text, expected", [
        # x509 names are taken whole, hyphens included.
        ("x509: certificate is valid for a.example, not my-bee-gw",
         "x509: certificate is valid for <host>, not <host>"),
        ("x509: certificate is not valid for any names, but wanted to match rpc-gw-01.corp.",
         "x509: certificate is not valid for any names, but wanted to match <host>."),
        ("x509: certificate is valid for a.example, not my-bee-gw; retry",
         "x509: certificate is valid for <host>, not <host>; retry"),
        # Decoding reveals more: the decoded form is used, even when it has
        # fewer replacements in all (three IPs become one URL when decoded).
        ("a%3A//10.0.0.1, then 10%2E0%2E0%2E3", "<url>, then <address>"),
        ("a%3A//10.0.0.1,10.0.0.2,10.0.0.4 then 10%2E0%2E0%2E3", "<url> then <address>"),
        # Credentials: JWT-sized, parentheses, a key name before them.
        ("Post u:" + "J" * 900 + "@bee:1633 x", "Post <address> x"),
        ("dial u:p(a)ss@rpc.example.com:8545", "dial <address>"),
        ("token=u:pass@x", "token=x"),
        ("a=1&token=u:pass@x", "a=1&token=x"),
        # Addresses glued to letters, "_" or non-ASCII text.
        ("_10.0.0.1", "_<address>"),
        ("ip10.0.0.1", "ip<address>"),
        ("é10.0.0.1", "é<address>"),
        ("日10.0.0.1", "日<address>"),
        ("ab.10.0.0.1", "ab.<address>"),
        ("ipfd00::1 down", "ip<address> down"),
        ("host.internal日", "<host>日"),
        ("日bee.internal:1633", "日<address>"),
        ("rpc.internal日", "<host>日"),
    ])
    def test_replaces(self, text, expected):
        assert without_addresses(text) == expected

    def test_configured_hyphenated_host_after_not(self):
        with patch.object(settings, "GNOSIS_RPC_URL", "https://gnosis-rpc.corp.example/v2/x"):
            assert without_addresses(
                "x509: certificate is valid for a.example, not gnosis-rpc.corp.example."
            ) == "x509: certificate is valid for <host>, not <host>."

    @pytest.mark.parametrize("text", [
        # Numbers glued to an address-like run are not addresses.
        "11.2.3.4.5",
        "build 1.2.3.4.5",
        # Go source positions under a module path, and public docs links.
        "panic at github.com/ethersphere/bee/v2/pkg/api.go:42",
        "see docs.ethswarm.org/api for details",
        "see pkg.go.dev/github.com/ethersphere/bee",
        "fork gitlab.com/a/b",
    ])
    def test_keeps(self, text):
        assert without_addresses(text) == text

    def test_public_host_is_replaced_when_configured(self):
        with patch.object(settings, "X402_BZZ_PRICE_FEED_URL", "https://docs.ethswarm.org/feed"):
            assert without_addresses("GET docs.ethswarm.org/api") == "GET <address>"

    def test_secret_rule(self):
        configured = {
            "GNOSIS_RPC_URL": ("https://admin:admin@rpc.example.net/Transaction/mainnet-v2"
                               "?network=gnosis-mainnet&project=0123abcdKEY4567"),
            "BASE_RPC_URL": "https://Ab12Cd34Ef56Gh78@base.example.net",
            "X402_BZZ_PRICE_FEED_URL": "https://Zx98Yw76Vu54:@feed.example.net/x",
            "SWARM_BEE_API_URL": "https://user:longpassword1@bee.example.net",
            "X402_FACILITATOR_URL": "   ",
        }
        with patch.multiple(settings, **configured):
            # Collected: a mixed key in the query, username-only keys, and a
            # password of 8 or more characters.
            assert without_addresses("bad key 0123abcdKEY4567") == "bad key <secret>"
            assert without_addresses("bad key Ab12Cd34Ef56Gh78") == "bad key <secret>"
            assert without_addresses("bad key Zx98Yw76Vu54") == "bad key <secret>"
            assert without_addresses("auth longpassword1 refused") == "auth <secret> refused"
            # Not collected: a short password, words and network names, and a
            # key of one letter case.
            for text in ("user admin rejected", "Transaction failed", "on mainnet-v2",
                         "network gnosis-mainnet", "invalid key abcdefghijklmnop"):
                assert without_addresses(text) == text

    def test_no_safe_cut_point(self):
        assert public_bee_text("x" * 300) is None
        assert public_bee_text("y" * 3000) is None
        assert public_bee_text("short word " + "y" * 3000) == "short word…"


# --------------------------------------------------------------------------- #
# Round 5: the safety gate, timestamps inside URLs, remaining scrubber gaps
# --------------------------------------------------------------------------- #
class TestRoundFive:

    @pytest.mark.parametrize("text, expected", [
        # A timestamp inside a URL or path no longer splits it.
        ('Get "https://rpc.example.org/v2/logs?from=2026-01-01T00:00:00Z&apikey=zz99YYxx77AA"',
         'Get "<url>"'),
        ("dial rpc.example.org/2026-01-01T00:00:00Z/abc123XYZ789", "dial <address>"),
        # What the first pass caught stays caught after decoding.
        ("10.0.0.1%41 bee%2Einternal", "<address>A <host>"),
        # An empty username.
        ("Post :pa55word@bee:1633 x", "Post <address> x"),
        # x509 names joined by "," without a space.
        ("x509: certificate is valid for a.example,b.example, not c.example",
         "x509: certificate is valid for <host>, not <host>"),
        ("x509: certificate is valid for a.example,b.example,not c.example",
         "x509: certificate is valid for <host>,not <host>"),
        # A configured secret glued to "-" or "_".
        ("id-0123abcdKEY4567_x", "id-<secret>_x"),
        # A host after a "." that follows punctuation (found by the property test).
        ("x509: .pkp.internal/v2/zT6yC5dQkvQv(", "x509: .<address>"),
    ])
    def test_scrubber_fixes(self, text, expected):
        assert without_addresses(text) == expected

    @pytest.mark.parametrize("text", [
        # Left to the gate: the scrubber keeps these, the gate hides them.
        "peer %C3%A910.0.0.1 gone",
        "x509: certificate is valid for " + ", ".join(f"h{i}.example" for i in range(40))
        + ", not z.example",
        "see github.com/ethersphere/bee.internal/y",
        "see github.com/x/rpc.internal:8545 y",
        # Refused by design: timestamps, key:number fields, source positions,
        # dotted words, and anything after "@", "%", "\\" or "?".
        "expires 2026-01-01T00:00:00Z ok",
        "want min:17",
        "panic at main.go:42",
        "e.g. this",
        "user@example",
        "load 50%25 done",
        "path c:\\x",
        "what?now",
        "bad ::1 address",
        "build 1.2.300.4",
    ])
    def test_gate_refuses(self, text):
        assert public_bee_text(text) is None

    @pytest.mark.parametrize("text", [
        "out of funds",
        "insufficient amount for 24h minimum validity",
        "batch not usable",
        "Bee is syncing",
        "insufficient funds",
        "not found",
        "bee/2.8.1 user agent",
        "version 2.8.1 is too old",
        "Post \"<url>\": dial tcp <address>: connect: connection refused",
        "is it ready? no",
        "100% done",
    ])
    def test_gate_passes(self, text):
        assert public_bee_text(text) == text

    @pytest.mark.parametrize("call, verb", [(_purchase, "purchase"), (_extend, "extension")])
    def test_refused_message_gives_the_status(self, call, verb):
        exc = _status_error("stamps/100/17", 400, method="POST",
                            json={"code": 400, "message": "panic at main.go:42"})
        assert call(exc).json()["detail"] == f"Bee rejected the stamp {verb}: HTTP 400"

    @pytest.mark.parametrize("call, verb", [(_purchase, "purchase"), (_extend, "extension")])
    def test_timestamp_in_url_does_not_leak(self, call, verb):
        message = ('Post "https://rpc.example.org/v2/logs?from=2026-01-01T00:00:00Z'
                   '&apikey=zz99YYxx77AA": EOF')
        exc = _status_error("stamps/100/17", 400, method="POST",
                            json={"code": 400, "message": message})
        detail = call(exc).json()["detail"]
        assert "zz99YYxx77AA" not in detail and "rpc.example.org" not in detail, detail

    def test_random_planted_addresses_never_shown(self):
        """Property test: whatever the scrubber misses, the gate stops."""
        import random
        rng = random.Random(549)
        letters = "abcdefghijklmnopqrstuvwxyz"
        mixed = letters + letters.upper() + "0123456789"
        glue = ["", " ", "<", ">", "@", "/", "(", ")", '"', "'", ",", ";", ":", "=", "?",
                "#", "%3A", "%2F", "日", "é", "_", "-", ".", "[", "]"]
        words = ["out", "of", "funds", "dial", "tcp", "failed", "Bee", "said", "lookup", "on",
                 "refused", "not", "valid", "for", "token", "2.8.1", "x509:", "certificate", "is"]
        configured = ["0123abcdKEY4567", "QUERYKEY123", "hunter22"]

        def label():
            return "".join(rng.choice(letters + "0123456789") for _ in range(rng.randint(3, 8)))

        for _ in range(400):
            ip = ".".join(str(rng.randint(1, 254)) for _ in range(4))
            host = label() + rng.choice([".internal", ".example.com", ".corp.example", ".svc",
                                         ".cluster.local", "." + label() + ".net"])
            password = "".join(rng.choice(mixed) for _ in range(12))
            port = str(rng.randint(1, 65535))
            planted = rng.choice([
                ip, f"{ip}:{port}", f"[fd00::{rng.randint(1, 9999):x}]:{port}", host,
                f"{host}:{port}", f"{host}/v2/{password}",
                f"{rng.choice(['http', 'https', 'wss'])}://{host}:{port}/x?key={password}",
                f"user:{password}@{host}:{port}", f"{rng.choice(configured)}",
                f"lookup {host} on {ip}:53", f"/dns4/{host}/tcp/{port}",
            ])
            parts = [rng.choice(words) for _ in range(rng.randint(0, 6))]
            parts.insert(rng.randint(0, len(parts)),
                         rng.choice(glue) + planted + rng.choice(glue))
            text = " ".join(parts)
            shown = public_bee_text(text)
            if shown is None:
                continue
            for secret in (ip, host, password, *configured):
                assert secret not in shown, (text, shown)
