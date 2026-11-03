# app/core/io_lane.py
"""A small pool of daemon threads for one state file's disk writes.

Each persisted payment store gets its own lane, so a hung write to one file
delays and fails closed only the requests that need that file (#553, #569).

Daemon threads, not a ThreadPoolExecutor: concurrent.futures joins its workers
at interpreter exit without a timeout, so one write hung on a dead disk would
keep the process from exiting. A hung write never landed, so nothing is lost
by not waiting for it beyond the shutdown bound.
"""
import concurrent.futures
import logging
import queue
import threading
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class _DaemonPool:
    def __init__(self, workers: int, name: str):
        self._queue: "queue.SimpleQueue" = queue.SimpleQueue()
        self._threads = [threading.Thread(target=self._run, name=f"{name}-{i}", daemon=True)
                         for i in range(workers)]
        for t in self._threads:
            t.start()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            fut, fn, args = item
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                fut.set_result(fn(*args))
            except BaseException as e:      # delivered to the waiter, never lost
                fut.set_exception(e)

    def submit(self, fn, *args) -> concurrent.futures.Future:
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._queue.put((fut, fn, args))
        return fut

    def shutdown(self) -> None:
        """Workers exit after the work queued before this; never waits."""
        for _ in self._threads:
            self._queue.put(None)


class IOLane:
    """Daemon worker pool plus the bookkeeping the stores need: how many writes
    are pending, how long the oldest has waited, and a bounded shutdown wait.
    The pool is created lazily, and again after a shutdown (tests)."""

    def __init__(self, workers: int, name: str):
        self._workers = workers
        self._name = name
        self._pool: Optional[_DaemonPool] = None
        self._pending: Dict[concurrent.futures.Future, float] = {}   # future -> queued at
        self._lock = threading.Lock()

    def executor(self) -> _DaemonPool:
        with self._lock:
            if self._pool is None:
                self._pool = _DaemonPool(self._workers, self._name)
            return self._pool

    def submit(self, fn, *args) -> concurrent.futures.Future:
        """Run fn(*args) on this lane; tracked for backlog, age and shutdown."""
        fut = self.executor().submit(fn, *args)
        with self._lock:
            self._pending[fut] = time.monotonic()
        fut.add_done_callback(self._discard)
        return fut

    def _discard(self, fut) -> None:
        with self._lock:
            self._pending.pop(fut, None)

    def backlog(self) -> int:
        with self._lock:
            return len(self._pending)

    def oldest_age(self) -> float:
        """Seconds the oldest pending write has been queued or running."""
        with self._lock:
            if not self._pending:
                return 0.0
            return time.monotonic() - min(self._pending.values())

    def shutdown(self, timeout: float = 5.0) -> None:
        """Wait at most `timeout` seconds for pending writes, then release the
        pool. Its daemon workers cannot hold up the process exit."""
        with self._lock:
            pending = set(self._pending)
            pool, self._pool = self._pool, None
        if pending:
            _, not_done = concurrent.futures.wait(pending, timeout=timeout)
            if not_done:
                logger.error("%s: %d state writes still running at shutdown (hung disk?); "
                             "exiting without them", self._name, len(not_done))
        if pool is not None:
            pool.shutdown()
