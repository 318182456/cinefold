"""Browser admission under load: FIFO, cancellation and capacity recovery."""
from concurrent.futures import ThreadPoolExecutor
import time

import pytest

from app.modules.ladysite.transport import BrowserQueue, FetchBudget, FetchCancelled


def queued(queue, count):
    deadline = time.perf_counter() + 2
    while time.perf_counter() < deadline:
        with queue.condition:
            if len(queue.waiting) == count:
                return
        time.sleep(0.005)
    pytest.fail("request did not enter browser queue")


def test_browser_waiters_enter_in_submission_order():
    queue = BrowserQueue(1)
    order = []

    def fetch(index):
        with queue.acquire(2):
            order.append(index)

    with ThreadPoolExecutor(3) as pool:
        with queue.acquire(2):
            futures = []
            for index in range(3):
                futures.append(pool.submit(fetch, index))
                queued(queue, index + 1)
        for future in futures:
            future.result(timeout=2)
    assert order == [0, 1, 2]
    assert queue.active == 0 and not queue.waiting


@pytest.mark.parametrize("cancel", [False, True])
def test_abandoned_waiter_does_not_block_following_requests(cancel):
    queue = BrowserQueue(1)
    budget = FetchBudget(time.perf_counter() + 5)

    def waiting():
        with queue.acquire(2 if cancel else 0.05, budget):
            pytest.fail("busy browser must not admit this request")

    with ThreadPoolExecutor(1) as pool:
        with queue.acquire(2):
            future = pool.submit(waiting)
            if cancel:
                queued(queue, 1)
                budget.cancel()
            with pytest.raises(FetchCancelled if cancel else TimeoutError):
                future.result(timeout=1)
            assert queue.active == 1 and not queue.waiting
        with queue.acquire(1):
            assert queue.active == 1
    assert queue.active == 0
