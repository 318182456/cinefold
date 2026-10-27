"""Shared bound for expensive background work; nested calls keep their permit."""
from contextlib import contextmanager
import threading

_slots = threading.BoundedSemaphore(2)
_local = threading.local()


@contextmanager
def slot():
    depth = getattr(_local, 'depth', 0)
    if not depth:
        _slots.acquire()
    _local.depth = depth + 1
    try:
        yield
    finally:
        _local.depth -= 1
        if not depth:
            _slots.release()
