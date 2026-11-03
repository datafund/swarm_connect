# tests/tools/bee_stub.py
"""
A stub Bee node over real TCP, for the flood tests (#588).

Answers the Bee endpoints the gateway reads for /health, the stamp list and
downloads, plus a JSON-RPC eth_getBalance at /rpc (point BASE_RPC_URL there).
Counts every request by path, and the most requests it ever had in flight at
once. Optional per-request latency, and a gate (`release`) that holds requests
until it is set.

Run standalone for scripts/flood_replay.py:

    python -m tests.tools.bee_stub --port 18633 --latency 0.05

GET /__stats returns the counters as JSON; POST /__reset clears them. Neither
is counted.
"""
import argparse
import asyncio
import json
from collections import Counter
from typing import Dict, Optional, Tuple

BATCH_ID = "ab" * 32
WALLET = "0x" + "11" * 20
CHEQUEBOOK = "0x" + "22" * 20


def _json(obj) -> Tuple[int, bytes, str]:
    return 200, json.dumps(obj).encode(), "application/json"


def default_routes(method: str, path: str, body: bytes) -> Tuple[int, bytes, str]:
    p = path.split("?", 1)[0].rstrip("/")
    if method == "POST" and p == "/rpc":
        req_id = 1
        try:
            req_id = json.loads(body or b"{}").get("id", 1)
        except ValueError:
            pass
        return _json({"jsonrpc": "2.0", "id": req_id, "result": hex(10 ** 17)})
    if p == "/topology":
        return _json({"population": 1000, "connected": 120, "depth": 10,
                      "reachability": "Public", "networkAvailability": "Available"})
    if p == "/status":
        return _json({"beeMode": "full", "connectedPeers": 120, "isWarmingUp": False,
                      "lastSyncedBlock": 100, "storageRadius": 10})
    if p == "/health":
        return _json({"status": "ok", "version": "2.8.0-stub", "apiVersion": "7.0.0"})
    if p == "/addresses":
        return _json({"overlay": "ff" * 32})
    if p == "/chainstate":
        return _json({"block": 100, "chainTip": 100, "totalAmount": "1", "currentPrice": "24000"})
    if p == "/wallet":
        return _json({"walletAddress": WALLET, "bzzBalance": str(10 ** 17),
                      "nativeTokenBalance": str(10 ** 18)})
    if p == "/chequebook/balance":
        return _json({"totalBalance": str(10 ** 16), "availableBalance": str(10 ** 16)})
    if p == "/chequebook/address":
        return _json({"chequebookAddress": CHEQUEBOOK})
    if p == "/batches":
        return _json({"batches": [{"batchID": BATCH_ID, "value": "1000", "depth": 20, "bucketDepth": 16,
                                   "batchTTL": 86400, "immutable": False}]})
    if p == "/stamps":
        return _json({"stamps": []})
    if p.startswith("/bzz/") and method == "GET":
        return 200, b"x" * 1024, "application/octet-stream"
    if p in ("/bzz", "/bytes", "/chunks") and method == "POST":
        return 201, json.dumps({"reference": "cd" * 32}).encode(), "application/json"
    return 404, b'{"message":"Not Found","code":404}', "application/json"


class BeeStub:
    def __init__(self, latency: float = 0.0, host: str = "127.0.0.1", port: int = 0) -> None:
        self.latency = latency
        self.host = host
        self.port = port
        self.counts: Counter = Counter()
        self.in_flight = 0
        self.peak_in_flight = 0
        self.release = asyncio.Event()
        self.release.set()
        self._server: Optional[asyncio.base_events.Server] = None
        self.latency_by_prefix: Dict[str, float] = {}
        self._writers = set()

    async def start(self) -> "BeeStub":
        self._server = await asyncio.start_server(self._handle, self.host, self.port, backlog=1024)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            # Kept-alive client connections would hold wait_closed() open.
            for writer in list(self._writers):
                writer.close()
            await self._server.wait_closed()

    def reset(self) -> None:
        self.counts = Counter()
        self.peak_in_flight = self.in_flight

    def stats(self) -> dict:
        return {"total": sum(self.counts.values()), "by_path": dict(self.counts),
                "peak_in_flight": self.peak_in_flight, "in_flight": self.in_flight}

    def _delay(self, path: str) -> float:
        for prefix, delay in self.latency_by_prefix.items():
            if path.startswith(prefix):
                return delay
        return self.latency

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    method, path, _ = line.decode("latin-1").split(" ", 2)
                except ValueError:
                    break
                headers = {}
                while True:
                    h = await reader.readline()
                    if h in (b"\r\n", b"\n", b""):
                        break
                    k, _, v = h.decode("latin-1").partition(":")
                    headers[k.strip().lower()] = v.strip()
                body = b""
                length = int(headers.get("content-length", "0") or 0)
                if length:
                    body = await reader.readexactly(length)

                if path == "/__stats":
                    status, payload, ctype = _json(self.stats())
                elif path == "/__reset":
                    self.reset()
                    status, payload, ctype = _json({"ok": True})
                else:
                    self.counts[path.split("?", 1)[0]] += 1
                    self.in_flight += 1
                    self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
                    try:
                        await self.release.wait()
                        delay = self._delay(path)
                        if delay:
                            await asyncio.sleep(delay)
                        status, payload, ctype = default_routes(method, path, body)
                    finally:
                        self.in_flight -= 1
                writer.write(
                    f"HTTP/1.1 {status} X\r\nContent-Type: {ctype}\r\n"
                    f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload
                )
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self._writers.discard(writer)
            try:
                writer.close()
            except Exception:
                pass


async def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18633)
    parser.add_argument("--latency", type=float, default=0.05, help="seconds per request")
    args = parser.parse_args()
    stub = await BeeStub(latency=args.latency, host=args.host, port=args.port).start()
    print(f"stub Bee on {stub.base_url} (latency {args.latency} s)", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(_main())
