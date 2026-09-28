"""Downloads are capped and streamed, not buffered without limit (#353)."""
import asyncio
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.services import swarm_api

REF = "a" * 64


def _client(body: bytes, declare_length: bool = True, chunk: int = 64 * 1024, status: int = 200,
            delay: float = 0.0):
    served = {"bytes": 0, "accept_encoding": None}

    async def stream():
        for i in range(0, len(body), chunk):
            if delay:
                await asyncio.sleep(delay)
            served["bytes"] += len(body[i:i + chunk])
            yield body[i:i + chunk]

    def handler(request):
        served["accept_encoding"] = request.headers.get("accept-encoding")
        headers = {"content-length": str(len(body))} if declare_length else {}
        return httpx.Response(status, headers=headers, content=stream())

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), served


def _download(client):
    with patch("app.services.swarm_api.get_client", return_value=client):
        return asyncio.run(swarm_api.download_data_from_swarm(REF))


def test_content_under_the_cap_is_returned(monkeypatch):
    monkeypatch.setattr(settings, "MAX_DOWNLOAD_SIZE_MB", 1)
    client, _ = _client(b"x" * 1000)
    assert _download(client) == b"x" * 1000


def test_declared_oversize_is_refused_without_reading_the_body(monkeypatch):
    monkeypatch.setattr(settings, "MAX_DOWNLOAD_SIZE_MB", 1)
    client, served = _client(b"x" * (3 * 1024 * 1024))
    with pytest.raises(swarm_api.DownloadTooLargeError):
        _download(client)
    assert served["bytes"] < 1024 * 1024


def test_undeclared_oversize_is_cut_off_while_streaming(monkeypatch):
    monkeypatch.setattr(settings, "MAX_DOWNLOAD_SIZE_MB", 1)
    client, served = _client(b"x" * (3 * 1024 * 1024), declare_length=False)
    with pytest.raises(swarm_api.DownloadTooLargeError):
        _download(client)
    assert served["bytes"] <= 1024 * 1024 + 64 * 1024


@pytest.mark.parametrize("path", [f"/api/v1/data/{REF}", f"/api/v1/data/{REF}/json"])
def test_endpoints_answer_413(path):
    async def too_big(ref):
        raise swarm_api.DownloadTooLargeError(25 * 1024 * 1024)

    with patch("app.api.endpoints.data.download_data_from_swarm", new=too_big):
        r = TestClient(app).get(path)
    assert r.status_code == 413
    assert r.json()["detail"]["code"] == "DOWNLOAD_TOO_LARGE"


def test_content_exactly_at_the_cap_is_returned(monkeypatch):
    monkeypatch.setattr(settings, "MAX_DOWNLOAD_SIZE_MB", 1)
    client, _ = _client(b"x" * (1024 * 1024), declare_length=False)
    assert len(_download(client)) == 1024 * 1024


def test_uncompressed_transfer_is_requested(monkeypatch):
    client, served = _client(b"x")
    _download(client)
    assert served["accept_encoding"] == "identity"


def test_missing_reference_is_not_found():
    client, _ = _client(b"nope", status=404)
    with pytest.raises(FileNotFoundError):
        _download(client)


def test_slow_transfer_hits_the_total_deadline(monkeypatch):
    monkeypatch.setattr(settings, "DOWNLOAD_TIMEOUT_SECONDS", 1)
    client, _ = _client(b"x" * (40 * 1024), chunk=1024, delay=0.05, declare_length=False)
    with pytest.raises(asyncio.TimeoutError):
        _download(client)


def test_endpoint_answers_504_on_the_deadline():
    async def slow(ref):
        raise asyncio.TimeoutError()

    with patch("app.api.endpoints.data.download_data_from_swarm", new=slow):
        assert TestClient(app).get(f"/api/v1/data/{REF}").status_code == 504


def test_large_json_is_labelled_without_a_full_parse():
    from app.api.endpoints.data import _detect_content_type_and_filename
    big = b"[" + b"1," * (2 * 1024 * 1024) + b"1]"
    with patch("app.api.endpoints.data.json.loads", side_effect=AssertionError("parsed")):
        content_type, name = _detect_content_type_and_filename(big, REF)
    assert content_type == "application/json"
