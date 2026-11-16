"""The adapter interface: what a CLI needs to serve as a plain language model.

An adapter is one module in this package that defines a subclass of Adapter
and names it ``ADAPTER``; the registry finds it by itself, and the
environment switch AGENTBOX_CLI_BRIDGE_<NAME> (off by default) enables it.
A subclass supplies:

- ``build_argv``: the command line (argument vector, never a shell string)
  that runs the CLI with no tools, MCP, skills, hooks or memory, reads the
  prompt from stdin and prints machine-readable events;
- ``adapter_env``: environment variables of its own (config directory ...);
- ``new_parser``: a Parser that turns the CLI's output lines into events;
- ``status_argv`` and ``login_argv``: the CLI's login check and login.

Every CLI runs with its own config directory under the bridge's state
directory (a separate login, nothing shared with the interactive sessions),
HOME and TMPDIR on the bridge's tmpfs, an empty working directory, and an
environment built from an allowlist that never carries an API key.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass

from .. import errors
from ..openai_format import Usage
from ..process import Invocation

# Copied from the bridge's own environment when set; nothing else is.
PASSTHROUGH = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "LD_PRELOAD",
    "NSS_WRAPPER_PASSWD",
    "NSS_WRAPPER_GROUP",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "all_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
    "DISABLE_AUTOUPDATER",
    "DISABLE_UPDATES",
    "NPM_CONFIG_UPDATE_NOTIFIER",
)

# Each would switch a CLI from the subscription login to API billing or
# another backend. They are never passed, whatever an adapter asks for.
FORBIDDEN_NAMES = {
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CODEX_API_KEY",
    "CLAUDE_CODE_SIMPLE",
}
FORBIDDEN_PREFIXES = ("ANTHROPIC_", "OPENAI_", "CLAUDE_CODE_USE_", "AWS_", "GOOGLE_", "AZURE_")


def forbidden_env(name: str) -> bool:
    return name in FORBIDDEN_NAMES or name.startswith(FORBIDDEN_PREFIXES)


# --- parser events -----------------------------------------------------------


@dataclass
class Text:
    text: str


@dataclass
class UsageEvent:
    usage: Usage


@dataclass
class Finish:
    reason: str  # "stop" or "length"


@dataclass
class Fail:
    error: errors.BridgeError


class Parser:
    """Turns a CLI's stdout lines into events. One instance per call."""

    def __init__(self):
        self.done = False  # a final result (success or failure) was seen
        self.non_json = 0
        self.plain = []  # the last lines that were not JSON (a CLI's own error text)

    def feed(self, line: bytes) -> list:
        try:
            obj = json.loads(line)
        except ValueError:
            self.non_json += 1
            self.plain = (self.plain + [line.decode("utf-8", "replace").strip()])[-5:]
            return []
        if not isinstance(obj, dict):
            return []
        return self.handle(obj)

    def handle(self, obj: dict) -> list:
        raise NotImplementedError

    def finish(self, returncode: int, stderr: str) -> list:
        """Events after the process exited (usually a Fail when no result came)."""
        raise NotImplementedError


def last_line(text: str, limit: int = 300) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][:limit] if lines else ""


# --- adapters -----------------------------------------------------------------


