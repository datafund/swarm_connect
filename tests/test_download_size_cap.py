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


def _client(body: bytes, declare_length: bool = True, chunk: int = 64 * 1024):
    served = {"bytes": 0}

    async def stream():
        for i in range(0, len(body), chunk):
            served["bytes"] += len(body[i:i + chunk])
            yield body[i:i + chunk]

    def handler(request):
        headers = {"content-length": str(len(body))} if declare_length else {}
        return httpx.Response(200, headers=headers, content=stream())

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
