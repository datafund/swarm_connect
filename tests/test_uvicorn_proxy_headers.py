"""uvicorn must not rewrite the client address from forwarding headers (#697).

uvicorn's proxy-header handling is on by default. For a peer in
FORWARDED_ALLOW_IPS (127.0.0.1 and ::1 by default, widened through the
environment) it replaces the connection's client address with the
X-Forwarded-For value before the app runs, without checking it is an address.
app/core/client_ip treats that peer as unforgeable, so with uvicorn's handling
on, a local caller sending `X-Forwarded-For: evil-123` was keyed as `evil-123`
even with TRUSTED_PROXY_HOPS=0 (measured in review of #716). The gateway's start
commands turn it off so client_ip is the only reader of the header.
"""
import ast
import importlib
import json
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request

from app.core.client_ip import get_client_ip
from app.core.config import settings

REPO = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO / "Dockerfile"
RUN_PY = REPO / "run.py"


def _dockerfile_cmd():
    cmds = [line for line in DOCKERFILE.read_text().splitlines() if line.startswith("CMD ")]
    assert len(cmds) == 1, cmds
    return json.loads(cmds[0][len("CMD "):])


def _cli_params(argv):
    """The settings uvicorn's own command line parser makes of argv."""
    cli = importlib.import_module("uvicorn.main").main
    assert argv[0] == "uvicorn", argv
    return cli.make_context("uvicorn", list(argv[1:])).params


def test_the_image_starts_uvicorn_without_proxy_headers():
    params = _cli_params(_dockerfile_cmd())
    assert params["proxy_headers"] is False, \
        "the Dockerfile CMD must pass --no-proxy-headers (#697)"


def test_the_parser_reading_is_meaningful():
    """Without the flag the same parser says proxy headers are on, so the test
    above is checking the flag and not a default."""
    cmd = [a for a in _dockerfile_cmd() if a != "--no-proxy-headers"]
    assert _cli_params(cmd)["proxy_headers"] is True


def test_run_py_starts_uvicorn_without_proxy_headers():
    tree = ast.parse(RUN_PY.read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", None) == "run"
             and getattr(n.func.value, "id", None) == "uvicorn"]
    assert len(calls) == 1
    kw = {k.arg: k.value for k in calls[0].keywords if k.arg}
    assert "proxy_headers" in kw and isinstance(kw["proxy_headers"], ast.Constant) \
        and kw["proxy_headers"].value is False, "run.py must pass proxy_headers=False (#697)"


# --- probe: a real uvicorn on loopback ---------------------------------------

def _probe_app():
    app = FastAPI()

    @app.get("/who")
    async def who(request: Request):
        return {"ip": get_client_ip(request)}

    return app


def _serve_and_ask(proxy_headers, headers):
    config = uvicorn.Config(_probe_app(), host="127.0.0.1", port=0, log_level="warning",
                            proxy_headers=proxy_headers, lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if time.monotonic() > deadline or not thread.is_alive():
                pytest.fail("uvicorn did not start")
            time.sleep(0.02)
        port = server.servers[0].sockets[0].getsockname()[1]
        r = httpx.get(f"http://127.0.0.1:{port}/who", headers=headers, timeout=5)
        return r.json()["ip"]
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.mark.parametrize("hops", [0, 1])
def test_with_the_images_setting_a_loopback_forged_header_gives_the_peer(monkeypatch, hops):
    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", hops)
    proxy_headers = _cli_params(_dockerfile_cmd())["proxy_headers"]
    assert _serve_and_ask(proxy_headers, {"X-Forwarded-For": "evil-123"}) == "127.0.0.1"


def test_with_uvicorns_default_the_header_would_become_the_peer(monkeypatch):
    """The mechanism the flag exists for: with hops=0, which should ignore the
    header entirely, uvicorn's default still makes it the caller."""
    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 0)
    assert _serve_and_ask(True, {"X-Forwarded-For": "evil-123"}) == "evil-123"