class Adapter:
    name = ""  # model prefix, env switch AGENTBOX_CLI_BRIDGE_<NAME>, state subdirectory
    title = ""
    owned_by = ""
    default_models: tuple | None = ()  # None: discover_models()
    efforts: tuple = ()  # accepted reasoning_effort values
    default_concurrency: int | None = None  # None: AGENTBOX_CLI_BRIDGE_CONCURRENCY

    def __init__(self, cfg, environ=None):
        self.cfg = cfg
        self.environ = os.environ if environ is None else environ
        self.state_dir = os.path.join(cfg.state_dir, self.name)
        self.home_dir = os.path.join(cfg.runtime_dir, "home")
        self.work_dir = os.path.join(cfg.runtime_dir, "work")
        self.misc_tmp = os.path.join(cfg.runtime_dir, "tmp")
        self._login = None
        self._login_checked = 0.0
        self._login_lock = threading.Lock()
        self._version = None
        self._discovered = None
        self._discover_lock = threading.Lock()

    # --- to implement ----------------------------------------------------------

    def adapter_env(self, tmpdir: str) -> dict:
        return {}

    def status_argv(self) -> list:
        raise NotImplementedError

    def login_argv(self) -> list:
        raise NotImplementedError

    def version_argv(self) -> list:
        raise NotImplementedError

    def build_argv(self, model: str | None, effort: str | None, system_file: str) -> list:
        raise NotImplementedError

    def new_parser(self) -> Parser:
        raise NotImplementedError

    def discover_models(self) -> tuple:
        return ()

    # --- provided --------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self.cfg.adapter_enabled(self.name)

    @property
    def concurrency(self) -> int:
        configured = self.cfg.adapter_concurrency.get(self.name)
        return configured or self.default_concurrency or self.cfg.concurrency

    def prepare(self) -> None:
        for path in (self.state_dir, self.home_dir, self.work_dir, self.misc_tmp):
            os.makedirs(path, mode=0o700, exist_ok=True)

    def env(self, tmpdir: str) -> dict:
        env = {k: self.environ[k] for k in PASSTHROUGH if self.environ.get(k)}
        env["HOME"] = self.home_dir
        env["TMPDIR"] = tmpdir
        env.update(self.adapter_env(tmpdir))
        bad = sorted(k for k in env if forbidden_env(k))
        if bad:
            raise RuntimeError(f"refusing to pass {', '.join(bad)} to {self.name}")
        return env

    def models(self) -> tuple:
        configured = self.cfg.models.get(self.name)
        if configured is not None:
            return configured
        if self.default_models is not None:
            return self.default_models
        with self._discover_lock:
            if self._discovered is None:
                try:
                    self._discovered = tuple(self.discover_models())
                except Exception:  # noqa: BLE001 - a failed discovery only shortens the list
                    self._discovered = ()
            return self._discovered

    def model_ids(self) -> list:
        return [self.name] + [f"{self.name}/{m}" for m in self.models()]

    def check_effort(self, effort: str | None) -> str | None:
        if effort is None:
            return None
        if effort not in self.efforts:
            raise errors.unsupported(
                f"reasoning_effort {effort!r} is not available for {self.name}; use one of: {', '.join(self.efforts)}",
                "reasoning_effort",
            )
        return effort

    def run_quietly(self, argv: list, timeout: float = 30.0):
        """Runs a short CLI command (status, version, model list) with the
        adapter's environment. Returns CompletedProcess or None."""
        try:
            return subprocess.run(
                argv,
                cwd=self.work_dir,
                env=self.env(self.misc_tmp),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
                start_new_session=True,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None

    def logged_in(self, max_age: float) -> bool | None:
        """Cached login state (None: unknown, the check itself failed)."""
        with self._login_lock:
            if self._login is None or time.monotonic() - self._login_checked > max_age:
                result = self.run_quietly(self.status_argv())
                self._login = None if result is None else result.returncode == 0
                self._login_checked = time.monotonic()
            return self._login

    def login_state(self) -> bool | None:
        """Login state for requests: a known login is re-checked every two
        minutes, a missing one every ten seconds (so a fresh login counts
        almost at once)."""
        return self.logged_in(120.0 if self._login else 10.0)

    def note_login(self, value: bool) -> None:
        with self._login_lock:
            self._login = value
            self._login_checked = time.monotonic()

    def version(self) -> str:
        if self._version is None:
            result = self.run_quietly(self.version_argv())
            out = result.stdout.decode("utf-8", "replace").strip() if result else ""
            self._version = out.splitlines()[0] if out else "unknown"
        return self._version

    def invocation(self, model: str | None, effort: str | None, system: str, prompt: str, scratch: str) -> Invocation:
        tmpdir = os.path.join(scratch, "tmp")
        os.makedirs(tmpdir, mode=0o700, exist_ok=True)
        system_file = os.path.join(scratch, "system-prompt.txt")
        fd = os.open(system_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(system)
        return Invocation(
            argv=self.build_argv(model, effort, system_file),
            env=self.env(tmpdir),
            cwd=self.work_dir,
            stdin=prompt.encode("utf-8"),
        )
