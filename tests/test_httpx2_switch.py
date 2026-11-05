# tests/test_httpx2_switch.py
"""
The gateway's own HTTP client is httpx2, everywhere at once (#595).

httpx and httpx2 exception classes are unrelated: neither library's HTTPError
is a subclass of the other's. A module left on `except httpx.HTTPError` would
silently stop catching the shared client's errors, and in the paid purchase
path a timeout after the purchase was sent would read as "Bee answered"
(`stamps._outcome_unknown`). So the switch is all or nothing, and these tests
hold it:

- no module in app/ imports or names httpx or httpcore (encode's libraries,
  still installed for x402), except an explicit allow-list, empty today;
- the shared client, the Bee call gate's error and the classifiers of the
  payment paths are built on httpx2;
- errors of the real shared client, through the real swarm_api call, are
  classified as before.

x402 1.0.0 builds its own httpx client for facilitator calls; the gateway
catches those errors only as Exception, so x402 stays on httpx.
"""
import ast
import asyncio
import pathlib
import socket
from unittest.mock import patch

import httpcore2
import httpx
import httpx2
import pytest
import pytest_asyncio
from fastapi import HTTPException

from app.api.endpoints.stamps import _outcome_unknown
from app.core.config import settings
from app.services import bee_gate
from app.services import http_client as hc
from app.services import swarm_api
from app.services.bee_gate import BeeBusyError, bee_call_gate
from app.services.spend_budget import spend_certainly_did_not_happen
from tests.tools.bee_stub import BeeStub

APP_DIR = pathlib.Path(__file__).resolve().parent.parent / "app"

# Modules of app/ allowed to use encode's httpx or httpcore, with the reason.
# Empty: nothing in app/ needs them. A module that hands objects to, or takes
# them from, a library built on httpx (x402 builds its own client) may be added
# here, as {"app/x402/example.py": "why"}; then every except clause there that
# can see the shared client's errors must also name the httpx2 classes.
ALLOWED: dict = {}

FORBIDDEN_MODULES = ("httpx", "httpcore")


