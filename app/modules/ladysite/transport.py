"""Cancellable network I/O for synchronous scraper parsers.

One daemon event loop performs HTTP I/O; parser workers only wait for their own
future. A scrape's deadline includes throttling, solver queues and all requests.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
from contextlib import contextmanager
from contextvars import ContextVar
import threading
import time

import httpx


class FetchCancelled(TimeoutError):
    pass


_budget: ContextVar[FetchBudget | None] = ContextVar("scrape_fetch_budget", default=None)
_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()


def current_budget() -> FetchBudget | None:
    return _budget.get()


class FetchBudget:
    def __init__(self, deadline: float):
        self.deadline = deadline
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._futures: set[concurrent.futures.Future] = set()

    @contextmanager
    def activate(self):
        token = _budget.set(self)
        try:
            self.check()
            yield self
        finally:
            _budget.reset(token)

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.perf_counter())

    def check(self):
        if self._cancelled.is_set() or self.remaining() <= 0:
            raise FetchCancelled("scrape network budget ended")

    def cancel(self):
        self._cancelled.set()
        with self._lock:
            futures = tuple(self._futures)
        for future in futures:
            future.cancel()

    def wait(self, seconds: float):
        self.check()
        self._cancelled.wait(min(max(0.0, seconds), self.remaining()))
        self.check()

    @contextmanager
    def acquire(self, lock):
        while True:
            self.check()
            if lock.acquire(timeout=min(0.05, self.remaining())):
                break
        try:
            self.check()
            yield
        finally:
            lock.release()

    def run(self, coroutine):
        try:
            self.check()
            loop = _network_loop()
        except BaseException:
            coroutine.close()
            raise
        with self._lock:
            if self._cancelled.is_set():
                coroutine.close()
                raise FetchCancelled("scrape cancelled")
            future = asyncio.run_coroutine_threadsafe(coroutine, loop)
            self._futures.add(future)
        try:
            return future.result(timeout=self.remaining())
        except (concurrent.futures.CancelledError, concurrent.futures.TimeoutError) as exc:
            future.cancel()
            raise FetchCancelled("scrape network budget ended") from exc
        finally:
            with self._lock:
                self._futures.discard(future)


def _network_loop():
    global _loop
    with _loop_lock:
        if _loop is None or _loop.is_closed():
            loop = asyncio.new_event_loop()
            thread = threading.Thread(target=loop.run_forever, name="scrape-http", daemon=True)
            thread.start()
            _loop = loop
        return _loop


class BudgetClient:
    """Sync-facing adapter; each request closes its async client on cancellation."""
    def __init__(self, **options):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def request(self, method, url, **kwargs):
        budget = current_budget()
        if budget is None:
            raise RuntimeError("BudgetClient requires an active fetch budget")

        async def send():
            budget.check()
            options = dict(self.options)
            timeout = options.get("timeout", 15.0)
            limit = min(float(timeout), budget.remaining())
            options["timeout"] = limit
            async with httpx.AsyncClient(**options) as client:
                return await asyncio.wait_for(client.request(method, url, **kwargs), timeout=limit)

        return budget.run(send())

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)


def http_client(**kwargs):
    return BudgetClient(**kwargs) if current_budget() else httpx.Client(**kwargs)
