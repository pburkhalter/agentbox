"""One chat completion: run the CLI, collect or stream its answer, clean up."""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field

from . import errors, process
from .adapters.base import Fail, Finish, Text, UsageEvent
from .openai_format import Usage, truncate_utf8


class ClientGone(process.Stop):
    """The HTTP client went away; the CLI is stopped to save quota."""


class _Done(process.Stop):
    """The answer is complete or failed; no need to wait for the CLI."""


@dataclass
class Outcome:
    text_parts: list = field(default_factory=list)
    finish: str | None = None
    usage: Usage = field(default_factory=Usage)
    error: errors.BridgeError | None = None
    client_gone: bool = False
    first_text_at: float | None = None
    returncode: int | None = None
    leftovers: list = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(self.text_parts)


class Sink:
    """Receives the answer while it is produced. The default collects it."""

    def text(self, delta: str) -> None:
        pass

    def tick(self) -> None:
        """Called about every 200 ms; raises ClientGone when the client left."""


def execute(adapter, model, effort, req, cfg, procs: process.Registry, sink: Sink, call_id: str, log) -> Outcome:
    out = Outcome()
    sent_bytes = 0
    started = time.monotonic()
    scratch = os.path.join(cfg.runtime_dir, "calls", call_id)
    os.makedirs(scratch, mode=0o700)

    def handle(ev):
        nonlocal sent_bytes
        if isinstance(ev, Text):
            data = ev.text
            size = len(data.encode("utf-8"))
            if sent_bytes + size > cfg.max_output_bytes:
                data = truncate_utf8(data, cfg.max_output_bytes - sent_bytes)
                if data:
                    _emit(data)
                out.finish = "length"
                raise _Done()
            sent_bytes += size
            _emit(data)
        elif isinstance(ev, UsageEvent):
            out.usage = ev.usage
        elif isinstance(ev, Finish):
            if out.finish is None:
                out.finish = ev.reason
        elif isinstance(ev, Fail):
            out.error = ev.error
            raise _Done()

    def _emit(data):
        if out.first_text_at is None:
            out.first_text_at = time.monotonic() - started
        out.text_parts.append(data)
        sink.text(data)

    parser = adapter.new_parser()

    def on_line(line):
        for ev in parser.feed(line):
            handle(ev)

    try:
        inv = adapter.invocation(model, effort, req.system or cfg.system_prompt, req.prompt, scratch)
        try:
            out.returncode, stderr = process.run(
                inv,
                deadline=started + cfg.timeout,
                on_line=on_line,
                poll=sink.tick,
                max_stdout=cfg.max_output_bytes * 8 + 4 * 1024 * 1024,
                registry=procs,
            )
            for ev in parser.finish(out.returncode, stderr):
                handle(ev)
        except ClientGone:
            out.client_gone = True
        except _Done:
            pass
        except process.Timeout:
            out.error = errors.timeout(
                f"{adapter.title} did not finish within {cfg.timeout:g} s; the process group was killed"
            )
        except process.OutputTooLarge:
            out.error = errors.cli_failed(f"{adapter.title} produced too much output; stopped", code="output_too_large")
        except OSError as err:
            out.error = errors.cli_failed(f"cannot start {adapter.title}: {err.strerror or err}", code="cli_unavailable")
        if out.error is None and out.finish is None and not out.client_gone:
            out.error = errors.cli_failed(f"{adapter.title} ended without an answer")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        out.leftovers = _clean_work_dir(adapter.work_dir)
        if out.leftovers:
            log(f"warning: {adapter.name} left files in its working directory, removed: {', '.join(out.leftovers[:10])}")

    if out.error is not None and out.error.code == "not_logged_in":
        adapter.note_login(False)
    elif out.error is None and not out.client_gone:
        adapter.note_login(True)
    return out


def _clean_work_dir(path: str) -> list:
    try:
        names = sorted(os.listdir(path))
    except OSError:
        return []
    for name in names:
        full = os.path.join(path, name)
        if os.path.isdir(full) and not os.path.islink(full):
            shutil.rmtree(full, ignore_errors=True)
        else:
            try:
                os.unlink(full)
            except OSError:
                pass
    return names
