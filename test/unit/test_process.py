"""Process runner: no shell, stdin, deadline, output cap, group kill, limiter."""

import os
import signal
import sys
import tempfile
import threading
import time
import unittest

import context  # noqa: F401
from cli_bridge import process
from cli_bridge.errors import BridgeError
from cli_bridge.limiter import Limiter

PY = sys.executable


def inv(code, stdin=b""):
    return process.Invocation(argv=[PY, "-c", code], env={"PATH": os.environ.get("PATH", "")},
                              cwd=tempfile.gettempdir(), stdin=stdin)


def group_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class Runner(unittest.TestCase):
    def run_lines(self, invocation, **kw):
        lines = []
        kw.setdefault("deadline", time.monotonic() + 20)
        kw.setdefault("max_stdout", 1 << 20)
        rc, err = process.run(invocation, on_line=lines.append, **kw)
        return rc, err, lines

    def test_stdin_and_lines(self):
        code = "import sys; data = sys.stdin.read(); print(data.upper()); print('second', end='')"
        rc, _, lines = self.run_lines(inv(code, b"$(rm -rf /) `x` ; hi"))
        self.assertEqual(rc, 0)
        self.assertEqual(lines, [b"$(RM -RF /) `X` ; HI", b"second"])

    def test_stderr_tail_and_exit_code(self):
        rc, err, _ = self.run_lines(inv("import sys; sys.stderr.write('bad thing\\n'); sys.exit(3)"))
        self.assertEqual(rc, 3)
        self.assertIn("bad thing", err)

    def test_timeout_kills_the_whole_group(self):
        code = ("import subprocess, time; subprocess.Popen(['sleep', '30']); "
                "print('started', flush=True); time.sleep(30)")
        pids = []
        registry = process.Registry()

        def on_line(_line):
            pids.extend(p.pid for p in list(registry._procs))

        t0 = time.monotonic()
        with self.assertRaises(process.Timeout):
            process.run(inv(code), deadline=time.monotonic() + 1.5, on_line=on_line, max_stdout=1 << 20,
                        registry=registry)
        self.assertLess(time.monotonic() - t0, 6)
        self.assertEqual(len(registry), 0)
        self.assertTrue(pids)
        time.sleep(0.2)
        self.assertFalse(group_alive(pids[0]), "the child in the group survived")

    def test_stop_from_a_line_handler(self):
        def on_line(line):
            raise process.Stop()

        with self.assertRaises(process.Stop):
            process.run(inv("import time; print('x', flush=True); time.sleep(30)"),
                        deadline=time.monotonic() + 20, on_line=on_line, max_stdout=1 << 20)

    def test_output_cap(self):
        with self.assertRaises(process.OutputTooLarge):
            self.run_lines(inv("print('x' * 100000)"), max_stdout=1000)

    def test_children_left_behind_are_killed(self):
        code = ("import subprocess, os; p = subprocess.Popen(['sleep', '30']); print(os.getpgid(0), flush=True)")
        rc, _, lines = self.run_lines(inv(code))
        self.assertEqual(rc, 0)
        time.sleep(0.2)
        self.assertFalse(group_alive(int(lines[0])))

    def test_kill_all(self):
        registry = process.Registry()
        result = {}

        def runner():
            try:
                process.run(inv("import time; time.sleep(30)"), deadline=time.monotonic() + 20,
                            on_line=lambda line: None, max_stdout=1 << 20, registry=registry)
                result["rc"] = "returned"
            except Exception as err:  # noqa: BLE001
                result["rc"] = err

        th = threading.Thread(target=runner)
        th.start()
        for _ in range(50):
            if len(registry):
                break
            time.sleep(0.05)
        registry.kill_all()
        th.join(5)
        self.assertFalse(th.is_alive())
        self.assertEqual(result["rc"], "returned")


class Limits(unittest.TestCase):
    def test_queue_full_and_timeout(self):
        lim = Limiter(slots=1, queue=1, queue_timeout=0.5)
        entered = threading.Event()
        release = threading.Event()

        def holder():
            with lim.slot():
                entered.set()
                release.wait(5)

        th = threading.Thread(target=holder)
        th.start()
        entered.wait(5)
        waiter_err = {}

        def waiter():
            try:
                with lim.slot():
                    pass
            except BridgeError as err:
                waiter_err["e"] = err

        tw = threading.Thread(target=waiter)
        tw.start()
        time.sleep(0.1)
        with self.assertRaises(BridgeError) as cm:
            with lim.slot():
                pass
        self.assertEqual((cm.exception.status, cm.exception.code), (429, "queue_full"))
        tw.join(5)
        self.assertEqual(waiter_err["e"].code, "queue_timeout")
        release.set()
        th.join(5)
        with lim.slot() as waited:
            self.assertLess(waited, 0.5)
        self.assertEqual(lim.snapshot()["running"], 0)


if __name__ == "__main__":
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    unittest.main()
