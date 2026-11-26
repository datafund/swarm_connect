# tests/test_manifest_tar_limits.py
"""POST /data/manifest parses the TAR safely (#694).

A compressed archive used to be unpacked with tarfile 'r:*' and every entry
held by getmembers(), on the event loop and before payment: a 300 KB xz with
4M empty entries OOM-killed a 512 MB gateway, and a gzip one blocked /health
for 20 s. These tests go through the endpoint only, so they exercise the old
code path as well as the new one.
"""
import asyncio
import bz2
import gzip
import io
import lzma
import tarfile
import threading
import time
import tracemalloc
import zlib
from unittest.mock import AsyncMock, patch

import httpx2
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app

client = TestClient(app)
STAMP = "a" * 64
URL = f"/api/v1/data/manifest?stamp_id={STAMP}"
EMPTY_FILE_HEADER = tarfile.TarInfo("f").tobuf(format=tarfile.USTAR_FORMAT)
END_OF_ARCHIVE = b"\0" * 1024


def plain_tar(names, content=b"x"):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name in names:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def many_empty_entries(n):
    """A plain tar of n empty files, built without tarfile's member list."""
    out = bytearray()
    for i in range(n):
        info = tarfile.TarInfo(f"f{i}")
        out += info.tobuf(format=tarfile.USTAR_FORMAT)
    return bytes(out) + END_OF_ARCHIVE


@pytest.fixture(scope="module")
def gzip_bomb():
    """447 KB of gzip that unpacks to 200,000 empty entries (100 MB)."""
    c = zlib.compressobj(9, zlib.DEFLATED, 31)
    chunk = EMPTY_FILE_HEADER * 2000
    parts = [c.compress(chunk) for _ in range(100)]
    parts += [c.compress(END_OF_ARCHIVE), c.flush()]
    return b"".join(parts)


def post(data, name="files.tar"):
    return client.post(URL, files={"file": (name, io.BytesIO(data), "application/x-tar")})


@pytest.fixture
def bee():
    """Bee upload and payment settlement, so a test can see they were not reached."""
    with patch("app.api.endpoints.data.upload_collection_to_swarm",
               new=AsyncMock(return_value="ref")) as upload, \
         patch("app.api.endpoints.data.settle_payment", new=AsyncMock()) as settle:
        yield upload, settle


def code_of(response):
    body = response.json()
    return body.get("code") or (body.get("detail") or {}).get("code")


class TestCompressedArchives:

    def test_a_gzip_bomb_is_refused_fast_and_in_bounded_memory(self, gzip_bomb, bee):
        upload, settle = bee
        tracemalloc.start()
        try:
            tracemalloc.reset_peak()
            start = time.perf_counter()
            r = post(gzip_bomb, "bomb.tar.gz")
            elapsed = time.perf_counter() - start
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert r.status_code == 400, r.text
        assert code_of(r) == "TAR_COMPRESSED"
        assert "uncompressed" in r.json()["message"]
        # Unpacking it held 200,000 TarInfo objects (well over 50 MB) and took
        # seconds. Refusing it reads one 512-byte header.
        assert peak < 10 * 1024 * 1024, f"peak {peak} bytes"
        assert elapsed < 2.0, f"{elapsed:.2f} s"
        upload.assert_not_called()
        settle.assert_not_called()

    @pytest.mark.parametrize("kind,compress", [
        ("gzip", gzip.compress),
        ("bzip2", bz2.compress),
        ("xz", lzma.compress),
    ])
    def test_every_compressed_format_is_refused_before_payment(self, kind, compress, bee):
        upload, settle = bee
        r = post(compress(plain_tar(["index.html"])), f"files.tar.{kind}")
        assert r.status_code == 400
        assert code_of(r) == "TAR_COMPRESSED"
        assert kind in r.json()["message"]
        upload.assert_not_called()
        settle.assert_not_called()

    def test_a_plain_tar_whose_first_file_looks_like_a_magic_number_is_accepted(self, bee):
        upload, _ = bee
        r = post(plain_tar(["BZh91AY.txt", "index.html"]))
        assert r.status_code == 200, r.text
        assert r.json()["file_count"] == 2
        upload.assert_called_once()


