"""Codex (``codex exec``) as a plain language model.

Verified against Codex 0.169.0 (``codex exec --help``, ``codex features
list``, ``codex debug prompt-input`` for what the model sees, its source at
tag rust-v0.169.0) and https://developers.openai.com/codex/noninteractive and
config-reference:

  exec --json - --ephemeral   JSON lines, prompt on stdin, no rollout files
  --skip-git-repo-check       the empty working directory is no repository
  --ignore-user-config        $CODEX_HOME/config.toml is not read (the
                              image's /etc/codex/config.toml still is)
  --ignore-rules              no execpolicy .rules files
  --sandbox read-only, -c approval_policy="never"
                              no writes, and nothing waits for an approval
  -c model_instructions_file  the caller's system prompt replaces Codex's
                              built-in agent instructions
  -c web_search="disabled", tools.update_plan / tools.experimental_request_
     user_input disabled, agents.enabled=false, skills off, project docs off,
     permission/apps/collaboration/environment instructions off
  --disable shell_tool unified_exec ... (see DISABLED_FEATURES)

Codex has its own CODEX_HOME under the bridge's state directory: the global
AGENTS.md, skills, plugins and MCP servers of the interactive Codex are not
there, and Codex has no switch for the global AGENTS.md. It also gets its
own login, and its calls run one at a time by default: OpenAI's CI/CD auth
guide says not to share one auth.json between concurrent jobs.

What cannot be switched off in 0.169.0: the ``apply_patch`` tool and, for
models the catalog marks code_mode_only, the JavaScript ``exec`` cell (a V8
isolate without file system or network). Both come from the model catalog.
With no writable path and approval_policy "never", Codex rejects every patch
(assess_patch_safety), and any tool item in the event stream stops the call.

``codex exec --json`` reports an answer only as a completed message:
streaming delivers it in one piece, not token by token.
"""

from __future__ import annotations

import json

from .. import errors
from ..config import MODEL_NAME
from ..openai_format import Usage
from .base import Adapter, Fail, Finish, Parser, Text, UsageEvent, last_line

DISABLED_FEATURES = (
    "shell_tool",
    "unified_exec",
    "shell_snapshot",
    "multi_agent",
    "apps",
    "plugins",
    "hooks",
    "memories",
    "view_image",
    "image_generation",
    "browser_use",
    "browser_use_external",
    "computer_use",
    "goals",
    "tool_suggest",
    "skill_search",
    "sleep_tool",
)

CONFIG = (
    'approval_policy="never"',
    'history.persistence="none"',
    "check_for_update_on_startup=false",
    'web_search="disabled"',
    "project_doc_max_bytes=0",
    "skills.bundled.enabled=false",
    "skills.include_instructions=false",
    "include_permissions_instructions=false",
    "include_apps_instructions=false",
    "include_environment_context=false",
    "include_collaboration_mode_instructions=false",
    "agents.enabled=false",
    "tools.update_plan.enabled=false",
    "tools.experimental_request_user_input.enabled=false",
)

# Item types an answer consists of. Anything else is a tool call.
ANSWER_ITEMS = {"agent_message", "reasoning", "error"}

LOGIN_HINT = "log the bridge in once with `agentbox cli-bridge login codex`"


def toml_string(value: str) -> str:
    # A JSON string is a valid TOML basic string.
    return json.dumps(value)


def classify(message: str) -> errors.BridgeError:
    text = (message or "unknown error")[:500]
    low = text.lower()
    if "usage limit" in low:
        return errors.usage_limit(f"Codex usage limit reached: {text}")
    if "quota exceeded" in low:
        return errors.usage_limit(f"Codex: {text}", code="insufficient_quota")
    if "upgrade to plus" in low:
        return errors.forbidden(f"Codex: {text}")
    if any(s in low for s in ("401", "unauthorized", "not logged in", "log in again", "sign in again",
                              "refresh token", "token_expired", "invalid_grant")):
        return errors.not_logged_in(f"Codex is not logged in ({text}); {LOGIN_HINT}")
    if any(s in low for s in ("rate limit", "429", "too many requests")):
        return errors.usage_limit(f"Codex: {text}", code="rate_limit_exceeded")
    if any(s in low for s in ("at capacity", "high demand", "503", "overloaded")):
        return errors.overloaded(f"Codex: {text}")
    if "context window" in low:
        return errors.invalid_request(f"Codex: {text}", code="context_length_exceeded")
    if "model" in low and any(s in low for s in ("not supported", "does not exist", "not found")):
        return errors.model_not_found(f"Codex: {text}")
    return errors.cli_failed(f"Codex failed: {text}")


