# tests/test_chunks_upload.py
"""
Tests for stamped-chunk forwarding: service + endpoint + toggle (issue #219).
"""
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app

VALID_STAMP = "a" * 226  # 113-byte marshaled stamp, hex-encoded


@pytest.fixture
def client():
    return TestClient(app)


def _enabled_settings(max_bytes=4104):
    # X402 off: these tests cover pure forwarding without bandwidth billing
    # (billing/credit behavior is covered in test_chunks_billing.py).
    ms = MagicMock()
    ms.CHUNK_UPLOAD_ENABLED = True
    ms.CHUNK_UPLOAD_MAX_BYTES_PER_REQUEST = max_bytes
    ms.X402_ENABLED = False
    return ms


# --------------------------------------------------------------------------- #
# Service layer
# --------------------------------------------------------------------------- #
class TestUploadChunkService:
    def _mock_client(self, reference="ref123"):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={"reference": reference})
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=resp)
        return mock_client

    @pytest.mark.asyncio
    async def test_forwards_stamp_header_and_no_batch_id(self):
        from app.services.swarm_api import upload_chunk_to_swarm

        mock_client = self._mock_client("abc")
        with patch("app.services.swarm_api.get_client", return_value=mock_client):
            ref = await upload_chunk_to_swarm(b"chunkdata", VALID_STAMP, deferred=False)

        assert ref == "abc"
        _, kwargs = mock_client.post.call_args
        headers = kwargs["headers"]
        assert headers["Swarm-Postage-Stamp"] == VALID_STAMP
        assert "Swarm-Postage-Batch-Id" not in headers
        assert headers["Swarm-Deferred-Upload"] == "false"
        assert kwargs["content"] == b"chunkdata"

    @pytest.mark.asyncio
    async def test_deferred_maps_to_header(self):
        from app.services.swarm_api import upload_chunk_to_swarm

        mock_client = self._mock_client()
        with patch("app.services.swarm_api.get_client", return_value=mock_client):
            await upload_chunk_to_swarm(b"x", VALID_STAMP, deferred=True)

        _, kwargs = mock_client.post.call_args
        assert kwargs["headers"]["Swarm-Deferred-Upload"] == "true"

    @pytest.mark.asyncio
    async def test_http_error_propagates(self):
        from app.services.swarm_api import upload_chunk_to_swarm

        mock_client = MagicMock()
        mock_client.post = AsyncMock(side_effect=httpx.HTTPError("boom"))
        with patch("app.services.swarm_api.get_client", return_value=mock_client):
            with pytest.raises(httpx.HTTPError):
                await upload_chunk_to_swarm(b"x", VALID_STAMP)

    @pytest.mark.asyncio
    async def test_missing_reference_raises_value_error(self):
        from app.services.swarm_api import upload_chunk_to_swarm

        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={})
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=resp)
        with patch("app.services.swarm_api.get_client", return_value=mock_client):
            with pytest.raises(ValueError):
                await upload_chunk_to_swarm(b"x", VALID_STAMP)


# --------------------------------------------------------------------------- #
# Endpoint
# --------------------------------------------------------------------------- #
class TestUploadChunkEndpoint:
    def test_happy_path_returns_reference(self, client):
        with patch("app.api.endpoints.chunks.settings", _enabled_settings()):
            with patch("app.api.endpoints.chunks.upload_chunk_to_swarm",
                       new=AsyncMock(return_value="ref123")):
                r = client.post(
                    "/api/v1/chunks/",
                    content=b"hello-chunk",
                    headers={"Swarm-Postage-Stamp": VALID_STAMP},
                )
        assert r.status_code == 201
        assert r.json()["reference"] == "ref123"

    def test_deferred_query_passed_through(self, client):
        mock_up = AsyncMock(return_value="ref")
        with patch("app.api.endpoints.chunks.settings", _enabled_settings()):
            with patch("app.api.endpoints.chunks.upload_chunk_to_swarm", new=mock_up):
                r = client.post(
                    "/api/v1/chunks/?deferred=true",
                    content=b"data",
                    headers={"Swarm-Postage-Stamp": VALID_STAMP},
                )
        assert r.status_code == 201
        assert mock_up.call_args.kwargs["deferred"] is True

    def test_disabled_returns_404(self, client):
        ms = MagicMock()
        ms.CHUNK_UPLOAD_ENABLED = False
        with patch("app.api.endpoints.chunks.settings", ms):
            r = client.post(
                "/api/v1/chunks/",
                content=b"data",
                headers={"Swarm-Postage-Stamp": VALID_STAMP},
            )
        assert r.status_code == 404

    def test_missing_stamp_header_400(self, client):
        with patch("app.api.endpoints.chunks.settings", _enabled_settings()):
            r = client.post("/api/v1/chunks/", content=b"data")
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "MISSING_STAMP"

    def test_invalid_stamp_400(self, client):
        with patch("app.api.endpoints.chunks.settings", _enabled_settings()):
            r = client.post(
                "/api/v1/chunks/",
                content=b"data",
                headers={"Swarm-Postage-Stamp": "not-hex-and-too-short"},
            )
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "INVALID_STAMP"

    def test_empty_body_400(self, client):
        with patch("app.api.endpoints.chunks.settings", _enabled_settings()):
            r = client.post(
                "/api/v1/chunks/",
                content=b"",
                headers={"Swarm-Postage-Stamp": VALID_STAMP},
            )
        assert r.status_code == 400
        assert r.json()["detail"]["code"] == "EMPTY_CHUNK"

    def test_oversize_body_413(self, client):
        with patch("app.api.endpoints.chunks.settings", _enabled_settings(max_bytes=10)):
            r = client.post(
                "/api/v1/chunks/",
                content=b"x" * 20,
                headers={"Swarm-Postage-Stamp": VALID_STAMP},
            )
        assert r.status_code == 413
        assert r.json()["detail"]["code"] == "CHUNK_TOO_LARGE"

    def test_bee_error_returns_502(self, client):
        with patch("app.api.endpoints.chunks.settings", _enabled_settings()):
            with patch("app.api.endpoints.chunks.upload_chunk_to_swarm",
                       new=AsyncMock(side_effect=httpx.HTTPError("bee down"))):
                r = client.post(
                    "/api/v1/chunks/",
                    content=b"data",
                    headers={"Swarm-Postage-Stamp": VALID_STAMP},
                )
        assert r.status_code == 502
