"""Runs one CLI process: argument vector (no shell), prompt on stdin, its own
process group, a hard deadline, capped output, and the whole group killed
whenever the call ends early.
"""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import threading
import time
from dataclasses import dataclass

STDERR_TAIL = 16 * 1024
KILL_GRACE = 2.0


@dataclass
class Invocation:
    argv: list
    env: dict
    cwd: str
    stdin: bytes


class Stop(Exception):
    """Raised by a line handler or poll function to end the process early.
    The exception propagates out of run() after the group was killed."""


class Timeout(Exception):
    pass


class OutputTooLarge(Exception):
    pass


class Registry:
    """Running processes, so a shutdown can kill them all."""

    def __init__(self):
        self._lock = threading.Lock()
        self._procs: set = set()

    def add(self, proc):
        with self._lock:
            self._procs.add(proc)

    def discard(self, proc):
        with self._lock:
            self._procs.discard(proc)

    def kill_all(self):
        with self._lock:
            procs = list(self._procs)
        for proc in procs:
            _signal_group(proc.pid, signal.SIGKILL)

    def __len__(self):
        with self._lock:
            return len(self._procs)


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _exited(proc) -> bool:
    """True once the process has exited, without reaping it: an unreaped
    leader keeps its process-group ID reserved, so killpg cannot hit an
    unrelated group that reused the number. (Python builds without
    os.waitid, such as some macOS ones, fall back to reaping.)"""
    if not hasattr(os, "waitid"):
        return proc.poll() is not None
    try:
        return os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
    except ChildProcessError:
        return True


def _kill_group(proc) -> None:
    _signal_group(proc.pid, signal.SIGTERM)
    deadline = time.monotonic() + KILL_GRACE
    while not _exited(proc) and time.monotonic() < deadline:
        time.sleep(0.05)
    _signal_group(proc.pid, signal.SIGKILL)


def run(inv: Invocation, *, deadline: float, on_line, poll=None, max_stdout: int, registry: Registry | None = None):
    """Runs inv until it exits; calls on_line(bytes) for every stdout line and
    poll() about every 200 ms. Returns (returncode, stderr_tail).

    Raises Timeout when the deadline passes, OutputTooLarge when stdout grows
    beyond max_stdout, or whatever on_line/poll raised. In every one of these
    cases, and on normal exit, the whole process group is gone afterwards.
    """
    proc = subprocess.Popen(
        inv.argv,
        cwd=inv.cwd,
        env=inv.env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        close_fds=True,
    )
    if registry is not None:
        registry.add(proc)

    def feed_stdin():
        try:
            proc.stdin.write(inv.stdin)
        except OSError:
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    writer = threading.Thread(target=feed_stdin, name="cli-stdin", daemon=True)
    writer.start()

    sel = selectors.DefaultSelector()
    for stream in (proc.stdout, proc.stderr):
        os.set_blocking(stream.fileno(), False)
        sel.register(stream, selectors.EVENT_READ)
    buf = bytearray()
    err = bytearray()
    total = 0

    def read_ready(timeout: float) -> bool:
        """Reads whatever is ready; False when nothing was."""
        nonlocal total
        got = False
        for key, _ in sel.select(timeout=timeout):
            try:
                data = os.read(key.fd, 65536)
            except BlockingIOError:
                continue
            if not data:
                sel.unregister(key.fileobj)
                continue
            got = True
            if key.fileobj is proc.stderr:
                err.extend(data)
                del err[:-STDERR_TAIL]
                continue
            total += len(data)
            if total > max_stdout:
                raise OutputTooLarge()
            buf.extend(data)
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                line = bytes(buf[:nl])
                del buf[: nl + 1]
                if line.strip():
                    on_line(line)
        return got

    try:
        while sel.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Timeout()
            read_ready(min(0.2, remaining))
            if poll is not None:
                poll()
            if _exited(proc):
                # The CLI is gone. A child it left may still hold the pipes
                # open: take what is buffered, then stop waiting for EOF.
                while sel.get_map() and read_ready(0):
                    pass
                break
        if buf.strip():
            on_line(bytes(buf))
        while not _exited(proc):
            if time.monotonic() >= deadline:
                raise Timeout()
            if poll is not None:
                poll()
            time.sleep(0.05)
        # Children the CLI left behind go with it.
        _signal_group(proc.pid, signal.SIGKILL)
    except BaseException:
        _kill_group(proc)
        raise
    finally:
        sel.close()
        returncode = proc.wait()
        for stream in (proc.stdout, proc.stderr):
            stream.close()
        writer.join(timeout=1)
        if registry is not None:
            registry.discard(proc)
    return returncode, err.decode("utf-8", "replace")