def _uses_of_encode_httpx(path: pathlib.Path):
    """(line, what) for every import or name of httpx/httpcore in one file."""
    tree = ast.parse(path.read_text(), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FORBIDDEN_MODULES:
                    found.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in FORBIDDEN_MODULES:
                found.append((node.lineno, f"from {node.module} import ..."))
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_MODULES:
            # Catches `except httpx.HTTPError` even when the import is hidden
            # behind an alias elsewhere or was forgotten.
            found.append((node.lineno, f"name {node.id}"))
    return found


class TestNoEncodeHttpxInApp:

    def test_no_module_in_app_uses_httpx_or_httpcore(self):
        offenders = []
        for path in sorted(APP_DIR.rglob("*.py")):
            rel = path.relative_to(APP_DIR.parent).as_posix()
            if rel in ALLOWED:
                continue
            offenders += [f"{rel}:{line}: {what}" for line, what in _uses_of_encode_httpx(path)]
        assert not offenders, (
            "app/ must use httpx2, not httpx/httpcore (#595); their exception classes are "
            "unrelated, so a leftover except clause silently stops matching:\n" + "\n".join(offenders))

    def test_the_scan_finds_what_it_looks_for(self, tmp_path):
        """The guard above is not vacuous: it sees every form a leftover can take."""
        sample = tmp_path / "sample.py"
        sample.write_text(
            "import httpx\n"
            "import httpcore._async\n"
            "from httpx import HTTPError\n"
            "def f(e):\n"
            "    return isinstance(e, httpx.TransportError)\n"
            "import httpx2\n"
            "x = httpx2.HTTPError\n")
        assert [what for _, what in _uses_of_encode_httpx(sample)] == [
            "import httpx", "import httpcore._async", "from httpx import ...", "name httpx"]

    def test_allow_list_names_existing_files(self):
        for rel in ALLOWED:
            assert (APP_DIR.parent / rel).is_file(), rel

    def test_the_two_libraries_do_not_share_exception_classes(self):
        """Why the switch must be complete: neither catches the other's errors."""
        assert not issubclass(httpx2.HTTPError, httpx.HTTPError)
        assert not issubclass(httpx.HTTPError, httpx2.HTTPError)


class TestBuiltOnHttpx2:

    def test_bee_busy_error_is_an_httpx2_transport_error(self):
        assert issubclass(BeeBusyError, httpx2.TransportError)
        assert not issubclass(BeeBusyError, httpx.HTTPError)

    def test_transport_wrappers_are_httpx2_classes(self):
        assert issubclass(hc.ObservedTransport, httpx2.AsyncBaseTransport)
        assert issubclass(hc._CountedStream, httpx2.AsyncByteStream)
        assert issubclass(bee_gate.GatedTransport, httpx2.AsyncBaseTransport)
        assert issubclass(bee_gate._ReleasingStream, httpx2.AsyncByteStream)

    @pytest.mark.asyncio
    async def test_shared_client_is_httpx2_on_an_httpcore2_pool(self):
        saved = {k: getattr(hc, k) for k in ("_client", "_transport")}
        await hc.init_client()
        try:
            assert isinstance(hc.get_client(), httpx2.AsyncClient)
            assert isinstance(hc._transport.inner, httpx2.AsyncHTTPTransport)
            assert isinstance(hc._transport.inner._pool, httpcore2.AsyncConnectionPool)
            assert hc.get_client().headers["user-agent"] == f"python-httpx2/{httpx2.__version__}"
        finally:
            await hc.close_client()
            for k, v in saved.items():
                setattr(hc, k, v)


# --------------------------------------------------------------------------
# The classifiers of the payment paths
# --------------------------------------------------------------------------

_REQ = httpx2.Request("POST", "http://bee.test/stamps/1000/17")


def _status(code):
    return httpx2.HTTPStatusError(str(code), request=_REQ, response=httpx2.Response(code, request=_REQ))


# (exception, _outcome_unknown, spend_certainly_did_not_happen)
CLASSIFIED = [
    pytest.param(httpx2.ConnectError("refused", request=_REQ), True, True, id="ConnectError"),
    pytest.param(httpx2.ConnectTimeout("", request=_REQ), True, True, id="ConnectTimeout"),
    pytest.param(httpx2.PoolTimeout("", request=_REQ), True, False, id="PoolTimeout"),
    pytest.param(httpx2.ReadTimeout("", request=_REQ), True, False, id="ReadTimeout"),
    pytest.param(httpx2.WriteTimeout("", request=_REQ), True, False, id="WriteTimeout"),
    pytest.param(httpx2.RemoteProtocolError("dropped", request=_REQ), True, False, id="RemoteProtocolError"),
    pytest.param(httpx2.ReadError("reset", request=_REQ), True, False, id="ReadError"),
    pytest.param(_status(400), False, True, id="400"),
    pytest.param(_status(429), False, True, id="429"),
    pytest.param(_status(500), False, False, id="500"),
    pytest.param(_status(502), True, False, id="502"),
    pytest.param(_status(503), False, False, id="503"),
    pytest.param(_status(504), True, False, id="504"),
    pytest.param(BeeBusyError("no Bee call slot free within 2 s (user)"), False, True, id="BeeBusyError"),
]


class TestClassifiers:

    @pytest.mark.parametrize("exc, unknown, not_spent", CLASSIFIED)
    def test_httpx2_errors_are_classified(self, exc, unknown, not_spent):
        assert _outcome_unknown(exc) is unknown
        assert spend_certainly_did_not_happen(exc) is not_spent

    def test_own_refusal_is_not_spent(self):
        assert spend_certainly_did_not_happen(HTTPException(status_code=400)) is True

    @pytest.mark.parametrize("exc", [
        httpx.ConnectError("refused"),
        httpx.ConnectTimeout(""),
        httpx.HTTPStatusError("400", request=httpx.Request("POST", "http://bee.test/"),
                              response=httpx.Response(400)),
    ], ids=["ConnectError", "ConnectTimeout", "400"])
    def test_a_foreign_httpx_error_never_releases_a_spend_hold(self, exc):
        """Fail closed: an error of the other library is not proof that nothing was spent."""
        assert spend_certainly_did_not_happen(exc) is False


# --------------------------------------------------------------------------
# Real errors of the shared client, through the real swarm_api call
# --------------------------------------------------------------------------

@pytest_asyncio.fixture
async def shared_on():
    """The shared client (with the Bee call gate) against a given Bee URL."""
    saved = {k: getattr(hc, k) for k in (
        "_client", "_transport", "_replace_lock", "_recovery_task", "_last_recovery_attempt")}
    patches = []

    async def start(base_url):
        patches.extend([
            patch.object(settings, "SWARM_BEE_API_URL", base_url),
            patch.object(settings, "BEE_CALL_GATE_ENABLED", True),
        ])
        for p in patches:
            p.start()
        bee_call_gate.reset()
        await hc.init_client()

    try:
        yield start
    finally:
        await hc.close_client()
        bee_call_gate.reset()
        for p in reversed(patches):
            p.stop()
        for k, v in saved.items():
            setattr(hc, k, v)


@pytest_asyncio.fixture
async def stub():
    s = await BeeStub().start()
    try:
        yield s
    finally:
        s.release.set()
        await s.stop()


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestRealErrors:

    @pytest.mark.asyncio
    async def test_bee_not_listening_is_a_connect_error_and_nothing_was_spent(self, shared_on):
        await shared_on(f"http://127.0.0.1:{_closed_port()}/")
        with pytest.raises(httpx2.HTTPError) as caught:
            await swarm_api.purchase_postage_stamp(1000, 17)
        assert isinstance(caught.value, httpx2.ConnectError), type(caught.value)
        # _outcome_unknown is conservative for every transport error; the spend
        # budget knows a refused connection sent nothing.
        assert _outcome_unknown(caught.value) is True
        assert spend_certainly_did_not_happen(caught.value) is True

    @pytest.mark.asyncio
    async def test_bee_dropping_the_purchase_is_an_unknown_outcome(self, shared_on, stub):
        stub.drop_prefixes.add("/stamps/")
        await shared_on(stub.base_url)
        with pytest.raises(httpx2.HTTPError) as caught:
            await swarm_api.purchase_postage_stamp(1000, 17)
        assert isinstance(caught.value, httpx2.TransportError), type(caught.value)
        assert _outcome_unknown(caught.value) is True
        assert spend_certainly_did_not_happen(caught.value) is False

    @pytest.mark.asyncio
    async def test_bee_refusing_the_purchase_is_its_own_answer(self, shared_on, stub):
        await shared_on(stub.base_url)  # the stub answers 404 for a purchase
        with pytest.raises(httpx2.HTTPStatusError) as caught:
            await swarm_api.purchase_postage_stamp(1000, 17)
        assert caught.value.response.status_code == 404
        assert _outcome_unknown(caught.value) is False
        assert spend_certainly_did_not_happen(caught.value) is True


# --------------------------------------------------------------------------
# Response bodies on httpx2 still release what they hold
# --------------------------------------------------------------------------

class TestBodiesRelease:
    """_CountedStream (in-flight count) and _ReleasingStream (gate slot) wrap
    httpx2's response stream; a body that is read, closed or cancelled must
    release both. Release happens in aclose(): httpx2/httpcore2 2.16 have no
    finalizer, so a stream dropped without aclose() releases nothing, as with
    httpx before #595."""

    @staticmethod
    def _released():
        return hc._transport.in_flight == 0 and sum(bee_call_gate.held.values()) == 0

    @pytest.mark.asyncio
    async def test_body_read_in_full(self, shared_on, stub):
        await shared_on(stub.base_url)
        response = await hc.get_client().get(str(settings.SWARM_BEE_API_URL) + "bzz/" + "ab" * 32)
        assert response.status_code == 200 and len(response.content) == 1024
        assert self._released()

    @pytest.mark.asyncio
    async def test_body_abandoned_mid_iteration_then_closed(self, shared_on, stub):
        """Iteration stopped early, then aclose(): the release depends on that
        aclose() (no finalizer), which `async with client.stream(...)` and
        client.get() both do."""
        await shared_on(stub.base_url)
        client = hc.get_client()
        request = client.build_request("GET", str(settings.SWARM_BEE_API_URL) + "bzz/" + "ab" * 32)
        response = await client.send(request, stream=True)
        assert hc._transport.in_flight == 1 and sum(bee_call_gate.held.values()) == 1
        async for _ in response.aiter_raw(16):
            break
        await response.aclose()
        assert self._released()

    @pytest.mark.asyncio
    async def test_request_cancelled_while_waiting_for_bee(self, shared_on, stub):
        await shared_on(stub.base_url)
        stub.release.clear()  # Bee holds every answer
        task = asyncio.ensure_future(hc.get_client().get(str(settings.SWARM_BEE_API_URL) + "wallet"))
        for _ in range(200):
            if stub.in_flight == 1:
                break
            await asyncio.sleep(0.01)
        assert stub.in_flight == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert self._released()
