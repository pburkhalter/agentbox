"""Settings, read once from the environment. Everything is off by default."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

PREFIX = "AGENTBOX_CLI_BRIDGE"
MIN_TOKEN_LENGTH = 32

# Model names are passed to the CLIs as one argument each, never through a
# shell; this pattern also keeps them from looking like an option.
MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,99}$")

DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."


class ConfigError(Exception):
    """The configuration cannot work; the service refuses to start."""


@dataclass(frozen=True)
class Config:
    enabled: bool = False
    bind: str = "0.0.0.0"
    port: int = 3002
    token: str = field(default="", repr=False)
    adapters: dict = field(default_factory=dict)  # name -> enabled
    models: dict = field(default_factory=dict)  # name -> tuple, or None for the adapter's default
    adapter_concurrency: dict = field(default_factory=dict)  # name -> int, or None for the adapter's default
    timeout: float = 300.0
    concurrency: int = 2
    queue: int = 4
    queue_timeout: float = 30.0
    max_request_bytes: int = 2 * 1024 * 1024
    max_output_bytes: int = 1024 * 1024
    max_connections: int = 32
    debug: bool = False
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    state_dir: str = ""  # persistent: the bridge's own CLI logins and state
    runtime_dir: str = ""  # tmpfs: work directory, per-call scratch, HOME

    def adapter_enabled(self, name: str) -> bool:
        return bool(self.adapters.get(name))


def _get(env, name: str) -> str | None:
    value = env.get(f"{PREFIX}_{name}" if name else PREFIX)
    return None if value is None else value.strip()


def _bool(env, name: str, default: bool = False) -> bool:
    value = _get(env, name)
    if value is None or value == "":
        return default
    if value.lower() in ("1", "true", "yes", "on"):
        return True
    if value.lower() in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{PREFIX}_{name}={value!r} is not a boolean (use 1 or 0)")


def _number(env, name: str, default, low, high, kind=int):
    value = _get(env, name)
    if value is None or value == "":
        return default
    try:
        number = kind(value)
    except ValueError:
        raise ConfigError(f"{PREFIX}_{name}={value!r} is not a number") from None
    if not low <= number <= high:
        raise ConfigError(f"{PREFIX}_{name}={value} is outside {low}..{high}")
    return number


def _models(env, name: str, default):
    value = _get(env, name)
    if value is None or value == "":
        return default
    models = tuple(m.strip() for m in value.split(",") if m.strip())
    for model in models:
        if not MODEL_NAME.match(model):
            raise ConfigError(f"{PREFIX}_{name}: {model!r} is not a valid model name")
    return models


def _token(env) -> str:
    direct = env.get(f"{PREFIX}_TOKEN")
    path = _get(env, "TOKEN_FILE")
    if direct and path:
        raise ConfigError(f"set {PREFIX}_TOKEN or {PREFIX}_TOKEN_FILE, not both")
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                token = fh.read().strip()
        except OSError as err:
            raise ConfigError(f"cannot read {PREFIX}_TOKEN_FILE {path}: {err.strerror}") from None
    else:
        token = (direct or "").strip()
    if not token:
        raise ConfigError(
            f"no token: set {PREFIX}_TOKEN or {PREFIX}_TOKEN_FILE (e.g. openssl rand -hex 32); "
            "the bridge does not run without one"
        )
    if len(token) < MIN_TOKEN_LENGTH or any(c.isspace() for c in token) or not token.isprintable():
        raise ConfigError(
            f"the token must be at least {MIN_TOKEN_LENGTH} printable characters without spaces"
        )
    return token


def load(env=None, require_token: bool = True, adapter_names=("claude", "codex")) -> Config:
    """Reads the configuration; raises ConfigError for anything unusable.
    Per adapter NAME: AGENTBOX_CLI_BRIDGE_<NAME> (on/off, default off) and
    AGENTBOX_CLI_BRIDGE_<NAME>_MODELS (comma-separated model names) and
    AGENTBOX_CLI_BRIDGE_<NAME>_CONCURRENCY (calls at a time for it)."""
    env = os.environ if env is None else env
    home = env.get("HOME") or "/home/agent"
    runtime_root = env.get("AGENTBOX_RUNTIME") or "/tmp/agentbox"
    cfg = Config(
        enabled=_bool(env, ""),
        bind=_get(env, "BIND") or "0.0.0.0",
        port=_number(env, "PORT", 3002, 1, 65535),
        token=_token(env) if require_token else "",
        adapters={name: _bool(env, name.upper()) for name in adapter_names},
        models={name: _models(env, f"{name.upper()}_MODELS", None) for name in adapter_names},
        adapter_concurrency={name: _number(env, f"{name.upper()}_CONCURRENCY", None, 1, 16) for name in adapter_names},
        timeout=_number(env, "TIMEOUT", 300.0, 5, 3600, float),
        concurrency=_number(env, "CONCURRENCY", 2, 1, 16),
        queue=_number(env, "QUEUE", 4, 0, 64),
        queue_timeout=_number(env, "QUEUE_TIMEOUT", 30.0, 1, 600, float),
        max_request_bytes=_number(env, "MAX_REQUEST_BYTES", 2 * 1024 * 1024, 1024, 64 * 1024 * 1024),
        max_output_bytes=_number(env, "MAX_OUTPUT_BYTES", 1024 * 1024, 1024, 64 * 1024 * 1024),
        max_connections=_number(env, "MAX_CONNECTIONS", 32, 2, 1024),
        debug=_bool(env, "DEBUG"),
        system_prompt=env.get(f"{PREFIX}_SYSTEM_PROMPT") or DEFAULT_SYSTEM_PROMPT,
        state_dir=_get(env, "STATE_DIR") or os.path.join(home, ".local/state/agentbox/cli-bridge"),
        runtime_dir=os.path.join(runtime_root, "cli-bridge"),
    )
    return cfg
