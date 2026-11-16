"""Claude Code in print mode (``claude -p``) as a plain language model.

Verified against Claude Code 2.1.324 (``claude --help``, and the hidden
options in its option table) and https://code.claude.com/docs/en/cli-reference.md,
headless.md, env-vars.md and claude-directory.md:

  -p --output-format stream-json --verbose --include-partial-messages
                              events as JSON lines, text deltas included
  --no-session-persistence    no transcript on disk (print mode only)
  --safe-mode                 no CLAUDE.md, skills, plugins, hooks, MCP
                              servers, custom commands/agents, output styles,
                              auto memory; auth and permissions as usual
  --setting-sources ""        no user, project or local settings (an empty
                              list is accepted; managed settings still apply)
  --strict-mcp-config         no MCP servers except --mcp-config (none given)
  --disable-slash-commands    no skills or commands
  --permission-mode dontAsk   anything that would ask is denied
  --max-turns 1               one model turn (hidden option, print mode)
  --system-prompt-file F      replaces Claude Code's whole system prompt
                              (hidden option; the file avoids argv limits)
  --tools "" --disallowedTools "*"
                              no built-in tools at all, none from MCP

``--bare`` is not used: it never reads OAuth or keychain credentials, so it
cannot use a subscription login. No ANTHROPIC_* variable is ever passed: an
ANTHROPIC_API_KEY would silently switch billing to the API.

The system/init event must report no tools and no MCP servers, or the call
is stopped before the model answers.
"""

from __future__ import annotations

import time

from .. import errors
from ..openai_format import Usage
from .base import Adapter, Fail, Finish, Parser, Text, UsageEvent, last_line

TOOL_BLOCKS = {"tool_use", "server_tool_use", "mcp_tool_use"}
AUTH_ERRORS = {"authentication_failed", "oauth_org_not_allowed"}
ACCOUNT_ERRORS = {"account_on_hold", "verification_required"}

LOGIN_HINT = "log the bridge in once with `agentbox cli-bridge login claude`"


