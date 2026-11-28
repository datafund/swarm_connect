"""Collection round trip against a real Bee node (#700). Opt-in.

Uploads tars through the gateway's own POST /api/v1/data/manifest (in
process) to a real Bee, then downloads them back through the gateway: every
file by its path, the root with and without index.html, a directory index,
a missing path, and an error document.

Needs a Bee node (2.8.1 measured) with a usable postage batch; see
TEST_STRATEGY.md for a local one. Uploads are deferred, so a single node
with no peers works.

    RUN_BEE_COLLECTION_TEST=1 \\
    BEE_COLLECTION_TEST_URL=http://127.0.0.1:<port>/ \\
    BEE_COLLECTION_TEST_STAMP=<batch id> \\
    pytest tests/test_collection_bee_roundtrip.py -s
"""
import io
import os
import tarfile

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_BEE_COLLECTION_TEST") != "1",
    reason="real-Bee collection round trip; set RUN_BEE_COLLECTION_TEST=1 (see TEST_STRATEGY.md)",
)

FILES = {
    "index.html": b"<h1>root</h1>",
    "assets/a.txt": b"A file in a directory",
    "assets/deep/b.json": b'{"b": 1}',
    "docs/index.html": b"<h1>docs</h1>",
    "404.html": b"<h1>not here</h1>",
    "home.html": b"<h1>home</h1>",
    "space name.txt": b"spaced",
}


def _tar(files, prefix=""):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(prefix + name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def gateway(monkeypatch):
    from app.core.config import settings
    from app.main import app

    url = os.environ["BEE_COLLECTION_TEST_URL"]
    monkeypatch.setattr(settings, "SWARM_BEE_API_URL", url if url.endswith("/") else url + "/")
    monkeypatch.setattr(settings, "X402_ENABLED", False)
    # The context manager runs the app's startup, which opens the shared
    # client the Bee calls go through.
    with TestClient(app) as client:
        yield client


def _upload(gateway, tar_bytes, query=""):
    stamp = os.environ["BEE_COLLECTION_TEST_STAMP"]
    r = gateway.post(
        f"/api/v1/data/manifest?stamp_id={stamp}&deferred=true{query}",
        files={"file": ("c.tar", io.BytesIO(tar_bytes), "application/x-tar")},
    )
    assert r.status_code == 200, r.text
    return r.json()["reference"]


def test_collection_with_index_round_trip(gateway):
    ref = _upload(gateway, _tar(FILES))
    print(f"\nwith index.html: {ref}")
    for path, data in FILES.items():
        r = gateway.get(f"/api/v1/data/{ref}/{path}")
        assert r.status_code == 200, (path, r.status_code, r.text)
        assert r.content == data, path
        assert r.headers["content-disposition"].startswith("attachment;")
        print(f"  {path}: {r.status_code} {r.headers['content-type']}")

    root = gateway.get(f"/api/v1/data/{ref}")
    assert root.status_code == 200, root.text
    assert root.content == FILES["index.html"]

    docs = gateway.get(f"/api/v1/data/{ref}/docs/")
    assert docs.status_code == 200
    assert docs.content == FILES["docs/index.html"]

    missing = gateway.get(f"/api/v1/data/{ref}/no/such/file.txt")
    assert missing.status_code == 404
    assert missing.json()["code"] == "COLLECTION_PATH_NOT_FOUND"
    print(f"  root: 200, docs/: 200, missing: 404 {missing.json()['code']}")


def test_collection_without_index_round_trip(gateway):
    files = {k: v for k, v in FILES.items() if k != "index.html"}
    ref = _upload(gateway, _tar(files, prefix="./"))
    print(f"\nwithout index.html (./ names): {ref}")
    for path, data in files.items():
        r = gateway.get(f"/api/v1/data/{ref}/{path}")
        assert r.status_code == 200, (path, r.status_code, r.text)
        assert r.content == data, path

    root = gateway.get(f"/api/v1/data/{ref}")
    assert root.status_code == 404
    assert f"/api/v1/data/{ref}/<path>" in root.json()["detail"]
    print(f"  root: 404 {root.json()['detail']!r}")


def test_dot_slash_index_and_error_document(gateway):
    ref = _upload(gateway, _tar(FILES, prefix="./"), "&error_document=404.html")
    print(f"\n./index.html + error_document: {ref}")
    root = gateway.get(f"/api/v1/data/{ref}")
    assert root.status_code == 200
    assert root.content == FILES["index.html"]
    # Bee serves the error document, with 200, for a missing path.
    missing = gateway.get(f"/api/v1/data/{ref}/no/such/file.txt")
    assert missing.status_code == 200
    assert missing.content == FILES["404.html"]


def test_explicit_index_document(gateway):
    ref = _upload(gateway, _tar(FILES), "&index_document=home.html")
    root = gateway.get(f"/api/v1/data/{ref}")
    assert root.status_code == 200
    assert root.content == FILES["home.html"]


def test_nested_index_document_is_refused_before_bee(gateway):
    # Bee 2.8.1 answers 500 "could not store directory" to an index document
    # with a slash; the gateway refuses it first.
    stamp = os.environ["BEE_COLLECTION_TEST_STAMP"]
    r = gateway.post(
        f"/api/v1/data/manifest?stamp_id={stamp}&deferred=true&index_document=assets/a.txt",
        files={"file": ("c.tar", io.BytesIO(_tar(FILES)), "application/x-tar")},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "INDEX_DOCUMENT_INVALID"

