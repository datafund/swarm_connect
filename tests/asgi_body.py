"""Drive an ASGI app with a request body served by a counting receive (#696).

Shared by the body-size tests: they need to see exactly how many body bytes
the app (middleware included) asked for before it answered.
"""
import asyncio
import json


class CountingBody:
    """An ASGI receive that serves `total` bytes in `piece`-sized messages and
    counts how many bytes the app actually asked for. With `refuse`, any call
    fails the test."""

    def __init__(self, total, piece, refuse=False, fill=b"x"):
        self.total = total
        self.piece = piece
        self.refuse = refuse
        self.fill = fill
        self.sent = 0
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        if self.refuse:
            raise AssertionError("the body was read")
        if self.sent >= self.total:
            # The app reads past the end only when it waits for a disconnect.
            await asyncio.sleep(3600)
        n = min(self.piece, self.total - self.sent)
        self.sent += n
        return {"type": "http.request", "body": self.fill * n,
                "more_body": self.sent < self.total}


def call(app, path, receive, headers, method="POST", timeout=10):
    """Run one request through `app`; return (status, parsed JSON body or raw bytes)."""
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path,
        "raw_path": path.encode(), "root_path": "", "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("127.0.0.1", 50000), "server": ("testserver", 80),
    }
    out = {"status": None, "body": b""}

    async def send(message):
        if message["type"] == "http.response.start":
            out["status"] = message["status"]
        elif message["type"] == "http.response.body":
            out["body"] += message.get("body", b"")

    async def run():
        await asyncio.wait_for(app(scope, receive, send), timeout=timeout)

    asyncio.run(run())
    try:
        return out["status"], json.loads(out["body"] or b"null")
    except ValueError:
        return out["status"], out["body"]