class ClaudeParser(Parser):
    def __init__(self):
        super().__init__()
        self.partial = False  # text arrives as stream_event deltas
        self.text_seen = False
        self.max_tokens = False
        self.api_error = None
        self.resets_at = None
        self.retries = 0

    def _text(self, text: str) -> list:
        if not text:
            return []
        self.text_seen = True
        return [Text(text)]

    def handle(self, m: dict) -> list:
        kind = m.get("type")
        if kind == "system":
            sub = m.get("subtype")
            if sub == "init":
                tools = m.get("tools") or []
                servers = [s.get("name", "?") if isinstance(s, dict) else str(s) for s in m.get("mcp_servers") or []]
                if tools or servers:
                    what = ", ".join([f"tools {tools}"] * bool(tools) + [f"MCP servers {servers}"] * bool(servers))
                    return [Fail(errors.isolation_violation(
                        f"Claude Code started with {what}; the bridge only runs it without tools. Call stopped."
                    ))]
            elif sub == "api_retry":
                self.retries += 1
            return []
        if m.get("parent_tool_use_id"):
            return [Fail(errors.isolation_violation("Claude Code started a subagent; call stopped."))]
        if kind == "stream_event":
            ev = m.get("event") or {}
            etype = ev.get("type")
            if etype == "content_block_start" and (ev.get("content_block") or {}).get("type") in TOOL_BLOCKS:
                return [Fail(errors.isolation_violation("Claude Code attempted a tool call; call stopped."))]
            if etype == "content_block_delta":
                delta = ev.get("delta") or {}
                if delta.get("type") == "text_delta":
                    self.partial = True
                    return self._text(delta.get("text") or "")
            elif etype == "message_delta" and (ev.get("delta") or {}).get("stop_reason") == "max_tokens":
                self.max_tokens = True
            return []
        if kind == "assistant":
            if m.get("error"):
                self.api_error = m["error"]
                if self.api_error == "max_output_tokens":
                    self.max_tokens = True
                return []
            out = []
            for block in (m.get("message") or {}).get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") in TOOL_BLOCKS:
                    return [Fail(errors.isolation_violation("Claude Code attempted a tool call; call stopped."))]
                if block.get("type") == "text" and not self.partial:
                    out += self._text(block.get("text") or "")
            return out
        if kind == "rate_limit_event":
            info = m.get("rate_limit_info") or {}
            if info.get("status") == "rejected" and isinstance(info.get("resetsAt"), (int, float)):
                self.resets_at = info["resetsAt"]
            return []
        if kind == "result":
            self.done = True
            if m.get("is_error") or m.get("subtype") != "success":
                return [Fail(self.classify(m))]
            out = [] if self.text_seen else self._text(m.get("result") or "")
            u = m.get("usage") or {}
            cache_read = int(u.get("cache_read_input_tokens") or 0)
            usage = Usage(
                prompt_tokens=int(u.get("input_tokens") or 0) + cache_read + int(u.get("cache_creation_input_tokens") or 0),
                completion_tokens=int(u.get("output_tokens") or 0),
                cached_tokens=cache_read,
                reported=bool(u),
            )
            stop = "length" if self.max_tokens or m.get("stop_reason") == "max_tokens" else "stop"
            return out + [UsageEvent(usage), Finish(stop)]
        return []

    def classify(self, m: dict) -> errors.BridgeError:
        text = m.get("result") if isinstance(m.get("result"), str) else ""
        if not text and m.get("errors"):
            text = "; ".join(str(e) for e in m["errors"])
        text = (text or m.get("subtype") or "unknown error")[:500]
        low = text.lower()
        api_error = self.api_error or m.get("api_error")
        if api_error in AUTH_ERRORS or "not logged in" in low or "/login" in low or "invalid api key" in low:
            return errors.not_logged_in(f"Claude Code is not logged in ({text}); {LOGIN_HINT}")
        if api_error in ACCOUNT_ERRORS:
            return errors.forbidden(f"Claude account problem: {text}", code=api_error)
        if api_error == "billing_error":
            return errors.usage_limit(f"Claude: {text}", code="insufficient_quota")
        if api_error == "rate_limit" or self.resets_at or "usage limit" in low or "hit your limit" in low or "limit reached" in low:
            retry = int(max(1, self.resets_at - time.time())) if self.resets_at else None
            return errors.usage_limit(f"Claude usage limit reached: {text}", retry_after=retry)
        if api_error == "overloaded":
            return errors.overloaded(f"Claude is overloaded: {text}")
        if api_error == "model_not_found":
            return errors.model_not_found(f"Claude: {text}")
        if api_error == "invalid_request":
            return errors.invalid_request(f"Claude rejected the request: {text}", code="upstream_invalid_request")
        if m.get("subtype") == "error_max_turns":
            return errors.cli_failed("Claude Code wanted more than one turn (it should never need a tool here)")
        return errors.cli_failed(f"Claude Code failed: {text}")

    def finish(self, returncode: int, stderr: str) -> list:
        if self.done:
            return []
        output = "\n".join(self.plain + [stderr])
        tail = last_line(output)
        if "not logged in" in output.lower() or "/login" in output:
            return [Fail(errors.not_logged_in(f"Claude Code is not logged in; {LOGIN_HINT}"))]
        return [Fail(errors.cli_failed(f"claude exited with status {returncode} without a result" + (f": {tail}" if tail else "")))]


class ClaudeAdapter(Adapter):
    name = "claude"
    title = "Claude Code"
    owned_by = "anthropic"
    default_models = ("sonnet", "opus", "haiku")
    efforts = ("low", "medium", "high", "xhigh", "max")

    def adapter_env(self, tmpdir: str) -> dict:
        return {
            "CLAUDE_CONFIG_DIR": self.state_dir,
            "CLAUDE_CODE_TMPDIR": tmpdir,
            "CLAUDE_CODE_SKIP_PROMPT_HISTORY": "1",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
            "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
            "CLAUDE_CODE_DISABLE_FILE_CHECKPOINTING": "1",
            # Also skips the background request that titles a session.
            "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1",
            "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_UPDATES": "1",
        }

    def status_argv(self) -> list:
        return ["claude", "auth", "status"]

    def login_argv(self) -> list:
        return ["claude", "auth", "login"]

    def version_argv(self) -> list:
        return ["claude", "--version"]

    def build_argv(self, model, effort, system_file) -> list:
        argv = [
            "claude", "-p",
            "--output-format", "stream-json", "--verbose", "--include-partial-messages",
            "--no-session-persistence",
            "--safe-mode",
            "--setting-sources", "",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--permission-mode", "dontAsk",
            "--max-turns", "1",
            "--system-prompt-file", system_file,
        ]
        if model:
            argv += ["--model", model]
        if effort:
            argv += ["--effort", effort]
        # Variadic options last, so nothing after them is read as a tool name.
        argv += ["--tools", "", "--disallowedTools", "*"]
        return argv

    def new_parser(self) -> Parser:
        return ClaudeParser()


ADAPTER = ClaudeAdapter
