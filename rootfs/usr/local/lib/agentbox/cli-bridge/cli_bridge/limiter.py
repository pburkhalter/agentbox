"""Bounded concurrency: a few calls run, a short queue waits, the rest get 429."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager

from . import errors


class Limiter:
    def __init__(self, slots: int, queue: int, queue_timeout: float):
        self.slots = slots
        self.queue = queue
        self.queue_timeout = queue_timeout
        self.active = 0
        self.waiting = 0
        self._cond = threading.Condition()

    @contextmanager
    def slot(self):
        """Yields the seconds spent waiting; raises a 429 BridgeError when the
        queue is full or the wait took longer than queue_timeout."""
        started = time.monotonic()
        with self._cond:
            if self.active >= self.slots:
                if self.waiting >= self.queue:
                    raise errors.queue_full(
                        f"busy: {self.active} call(s) running and {self.waiting} waiting; try again shortly"
                    )
                self.waiting += 1
                try:
                    deadline = started + self.queue_timeout
                    while self.active >= self.slots:
                        left = deadline - time.monotonic()
                        if left <= 0:
                            raise errors.queue_full(
                                f"no free slot within {self.queue_timeout:g} s; try again shortly",
                                code="queue_timeout",
                            )
                        self._cond.wait(left)
                finally:
                    self.waiting -= 1
            self.active += 1
        try:
            yield time.monotonic() - started
        finally:
            with self._cond:
                self.active -= 1
                self._cond.notify()

    def snapshot(self) -> dict:
        with self._cond:
            return {"running": self.active, "waiting": self.waiting, "slots": self.slots, "queue": self.queue}
