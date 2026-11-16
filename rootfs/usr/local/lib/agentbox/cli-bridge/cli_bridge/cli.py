"""Command line: serve, status, login, check."""

from __future__ import annotations

import os
import shutil
import signal
import sys
import threading

from . import adapters, config
from .server import BridgeServer, log

EX_CONFIG = 78  # the s6 finish script marks the service down on this code


def _load(require_token: bool):
    return config.load(require_token=require_token, adapter_names=adapters.names())


def _prepare_runtime(cfg) -> None:
    os.makedirs(cfg.state_dir, mode=0o700, exist_ok=True)
    os.chmod(cfg.state_dir, 0o700)
    os.makedirs(cfg.runtime_dir, mode=0o700, exist_ok=True)
    os.chmod(cfg.runtime_dir, 0o700)
    # Leftovers of a crash: the work directory must start empty.
    for sub in ("calls", "work", "tmp"):
        shutil.rmtree(os.path.join(cfg.runtime_dir, sub), ignore_errors=True)
    os.makedirs(os.path.join(cfg.runtime_dir, "calls"), mode=0o700)


def serve() -> int:
    try:
        cfg = _load(require_token=True)
    except config.ConfigError as err:
        log(f"refusing to start: {err}")
        return EX_CONFIG
    if not cfg.enabled:
        log("AGENTBOX_CLI_BRIDGE is not 1; not starting")
        return EX_CONFIG
    registry = adapters.Registry(cfg)
    _prepare_runtime(cfg)
    enabled = registry.enabled()
    for adapter in enabled:
        adapter.prepare()
    if not enabled:
        log("warning: no adapter is enabled (AGENTBOX_CLI_BRIDGE_CLAUDE=1, AGENTBOX_CLI_BRIDGE_CODEX=1); "
            "every request gets 404")
    try:
        server = BridgeServer(cfg, registry)
    except OSError as err:
        log(f"cannot listen on {cfg.bind}:{cfg.port}: {err.strerror or err}")
        return 1

    def stop(signum, _frame):
        log(f"signal {signum}: stopping")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log(
        f"listening on {cfg.bind}:{cfg.port}; adapters: "
        + (", ".join(f"{a.name} ({a.concurrency} at a time)" for a in enabled) or "none")
        + f"; queue {cfg.queue} per adapter, timeout {cfg.timeout:g} s"
        + ("; DEBUG logs prompts and answers" if cfg.debug else "")
    )

    def report():
        for adapter in enabled:
            state = adapter.login_state()
            login = {True: "logged in", False: f"NOT logged in (agentbox cli-bridge login {adapter.name})",
                     None: "login state unknown"}[state]
            log(f"{adapter.name}: {adapter.version()}, {login}; models: {', '.join(adapter.model_ids())}")

    threading.Thread(target=report, name="startup-report", daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.procs.kill_all()
        server.server_close()
    log("stopped")
    return 0


def status() -> int:
    try:
        cfg = _load(require_token=False)
    except config.ConfigError as err:
        print(f"configuration error: {err}")
        return EX_CONFIG
    state = "enabled" if cfg.enabled else "disabled (AGENTBOX_CLI_BRIDGE=1 enables it)"
    print(f"cli-bridge {state}, {cfg.bind}:{cfg.port}, state in {cfg.state_dir}")
    registry = adapters.Registry(cfg)
    for name, adapter in registry.adapters.items():
        if not adapter.enabled:
            print(f"  {name:<7} off (AGENTBOX_CLI_BRIDGE_{name.upper()}=1)")
            continue
        adapter.prepare()
        login = {True: "logged in", False: f"not logged in: agentbox cli-bridge login {name}",
                 None: "login check failed"}[adapter.logged_in(0)]
        print(f"  {name:<7} {adapter.version()}  ({login})")
        print(f"          models: {', '.join(adapter.model_ids())}")
    return 0


def login(name: str | None) -> int:
    try:
        cfg = _load(require_token=False)
    except config.ConfigError as err:
        print(f"configuration error: {err}")
        return EX_CONFIG
    registry = adapters.Registry(cfg)
    adapter = registry.adapters.get(name or "")
    if adapter is None:
        print(f"usage: agentbox cli-bridge login {'|'.join(registry.adapters)}")
        return 2
    adapter.prepare()
    argv = adapter.login_argv()
    print(f"Logging in the bridge's own {adapter.title} (config in {adapter.state_dir}).")
    print("This is a separate login from your interactive one; use the same account.")
    os.chdir(adapter.work_dir)
    os.execvpe(argv[0], argv, adapter.env(adapter.misc_tmp))
    return 1  # not reached


def main(argv: list) -> int:
    cmd = argv[0] if argv else ""
    if cmd == "serve":
        return serve()
    if cmd == "status":
        return status()
    if cmd == "login":
        return login(argv[1] if len(argv) > 1 else None)
    if cmd == "check":
        from . import check

        return check.main(argv[1:])
    print("usage: main.py serve | status | login ADAPTER | check [MODEL ...]", file=sys.stderr)
    return 2