class CodexParser(Parser):
    def __init__(self):
        super().__init__()
        self.messages = 0
        self.last_error = ""

    def handle(self, m: dict) -> list:
        kind = m.get("type")
        if kind in ("item.started", "item.updated", "item.completed"):
            item = m.get("item") or {}
            itype = item.get("type")
            if itype not in ANSWER_ITEMS:
                return [Fail(errors.isolation_violation(f"Codex attempted a tool call ({itype}); call stopped."))]
            if kind == "item.completed" and itype == "agent_message":
                text = item.get("text") or ""
                if not text:
                    return []
                if self.messages:
                    text = "\n\n" + text
                self.messages += 1
                return [Text(text)]
            return []
        if kind == "turn.completed":
            self.done = True
            u = m.get("usage") or {}
            usage = Usage(
                prompt_tokens=int(u.get("input_tokens") or 0),
                completion_tokens=int(u.get("output_tokens") or 0),
                cached_tokens=int(u.get("cached_input_tokens") or 0),
                reasoning_tokens=int(u.get("reasoning_output_tokens") or 0),
                reported=bool(u),
            )
            return [UsageEvent(usage), Finish("stop")]
        if kind == "turn.failed":
            self.done = True
            return [Fail(classify((m.get("error") or {}).get("message") or self.last_error or "turn failed"))]
        if kind == "error":
            self.last_error = m.get("message") or self.last_error
        return []

    def finish(self, returncode: int, stderr: str) -> list:
        if self.done:
            return []
        message = self.last_error or last_line("\n".join(self.plain + [stderr]))
        if message:
            return [Fail(classify(message))]
        return [Fail(errors.cli_failed(f"codex exited with status {returncode} without a result"))]


class CodexAdapter(Adapter):
    name = "codex"
    title = "Codex"
    owned_by = "openai"
    default_models = None  # the pinned Codex's model catalog
    efforts = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
    # OpenAI's CI/CD auth guide: one auth.json per runner or per serialized
    # workflow stream, never shared across concurrent jobs.
    default_concurrency = 1

    def adapter_env(self, tmpdir: str) -> dict:
        return {"CODEX_HOME": self.state_dir}

    def status_argv(self) -> list:
        return ["codex", "login", "status"]

    def login_argv(self) -> list:
        return ["codex", "login", "--device-auth"]

    def version_argv(self) -> list:
        return ["codex", "--version"]

    def build_argv(self, model, effort, system_file) -> list:
        argv = [
            "codex", "exec", "--json", "--ephemeral",
            "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules",
            "--sandbox", "read-only", "--color", "never",
        ]
        for item in CONFIG:
            argv += ["-c", item]
        argv += ["-c", f"model_instructions_file={toml_string(system_file)}"]
        for feature in DISABLED_FEATURES:
            argv += ["--disable", feature]
        if model:
            argv += ["--model", model]
        if effort:
            argv += ["-c", f"model_reasoning_effort={toml_string(effort)}"]
        argv.append("-")
        return argv

    def new_parser(self) -> Parser:
        return CodexParser()

    def discover_models(self) -> tuple:
        """The models the pinned Codex lists in its picker."""
        result = self.run_quietly(["codex", "debug", "models"], timeout=60)
        if result is None or result.returncode != 0:
            return ()
        catalog = json.loads(result.stdout)
        models = catalog.get("models", []) if isinstance(catalog, dict) else []
        return tuple(
            m["slug"] for m in models
            if isinstance(m, dict) and m.get("visibility") == "list"
            and isinstance(m.get("slug"), str) and MODEL_NAME.match(m["slug"])
        )


ADAPTER = CodexAdapter