class TestEntryLimits:

    def test_the_default_limit_refuses_10001_entries(self, bee):
        upload, settle = bee
        r = post(many_empty_entries(10_001))
        assert r.status_code == 400, r.text
        assert code_of(r) == "TAR_TOO_MANY_ENTRIES"
        assert settings.MANIFEST_MAX_MEMBERS == 10_000
        upload.assert_not_called()
        settle.assert_not_called()

    def test_exactly_the_limit_is_accepted_and_counted(self, bee, monkeypatch):
        upload, _ = bee
        monkeypatch.setattr(settings, "MANIFEST_MAX_MEMBERS", 50)
        r = post(many_empty_entries(50))
        assert r.status_code == 200, r.text
        assert r.json()["file_count"] == 50
        upload.assert_called_once()

    def test_directories_count_towards_the_limit(self, bee):
        upload, _ = bee
        # 9,999 directories and two files: 10,001 entries, only two of them files.
        out = bytearray()
        for i in range(9_999):
            info = tarfile.TarInfo(f"d{i}/")
            info.type = tarfile.DIRTYPE
            out += info.tobuf(format=tarfile.USTAR_FORMAT)
        r = post(bytes(out) + plain_tar(["index.html", "app.js"]))
        assert r.status_code == 400, r.text
        assert code_of(r) == "TAR_TOO_MANY_ENTRIES"
        upload.assert_not_called()

    def test_a_path_over_the_byte_limit_is_refused(self, bee):
        upload, _ = bee
        r = post(plain_tar(["index.html", "d/" + "x" * 1100]))
        assert r.status_code == 400
        assert code_of(r) == "TAR_PATH_TOO_LONG"
        upload.assert_not_called()

    def test_a_path_at_the_byte_limit_is_accepted(self, bee):
        r = post(plain_tar(["d/" + "x" * 1022]))
        assert r.status_code == 200, r.text

    def test_a_path_over_the_depth_limit_is_refused(self, bee):
        upload, _ = bee
        r = post(plain_tar(["/".join(["d"] * 65)]))
        assert r.status_code == 400
        assert code_of(r) == "TAR_PATH_TOO_DEEP"
        upload.assert_not_called()

    def test_a_path_at_the_depth_limit_is_accepted(self, bee):
        r = post(plain_tar(["/".join(["d"] * 64)]))
        assert r.status_code == 200, r.text

    def test_sparse_entries_declaring_more_than_the_upload_are_refused(self, bee):
        upload, _ = bee
        # A GNU sparse entry declares its full size while carrying almost no
        # data, so a plain tar's sizes can exceed the upload only this way.
        info = tarfile.TarInfo("sparse.bin")
        info.type = tarfile.GNUTYPE_SPARSE
        info.size = 0
        header = bytearray(info.tobuf(format=tarfile.GNU_FORMAT))
        # realsize field of the old GNU header (offset 483, 12 octal bytes):
        header[483:495] = b"%011o\0" % (1 << 30)
        header[148:156] = b"        "
        header[148:156] = b"%06o\0 " % sum(header)
        r = post(bytes(header) + END_OF_ARCHIVE)
        assert r.status_code == 400, r.text
        assert code_of(r) == "TAR_CONTENT_TOO_LARGE"
        upload.assert_not_called()


class TestLegitimateManifests:

    def test_a_website_with_index_html_uploads_and_counts_only_files(self, bee):
        upload, settle = bee
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            d = tarfile.TarInfo("assets/")
            d.type = tarfile.DIRTYPE
            tar.addfile(d)
            for name, body in (("index.html", b"<h1>hi</h1>"), ("assets/app.js", b"1;"),
                               ("assets/style.css", b"a{}")):
                info = tarfile.TarInfo(name)
                info.size = len(body)
                tar.addfile(info, io.BytesIO(body))
        data = buf.getvalue()
        r = post(data)
        assert r.status_code == 200, r.text
        assert r.json()["file_count"] == 3
        settle.assert_awaited_once()
        assert upload.call_args.args[0] == data   # Bee gets the archive unchanged

    def test_timing_still_reports_both_tar_fields(self, bee):
        r = client.post(URL + "&include_timing=true",
                        files={"file": ("f.tar", io.BytesIO(plain_tar(["index.html"])), "application/x-tar")})
        assert r.status_code == 200
        timing = r.json()["timing"]
        assert timing["tar_validate_ms"] >= 0
        assert timing["tar_count_ms"] == 0.0


class TestEventLoopStaysResponsive:

    @pytest.mark.asyncio
    async def test_health_answers_while_a_manifest_is_being_parsed(self, bee):
        """Parsing is made slow (1.5 s inside TarFile.next, which both the old
        getmembers() and the new streaming pass call). On the event loop that
        stalled every request; off it, /health keeps answering throughout."""
        parse = {}
        real_next = tarfile.TarFile.next
        first = threading.Event()

        def slow_next(self):
            if not first.is_set():
                first.set()
                parse["start"] = time.monotonic()
                time.sleep(1.5)
                parse["end"] = time.monotonic()
            return real_next(self)

        data = plain_tar(["index.html"])
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
            await c.get("/health")   # warm up outside the measured window
            probes = []

            async def probe_until(task):
                while not task.done():
                    t0 = time.monotonic()
                    r = await c.get("/health")
                    probes.append((t0, time.monotonic(), r.status_code))
                    await asyncio.sleep(0.05)

            with patch.object(tarfile.TarFile, "next", slow_next):
                upload = asyncio.create_task(c.post(
                    URL, files={"file": ("f.tar", data, "application/x-tar")}))
                await probe_until(upload)
                r = await upload

        assert r.status_code == 200, r.text
        assert "start" in parse and "end" in parse
        answered_during = [p for p in probes if parse["start"] < p[1] < parse["end"]]
        worst = max(end - start for start, end, _ in probes)
        assert len(answered_during) >= 3, (probes, parse)
        assert worst < 0.75, f"/health took {worst:.2f} s during the parse"
        assert all(status == 200 for *_, status in probes)
