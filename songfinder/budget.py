"""Cooperative deadlines shared by a search stage and its worker threads."""
from contextlib import contextmanager
from contextvars import ContextVar
import time

_deadline = ContextVar('songfinder_deadline', default=None)


class Expired(TimeoutError):
    pass


def timeout(default):
    end = _deadline.get()
    if end is None:
        return default
    left = end - time.monotonic()
    if left <= 0:
        raise Expired('Search stage budget reached')
    return min(default, left)


def check():
    timeout(float('inf'))


def sleep(seconds):
    time.sleep(timeout(seconds))
    check()


@contextmanager
def limit(seconds):
    end = time.monotonic() + seconds
    parent = _deadline.get()
    token = _deadline.set(min(end, parent) if parent is not None else end)
    try:
        check()
        yield
    finally:
        _deadline.reset(token)
