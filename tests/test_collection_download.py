"""Collections uploaded with POST /data/manifest download through the gateway (#700).

Bee is a stub (httpx2.MockTransport behind swarm_api.get_client) that answers
the way Bee 2.8.1 was measured to answer: files by path, 404 `path address not
found` for a missing path in a collection, 404 `address not found or incorrect`
at the root of a collection without an index document, 404 `Not Found` for a
path under a reference that is not a collection. The real-Bee round trip is
tests/test_collection_bee_roundtrip.py (opt-in).
"""
import io
import tarfile
from unittest.mock import patch
from urllib.parse import quote, unquote

import httpx2
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.services import swarm_api
from app.services.metrics import downloads_total
from app.services.swarm_api import TarSummary, inspect_tar

REF = "ab" * 32
STAMP = "cd" * 32
client = TestClient(app)


def _tar(entries):
    """entries: (name, bytes) for a file, (name, None) for a directory,
    (name, "->target") for a symlink."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            if data is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            elif isinstance(data, str):
                info.type = tarfile.SYMTYPE
                info.linkname = data[2:]
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class StubBee:
    """Serves `files` ({path: (body, content_type)}) of one collection REF."""

    def __init__(self, files=None, index=None, status=None, disposition=None):
        self.files = files or {}
        self.index = index
        self.status = status
        # path -> raw Content-Disposition bytes, instead of Bee's usual one
        self.disposition = disposition or {}
        self.requests = []

    def handler(self, request):
        self.requests.append(request)
        raw = request.url.raw_path.decode()
        if not raw.startswith("/bzz/"):
            return httpx2.Response(404, json={"code": 404, "message": "Not Found"})
        if self.status is not None:
            return httpx2.Response(self.status, json={"code": self.status, "message": "boom"})
        rest = raw[len("/bzz/"):]
        ref, _, path = rest.partition("/")
        if ref != REF:
            return httpx2.Response(404, json={"code": 404, "message": "Not Found"})
        path = unquote(path)
        if path == "" or path.endswith("/"):
            if self.index is None:
                return httpx2.Response(404, json={"code": 404, "message": "address not found or incorrect"})
            path += self.index
        if path not in self.files:
            return httpx2.Response(404, json={"code": 404, "message": "path address not found"})
        body, ctype = self.files[path]
        # Bee sends the file name as raw UTF-8 (measured).
        name = path.rsplit("/", 1)[-1]
        headers = [(b"content-disposition",
                    self.disposition.get(path, f'inline; filename="{name}"'.encode("utf-8")))]
        if ctype is not None:
            headers.append((b"content-type", ctype.encode()))
        return httpx2.Response(200, content=body, headers=headers)

    def client(self):
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self.handler))


def _get(bee, url):
    with patch("app.services.swarm_api.get_client", return_value=bee.client()):
        return client.get(url)


def _raw_get(bee, raw_path):
    """GET a path exactly as given, through the ASGI app.

    An HTTP client (httpx, a browser) removes '.' and '..' segments before it
    sends a URL; curl --path-as-is or a raw socket does not, so the gateway
    must refuse them itself. The server decodes the path once, as uvicorn does.
    Returns (status, json body or None).
    """
    import asyncio
    import json
    from urllib.parse import unquote as _unquote

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "root_path": "", "query_string": b"",
        "path": _unquote(raw_path), "raw_path": raw_path.encode(),
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 50000), "server": ("testserver", 80),
    }
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    with patch("app.services.swarm_api.get_client", return_value=bee.client()):
        asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    try:
        return status, json.loads(body)
    except ValueError:
        return status, None


FILES = {
    "index.html": (b"<h1>root</h1>", "text/html; charset=utf-8"),
    "assets/a.txt": (b"A", "text/plain; charset=utf-8"),
    "deep/er/still/x.json": (b"{}", "application/json"),
    "sub/index.html": (b"<h1>sub</h1>", "text/html; charset=utf-8"),
    "weird name #1%.txt": (b"W", "text/plain; charset=utf-8"),
    "jé.txt": (b"J", "text/plain; charset=utf-8"),
    "json": (b"JSONFILE", ""),
    "sub/json": (b"SUBJSON", ""),
    "logo.svg": (b"<svg onload='alert(1)'/>", "image/svg+xml"),
}


class TestPathDownload:
    def test_a_file_downloads_by_its_path(self):
        bee = StubBee(FILES)
        r = _get(bee, f"/api/v1/data/{REF}/assets/a.txt")
        assert r.status_code == 200, r.text
        assert r.content == b"A"
        assert r.headers["content-type"] == "text/plain; charset=utf-8"
        assert r.headers["content-disposition"] == 'attachment; filename="a.txt"'
        assert r.headers["x-swarm-reference"] == REF
        assert bee.requests[0].url.raw_path.decode() == f"/bzz/{REF}/assets/a.txt"

    def test_nested_path(self):
        bee = StubBee(FILES)
        r = _get(bee, f"/api/v1/data/{REF}/deep/er/still/x.json")
        assert r.status_code == 200
        assert r.content == b"{}"
        assert r.headers["content-type"] == "application/json"

    def test_each_segment_is_percent_encoded_towards_bee(self):
        bee = StubBee(FILES)
        r = _get(bee, f"/api/v1/data/{REF}/weird%20name%20%231%25.txt")
        assert r.status_code == 200, r.text
        assert r.content == b"W"
        assert bee.requests[0].url.raw_path.decode() == f"/bzz/{REF}/weird%20name%20%231%25.txt"

    def test_non_ascii_name_gets_an_rfc6266_filename(self):
        bee = StubBee(FILES)
        r = _get(bee, f"/api/v1/data/{REF}/j%C3%A9.txt")
        assert r.status_code == 200
        assert bee.requests[0].url.raw_path.decode() == f"/bzz/{REF}/j%C3%A9.txt"
        assert r.headers["content-disposition"] == "attachment; filename=\"j_.txt\"; filename*=UTF-8''j%C3%A9.txt"

    def test_quote_in_a_name_is_replaced_in_the_fallback(self):
        bee = StubBee({'a"b.txt': (b"Q", "text/plain")})
        r = _get(bee, f"/api/v1/data/{REF}/a%22b.txt")
        assert r.status_code == 200, r.text
        assert r.headers["content-disposition"] == "attachment; filename=\"a_b.txt\"; filename*=UTF-8''a%22b.txt"

    def test_quote_and_backslash_are_replaced_in_the_fallback(self):
        # A backslash cannot come through the route (PATH_INVALID), but Bee's
        # own file name can carry one.
        from app.api.endpoints.data import _attachment
        assert _attachment('a"b\\c.txt') == "attachment; filename=\"a_b_c.txt\"; filename*=UTF-8''a%22b%5Cc.txt"

    @pytest.mark.parametrize("bee_name", [
        b"x/y.txt", b"x\x01y.txt", b"x\x7fy.txt", b"",
        b"..\\..\\evil.exe", b"x\\y.txt", b".", b"..",
        "evil\u202etxt.exe".encode(), "a\u200fb.txt".encode(), "a\ufeffb.txt".encode(),
        "a\u2028b.txt".encode(), "a\u2029b.txt".encode(), "a\x85b.txt".encode(),
        b" a.txt", b"a.txt ", b"a.txt.", b"a.txt..",
    ])
    def test_an_unusable_bee_name_falls_back_to_the_path(self, bee_name):
        bee = StubBee(FILES, disposition={"assets/a.txt": b'inline; filename="' + bee_name + b'"'})
        r = _get(bee, f"/api/v1/data/{REF}/assets/a.txt")
        assert r.status_code == 200
        assert r.headers["content-disposition"] == 'attachment; filename="a.txt"'

    @pytest.mark.parametrize("name", [".env", "a b.txt", "\u00edndex.html", "a..b.txt"])
    def test_a_safe_bee_name_is_kept(self, name):
        bee = StubBee(FILES, disposition={"assets/a.txt": f'inline; filename="{name}"'.encode()})
        r = _get(bee, f"/api/v1/data/{REF}/assets/a.txt")
        assert r.status_code == 200
        assert "filename*=UTF-8''" + quote(name, safe="") in r.headers["content-disposition"] \
            or r.headers["content-disposition"] == f'attachment; filename="{name}"'

    @pytest.mark.parametrize("segment,expected", [
        ("evil%E2%80%AEtxt.exe", "evil_txt.exe"),        # U+202E in a valid file name
        ("a%E2%80%8Fb.txt", "a_b.txt"),                   # U+200F
        ("a.txt%20", "a.txt"),                            # trailing space
        ("a.txt.", "a.txt"),                              # trailing dot
        ("%E2%80%AE", "_"),                               # only the override
        ("..%20", "download"),                            # nothing left after the trim
    ])
    def test_the_path_fallback_is_made_safe_too(self, segment, expected):
        raw = unquote(segment)
        # Bee names the file the same unsafe way, so only the fallback decides.
        bee = StubBee({raw: (b"x", "text/plain")},
                      disposition={raw: f'inline; filename="{raw}"'.encode()})
        r = _get(bee, f"/api/v1/data/{REF}/{segment}")
        assert r.status_code == 200, r.text
        cd = r.headers["content-disposition"]
        assert cd.startswith(f'attachment; filename="{expected}"'), cd
        assert "%E2%80%AE" not in cd and "%E2%80%8F" not in cd

    def test_bee_utf8_name_is_encoded_exactly(self):
        # A directory request: Bee names the index document it served.
        bee = StubBee({"sub/\u00edndex.html": (b"i", "text/html")}, index="\u00edndex.html")
        r = _get(bee, f"/api/v1/data/{REF}/sub/")
        assert r.status_code == 200
        assert r.headers["content-disposition"] == "attachment; filename=\"_ndex.html\"; filename*=UTF-8''%C3%ADndex.html"

    def test_bee_name_that_looks_like_mojibake_is_kept(self):
        # A file really named "\u00c3\u00a9.txt": httpx2 decodes Bee's UTF-8 bytes, and
        # decoding them a second time would turn it into "\u00e9.txt".
        bee = StubBee({"sub/\u00c3\u00a9.txt": (b"m", "text/plain")}, index="\u00c3\u00a9.txt")
        r = _get(bee, f"/api/v1/data/{REF}/sub/")
        assert r.status_code == 200
        assert r.headers["content-disposition"].endswith("filename*=UTF-8''%C3%83%C2%A9.txt")

    def test_uppercase_reference_is_sent_lowercase(self):
        bee = StubBee(FILES)
        r = _get(bee, f"/api/v1/data/{REF.upper()}/assets/a.txt")
        assert r.status_code == 200
        assert bee.requests[0].url.raw_path.decode() == f"/bzz/{REF}/assets/a.txt"

    def test_html_is_an_attachment_that_cannot_run(self):
        bee = StubBee(FILES)
        r = _get(bee, f"/api/v1/data/{REF}/index.html")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        assert r.headers["content-disposition"].startswith("attachment;")
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["content-security-policy"] == "sandbox"

    def test_svg_too(self):
        bee = StubBee(FILES)
        r = _get(bee, f"/api/v1/data/{REF}/logo.svg")
        assert r.headers["content-type"] == "image/svg+xml"
        assert r.headers["content-disposition"] == 'attachment; filename="logo.svg"'
        assert r.headers["content-security-policy"] == "sandbox"

    @pytest.mark.parametrize("bee_type", ["", None, "nonsense", "text/html x y", "text/"])
    def test_missing_or_malformed_content_type_falls_back(self, bee_type):
        bee = StubBee({"f": (b"x", bee_type)})
        r = _get(bee, f"/api/v1/data/{REF}/f")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/octet-stream"

    def test_trailing_slash_serves_the_directory_index(self):
        # Bee 2.8.1 applies the index document to every directory (measured).
        bee = StubBee(FILES, index="index.html")
        r = _get(bee, f"/api/v1/data/{REF}/sub/")
        assert r.status_code == 200
        assert r.content == b"<h1>sub</h1>"
        assert bee.requests[0].url.raw_path.decode() == f"/bzz/{REF}/sub/"
        # Named after the file Bee served, not the directory.
        assert r.headers["content-disposition"] == 'attachment; filename="index.html"'

    def test_empty_path_is_the_root_download(self):
        bee = StubBee(FILES, index="index.html")
        r = _get(bee, f"/api/v1/data/{REF}/")
        assert r.status_code == 200
        assert r.content == b"<h1>root</h1>"
        assert bee.requests[0].url.raw_path.decode() == f"/bzz/{REF}"


class TestJsonShadowing:
    def test_json_at_the_root_is_the_metadata_route(self):
        bee = StubBee(FILES, index="index.html")
        r = _get(bee, f"/api/v1/data/{REF}/json")
        assert r.status_code == 200
        body = r.json()
        assert set(body) >= {"data", "content_type", "size", "reference"}
        # It fetched the collection root, not the file named json.
        assert bee.requests[0].url.raw_path.decode() == f"/bzz/{REF}"

    def test_json_in_a_directory_is_a_file(self):
        bee = StubBee(FILES)
        r = _get(bee, f"/api/v1/data/{REF}/sub/json")
        assert r.status_code == 200
        assert r.content == b"SUBJSON"

    def test_a_path_under_json_is_a_file_path(self):
        bee = StubBee({"json/x": (b"X", "text/plain")})
        r = _get(bee, f"/api/v1/data/{REF}/json/x")
        assert r.status_code == 200
        assert r.content == b"X"


class TestPathValidation:
    @pytest.mark.parametrize("path", [
        "..", "a/..", "../etc/passwd", "a/../b", ".", "a/./b", "a/.",
        "%2e%2e", "a/%2e%2e/b", "%2E", ".%2e/x",     # decoded once by the server
        "%252e%252e", "a/%252e%252e/b", "%252e",     # decoded once more by us
        "a//b", "/a", "a\\b", "%5C", "%255C", "a%00b", "a%01b", "a%7fb", "a%2500b",
        "a/%252Fb", "a//", "x" * 1100,
    ])
    def test_refused_before_bee(self, path):
        bee = StubBee(FILES)
        before = downloads_total.labels(status="error")._value.get()
        status, body = _raw_get(bee, f"/api/v1/data/{REF}/{path}")
        assert status == 400, (path, status, body)
        assert body["code"] == "PATH_INVALID"
        assert bee.requests == []
        assert downloads_total.labels(status="error")._value.get() == before + 1

    @pytest.mark.parametrize("path", ["a%0ab", "a%0db", "%0a", "a/%0d%0a", "%0a/b", "a%0a"])
    def test_cr_and_lf_are_path_invalid(self, path):
        # The route's convertor matches newlines (Starlette's `path` stops at
        # them), so CR and LF reach _path_segments and get PATH_INVALID.
        bee = StubBee(FILES, index="index.html")
        status, body = _raw_get(bee, f"/api/v1/data/{REF}/{path}")
        assert status == 400, (path, status, body)
        assert body["code"] == "PATH_INVALID"
        assert bee.requests == []

    def test_a_single_encoded_slash_is_a_separator(self):
        # uvicorn decodes %2F before routing, so a%2Fb is the path a/b: it stays
        # inside the reference and reaches Bee as two segments.
        bee = StubBee({"a/b": (b"AB", "text/plain")})
        status, _ = _raw_get(bee, f"/api/v1/data/{REF}/a%2Fb")
        assert status == 200
        assert [q.url.raw_path.decode() for q in bee.requests] == [f"/bzz/{REF}/a/b"]

    def test_a_normal_path_passes_the_raw_route_too(self):
        bee = StubBee(FILES)
        status, _ = _raw_get(bee, f"/api/v1/data/{REF}/assets/a.txt")
        assert status == 200

    def test_bad_reference_is_422(self):
        bee = StubBee(FILES)
        r = _get(bee, "/api/v1/data/nothex/assets/a.txt")
        assert r.status_code == 422
        assert bee.requests == []


class TestErrors:
    def test_missing_path_in_a_collection(self):
        bee = StubBee(FILES)
        r = _get(bee, f"/api/v1/data/{REF}/missing.txt")
        assert r.status_code == 404
        body = r.json()
        assert body["code"] == "COLLECTION_PATH_NOT_FOUND"
        assert body["detail"]["path"] == "missing.txt"
        assert body["detail"]["reference"] == REF

    def test_other_bee_404_is_neutral(self):
        bee = StubBee(FILES)
        r = _get(bee, f"/api/v1/data/{'ef' * 32}/a.txt")
        assert r.status_code == 404
        assert r.json()["code"] == "DATA_NOT_FOUND"
        assert "may not exist, or may not be a collection" in r.json()["message"]

    def test_directory_without_index(self):
        bee = StubBee(FILES)
        r = _get(bee, f"/api/v1/data/{REF}/sub/")
        assert r.status_code == 404
        assert r.json()["code"] == "DATA_NOT_FOUND"

    def test_bee_failure_is_502(self):
        bee = StubBee(FILES, status=500)
        r = _get(bee, f"/api/v1/data/{REF}/assets/a.txt")
        assert r.status_code == 502

    def test_too_large_uses_the_download_limit(self, monkeypatch):
        monkeypatch.setattr(settings, "MAX_DOWNLOAD_SIZE_MB", 1)
        bee = StubBee({"big.bin": (b"x" * (1024 * 1024 + 1), "application/octet-stream")})
        r = _get(bee, f"/api/v1/data/{REF}/big.bin")
        assert r.status_code == 413
        assert r.json()["code"] == "DOWNLOAD_TOO_LARGE"

    def test_timeout_uses_the_download_deadline(self):
        import asyncio

        async def slow(*a, **k):
            raise asyncio.TimeoutError()

        with patch("app.api.endpoints.data.download_collection_file", new=slow):
            r = client.get(f"/api/v1/data/{REF}/assets/a.txt")
        assert r.status_code == 504

    def test_download_limit_and_deadline_are_shared(self):
        # download_collection_file goes through the same _fetch_bzz as
        # download_data_from_swarm, so the limit and deadline cannot drift.
        import inspect as pyinspect
        assert "_fetch_bzz(" in pyinspect.getsource(swarm_api.download_collection_file)
        assert "_fetch_bzz(" in pyinspect.getsource(swarm_api.download_data_from_swarm)


class TestRootDownloadHeaders:
    def test_root_download_cannot_run_as_a_page(self):
        bee = StubBee(FILES, index="index.html")
        r = _get(bee, f"/api/v1/data/{REF}")
        assert r.status_code == 200
        assert r.content == b"<h1>root</h1>"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["content-security-policy"] == "sandbox"
        assert r.headers["content-disposition"] == f'attachment; filename="text-{REF[:8]}.txt"'


class TestRootDownloadHint:
    def test_root_404_points_at_the_path_route_without_guessing(self):
        bee = StubBee(FILES)       # no index document
        for url in (f"/api/v1/data/{REF}", f"/api/v1/data/{REF}/json"):
            r = _get(bee, url)
            assert r.status_code == 404
            detail = r.json()["detail"]
            assert isinstance(detail, str)
            assert detail.startswith(f"Data not found for reference {REF}")
            assert f"/api/v1/data/{REF}/<path>" in detail
            assert "If it is a collection" in detail


class TestFreeAndCounted:
    def test_not_a_paid_route(self):
        from app.x402.middleware import is_protected_endpoint
        assert is_protected_endpoint("GET", f"/api/v1/data/{REF}/assets/a.txt") is False

    def test_free_with_x402_enabled(self, monkeypatch):
        monkeypatch.setattr(settings, "X402_ENABLED", True)
        bee = StubBee(FILES)
        r = _get(bee, f"/api/v1/data/{REF}/assets/a.txt")
        assert r.status_code == 200

    def test_success_is_counted(self):
        before = downloads_total.labels(status="success")._value.get()
        _get(StubBee(FILES), f"/api/v1/data/{REF}/assets/a.txt")
        assert downloads_total.labels(status="success")._value.get() == before + 1

    def test_bee_call_is_a_transfer(self):
        from app.services.bee_gate import is_transfer
        url = str(settings.SWARM_BEE_API_URL).rstrip("/") + f"/bzz/{REF}/assets/a.txt"
        assert is_transfer(httpx2.Request("GET", url))


class TestInspectTarFinds:
    def test_root_index_variants(self):
        assert inspect_tar(_tar([("index.html", b"x")]), wanted={"index.html"}) == TarSummary(1, frozenset({"index.html"}))
        assert inspect_tar(_tar([("./", None), ("./index.html", b"x")]), wanted={"index.html"}).found == {"index.html"}

    @pytest.mark.parametrize("entries", [
        [("sub/index.html", b"x")],
        [("index.html/", None), ("index.html/a", b"x")],
        [("a", b"x"), ("index.html", "->a")],
        [("/index.html", b"x")],      # Bee serves no path for a leading-slash entry (measured)
    ])
    def test_not_a_root_index(self, entries):
        assert inspect_tar(_tar(entries), wanted={"index.html"}).found == frozenset()

    def test_only_wanted_names_are_reported(self):
        s = inspect_tar(_tar([("a.txt", b"1"), ("e/404.html", b"2")]), wanted={"e/404.html", "zz"})
        assert s == TarSummary(2, frozenset({"e/404.html"}))
        assert inspect_tar(_tar([("a.txt", b"1")])) == TarSummary(1, frozenset())


def _upload(bee, tar_bytes, query=""):
    with patch("app.services.swarm_api.get_client", return_value=bee.client()), \
         patch("app.api.endpoints.data.get_batch_expiry", return_value=None):
        return client.post(
            f"/api/v1/data/manifest?stamp_id={STAMP}{query}",
            files={"file": ("c.tar", io.BytesIO(tar_bytes), "application/x-tar")},
        )


class UploadBee(StubBee):
    def handler(self, request):
        if request.method == "POST" and request.url.path.endswith("/bzz"):
            self.requests.append(request)
            return httpx2.Response(201, json={"reference": REF})
        return httpx2.Response(404, json={"code": 404, "message": "Not Found"})


class TestIndexDocumentOnUpload:
    def _headers(self, bee):
        posts = [q for q in bee.requests if q.method == "POST"]
        assert len(posts) == 1
        return posts[0].headers

    def test_root_index_html_sets_the_index_document(self):
        bee = UploadBee()
        r = _upload(bee, _tar([("index.html", b"<h1>x</h1>"), ("a.txt", b"a")]))
        assert r.status_code == 200, r.text
        h = self._headers(bee)
        assert h["swarm-index-document"] == "index.html"
        assert "swarm-error-document" not in h
        assert h["swarm-collection"] == "true"

    def test_dot_slash_index_html_counts(self):
        bee = UploadBee()
        r = _upload(bee, _tar([("./", None), ("./index.html", b"x")]))
        assert r.status_code == 200, r.text
        assert self._headers(bee)["swarm-index-document"] == "index.html"

    @pytest.mark.parametrize("entries", [
        [("a.txt", b"a")],
        [("sub/index.html", b"x")],
        [("index.html/", None), ("index.html/a", b"x")],
    ])
    def test_no_root_index_sends_no_header(self, entries):
        bee = UploadBee()
        r = _upload(bee, _tar(entries))
        assert r.status_code == 200, r.text
        assert "swarm-index-document" not in self._headers(bee)

    def test_explicit_documents(self):
        bee = UploadBee()
        tar_bytes = _tar([("index.html", b"x"), ("home.html", b"h"), ("e/404.html", b"n")])
        r = _upload(bee, tar_bytes, "&index_document=home.html&error_document=e/404.html")
        assert r.status_code == 200, r.text
        h = self._headers(bee)
        assert h["swarm-index-document"] == "home.html"
        assert h["swarm-error-document"] == "e/404.html"

    def test_error_document_alone_keeps_the_automatic_index(self):
        bee = UploadBee()
        r = _upload(bee, _tar([("index.html", b"x"), ("404.html", b"n")]), "&error_document=404.html")
        assert r.status_code == 200, r.text
        h = self._headers(bee)
        assert h["swarm-index-document"] == "index.html"
        assert h["swarm-error-document"] == "404.html"

    @pytest.mark.parametrize("param,value,code", [
        ("index_document", "missing.html", "INDEX_DOCUMENT_INVALID"),
        ("index_document", "sub", "INDEX_DOCUMENT_INVALID"),          # a directory
        ("index_document", "../index.html", "INDEX_DOCUMENT_INVALID"),
        ("index_document", "/index.html", "INDEX_DOCUMENT_INVALID"),
        ("index_document", "sub/", "INDEX_DOCUMENT_INVALID"),
        ("index_document", "", "INDEX_DOCUMENT_INVALID"),
        ("index_document", "hé.html", "INDEX_DOCUMENT_INVALID"),     # in the tar, not ASCII
        ("error_document", "hé.html", "ERROR_DOCUMENT_INVALID"),     # in the tar, not ASCII
        # In the tar too: only the header rule refuses them (h11 would refuse
        # the header after settle_payment).
        ("index_document", " index.html", "INDEX_DOCUMENT_INVALID"),
        ("index_document", "index.html ", "INDEX_DOCUMENT_INVALID"),
        ("index_document", "in\tdex.html", "INDEX_DOCUMENT_INVALID"),
        ("error_document", " index.html", "ERROR_DOCUMENT_INVALID"),
        ("error_document", "index.html ", "ERROR_DOCUMENT_INVALID"),
        ("error_document", "in\tdex.html", "ERROR_DOCUMENT_INVALID"),
        ("index_document", "a%2Fb", "INDEX_DOCUMENT_INVALID"),
        ("error_document", "e/a%2Fb", "ERROR_DOCUMENT_INVALID"),
        ("index_document", "sub/a", "INDEX_DOCUMENT_INVALID"),        # exists; Bee refuses a slash
        ("error_document", "nope.html", "ERROR_DOCUMENT_INVALID"),
        ("error_document", "a/./b", "ERROR_DOCUMENT_INVALID"),
        ("error_document", "link.html", "ERROR_DOCUMENT_INVALID"),     # a symlink
    ])
    def test_invalid_document_is_refused_before_payment(self, param, value, code):
        from urllib.parse import quote as q
        bee = UploadBee()
        # hé.html is a regular file at the root: only the ASCII rule refuses it
        # (a non-ASCII header would fail in httpx2 after settle_payment).
        tar_bytes = _tar([("index.html", b"x"), ("sub/", None), ("sub/a", b"a"),
                          ("link.html", "->index.html"), ("hé.html", b"h"),
                          (" index.html", b"s"), ("index.html ", b"s"), ("in\tdex.html", b"t")])
        with patch("app.api.endpoints.data.settle_payment") as settle:
            r = _upload(bee, tar_bytes, f"&{param}={q(value)}")
        assert r.status_code == 400, r.text
        assert r.json()["code"] == code
        if "%2F" in value:
            assert "percent-encoded slash (%2F)" in r.json()["message"]
            assert "%252F" not in r.json()["message"]
        settle.assert_not_called()
        assert [q for q in bee.requests if q.method == "POST"] == []


class TestDocumentNamesReachBeeUnchanged:
    """Every name the pre-payment check accepts must go out as a header the
    gateway's real HTTP stack (httpx2, httpcore2, h11) sends unchanged; a name
    it cannot send fails after settle_payment. A real socket server stands in
    for Bee, so nothing here is mocked between the gateway and the wire."""

    ACCEPTED = ["index.html", "a b.html", "~x.html", "x~!$&'()*+,;=@[]^`{|}.html",
                '"q".html', "%41.html", "x" * 200]

    @pytest.fixture
    def bee_server(self):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("content-length", 0)))
                received.append((self.headers.get("Swarm-Index-Document"),
                                 self.headers.get("Swarm-Error-Document")))
                body = json.dumps({"reference": REF}).encode()
                self.send_response(201)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{server.server_address[1]}/", received
        server.shutdown()
        server.server_close()

    def _send(self, url, index_document, error_document=None):
        import asyncio
        from app.services.http_client import _new_client

        async def go():
            client, _ = _new_client()
            try:
                with patch("app.services.swarm_api.get_client", return_value=client):
                    return await swarm_api.upload_collection_to_swarm(
                        _tar([("a", b"a")]), STAMP, index_document=index_document,
                        error_document=error_document)
            finally:
                await client.aclose()

        with patch.object(settings, "SWARM_BEE_API_URL", url):
            return asyncio.run(go())

    @pytest.mark.parametrize("name", ACCEPTED)
    def test_accepted_names_pass_the_check_and_arrive_exactly(self, bee_server, name):
        from app.api.endpoints.data import _document_path
        _document_path(name, single_segment=True)       # accepted as index_document
        _document_path("e/" + name)                      # and nested as error_document
        url, received = bee_server
        assert self._send(url, name, "e/" + name) == REF
        assert received == [(name, "e/" + name)]

    @pytest.mark.parametrize("name", [" index.html", "index.html "])
    def test_refused_names_are_ones_the_client_cannot_send_unchanged(self, bee_server, name):
        # Why the space rule exists: h11 refuses these or they reach Bee
        # altered. (A tab or DEL does go out unchanged; those are refused only
        # because the rule is printable ASCII, the same set as file names.)
        from app.api.endpoints.data import _document_path
        with pytest.raises(ValueError):
            _document_path(name)
        url, received = bee_server
        try:
            self._send(url, name)
        except Exception:
            return
        assert received[0][0] != name
