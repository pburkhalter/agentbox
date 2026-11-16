"""Adapters: command lines, environment, event parsers, model resolution, config."""

import json
import os
import tempfile
import time
import unittest

import context  # noqa: F401
from cli_bridge import adapters, config
from cli_bridge.adapters.base import Adapter, Fail, Finish, Text, UsageEvent
from cli_bridge.adapters.claude import ClaudeParser
from cli_bridge.adapters.codex import CodexParser
from cli_bridge.errors import BridgeError

TOKEN = "t" * 40


def make_cfg(tmp, **env):
    base = {"HOME": tmp, "AGENTBOX_RUNTIME": os.path.join(tmp, "rt"), "AGENTBOX_CLI_BRIDGE": "1",
            "AGENTBOX_CLI_BRIDGE_TOKEN": TOKEN}
    base.update(env)
    return config.load(base, adapter_names=adapters.names())


def feed(parser, *objs):
    events = []
    for obj in objs:
        events += parser.feed(json.dumps(obj).encode())
    return events


class Config(unittest.TestCase):
    def test_everything_off_by_default(self):
        cfg = config.load({"HOME": "/h"}, require_token=False, adapter_names=adapters.names())
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.adapters, {"claude": False, "codex": False})
        self.assertEqual((cfg.bind, cfg.port), ("0.0.0.0", 3002))
        self.assertEqual(cfg.state_dir, "/h/.local/state/agentbox/cli-bridge")

    def test_token_is_required_and_checked(self):
        with self.assertRaisesRegex(config.ConfigError, "no token"):
            config.load({"AGENTBOX_CLI_BRIDGE": "1"})
        with self.assertRaisesRegex(config.ConfigError, "at least 32"):
            config.load({"AGENTBOX_CLI_BRIDGE_TOKEN": "short"})
        with self.assertRaisesRegex(config.ConfigError, "at least 32"):
            config.load({"AGENTBOX_CLI_BRIDGE_TOKEN": "a" * 20 + " " + "b" * 20})
        with tempfile.NamedTemporaryFile("w", delete=False) as fh:
            fh.write(TOKEN + "\n")
        try:
            self.assertEqual(config.load({"AGENTBOX_CLI_BRIDGE_TOKEN_FILE": fh.name}).token, TOKEN)
            with self.assertRaisesRegex(config.ConfigError, "not both"):
                config.load({"AGENTBOX_CLI_BRIDGE_TOKEN_FILE": fh.name, "AGENTBOX_CLI_BRIDGE_TOKEN": TOKEN})
        finally:
            os.unlink(fh.name)
        with self.assertRaisesRegex(config.ConfigError, "cannot read"):
            config.load({"AGENTBOX_CLI_BRIDGE_TOKEN_FILE": "/nonexistent/token"})

    def test_token_not_in_repr(self):
        self.assertNotIn(TOKEN, repr(config.load({"AGENTBOX_CLI_BRIDGE_TOKEN": TOKEN})))

    def test_bad_values(self):
        for env in ({"AGENTBOX_CLI_BRIDGE": "maybe"}, {"AGENTBOX_CLI_BRIDGE_PORT": "0"},
                    {"AGENTBOX_CLI_BRIDGE_TIMEOUT": "abc"}, {"AGENTBOX_CLI_BRIDGE_CLAUDE_MODELS": "sonnet,-x"}):
            with self.subTest(env=env), self.assertRaises(config.ConfigError):
                config.load(env, require_token=False)


class CommandLines(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = make_cfg(self.tmp, AGENTBOX_CLI_BRIDGE_CLAUDE="1", AGENTBOX_CLI_BRIDGE_CODEX="1",
                            AGENTBOX_CLI_BRIDGE_CODEX_MODELS="gpt-x")
        env = {"PATH": "/usr/bin:/bin", "ANTHROPIC_API_KEY": "sk-ant", "OPENAI_API_KEY": "sk-oai",
               "CLAUDE_CODE_OAUTH_TOKEN": "x", "SECRET": "y", "LANG": "C.UTF-8"}
        self.reg = adapters.Registry(self.cfg, env)

    def test_claude_argv(self):
        argv = self.reg.adapters["claude"].build_argv("opus", "high", "/s.txt")
        self.assertEqual(argv[:2], ["claude", "-p"])
        for flags in (["--output-format", "stream-json"], ["--include-partial-messages"], ["--verbose"],
                      ["--no-session-persistence"], ["--safe-mode"], ["--setting-sources", ""],
                      ["--strict-mcp-config"], ["--disable-slash-commands"], ["--permission-mode", "dontAsk"],
                      ["--max-turns", "1"], ["--system-prompt-file", "/s.txt"], ["--model", "opus"],
                      ["--effort", "high"]):
            with self.subTest(flags=flags):
                i = argv.index(flags[0])
                self.assertEqual(argv[i:i + len(flags)], flags)
        self.assertEqual(argv[-4:], ["--tools", "", "--disallowedTools", "*"])
        self.assertNotIn("--bare", argv)

    def test_codex_argv(self):
        argv = self.reg.adapters["codex"].build_argv("gpt-x", "low", "/s.txt")
        self.assertEqual(argv[:4], ["codex", "exec", "--json", "--ephemeral"])
        for flag in ("--skip-git-repo-check", "--ignore-user-config", "--ignore-rules"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        configs = [argv[i + 1] for i, a in enumerate(argv) if a == "-c"]
        self.assertIn('model_instructions_file="/s.txt"', configs)
        self.assertIn('model_reasoning_effort="low"', configs)
        self.assertIn('approval_policy="never"', configs)
        self.assertIn('web_search="disabled"', configs)
        disabled = [argv[i + 1] for i, a in enumerate(argv) if a == "--disable"]
        self.assertIn("shell_tool", disabled)
        self.assertIn("unified_exec", disabled)
        self.assertEqual(argv[argv.index("--model") + 1], "gpt-x")
        self.assertEqual(argv[-1], "-")

    def test_environment_is_an_allowlist_without_keys(self):
        for name in ("claude", "codex"):
            a = self.reg.adapters[name]
            env = a.env("/tmp/x")
            with self.subTest(adapter=name):
                self.assertFalse([k for k in env if k.startswith(("ANTHROPIC_", "OPENAI_"))])
                self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
                self.assertNotIn("SECRET", env)
                self.assertEqual(env["PATH"], "/usr/bin:/bin")
                self.assertEqual(env["HOME"], os.path.join(self.cfg.runtime_dir, "home"))
                self.assertEqual(env["TMPDIR"], "/tmp/x")
        claude = self.reg.adapters["claude"].env("/tmp/x")
        self.assertEqual(claude["CLAUDE_CONFIG_DIR"], os.path.join(self.cfg.state_dir, "claude"))
        for k in ("CLAUDE_CODE_SKIP_PROMPT_HISTORY", "CLAUDE_CODE_DISABLE_AUTO_MEMORY", "CLAUDE_CODE_DISABLE_CLAUDE_MDS"):
            self.assertEqual(claude[k], "1")
        self.assertEqual(self.reg.adapters["codex"].env("/tmp/x")["CODEX_HOME"],
                         os.path.join(self.cfg.state_dir, "codex"))

    def test_an_adapter_cannot_smuggle_a_key(self):
        class Bad(Adapter):
            name = "bad"

            def adapter_env(self, tmpdir):
                return {"ANTHROPIC_API_KEY": "sk"}

        with self.assertRaisesRegex(RuntimeError, "ANTHROPIC_API_KEY"):
            Bad(self.cfg, {}).env("/tmp")

    def test_model_resolution(self):
        claude, model, mid = self.reg.resolve("claude")
        self.assertEqual((claude.name, model, mid), ("claude", None, "claude"))
        self.assertEqual(self.reg.resolve("claude/default")[1:], (None, "claude"))
        self.assertEqual(self.reg.resolve("claude/sonnet")[1:], ("sonnet", "claude/sonnet"))
        self.assertEqual(self.reg.resolve("codex/gpt-x")[1:], ("gpt-x", "codex/gpt-x"))
        for bad in ("gpt-4o", "claude/gpt-x", "codex/sonnet", "openai/claude"):
            with self.subTest(model=bad), self.assertRaises(BridgeError) as cm:
                self.reg.resolve(bad)
            self.assertEqual(cm.exception.status, 404)

    def test_disabled_adapter(self):
        reg = adapters.Registry(make_cfg(self.tmp), {})
        with self.assertRaisesRegex(BridgeError, "disabled"):
            reg.resolve("claude/sonnet")

    def test_concurrency(self):
        self.assertEqual(self.reg.adapters["claude"].concurrency, 2)
        self.assertEqual(self.reg.adapters["codex"].concurrency, 1)  # one auth.json, serialized
        reg = adapters.Registry(make_cfg(self.tmp, AGENTBOX_CLI_BRIDGE_CONCURRENCY="3",
                                         AGENTBOX_CLI_BRIDGE_CODEX_CONCURRENCY="2"), {})
        self.assertEqual((reg.adapters["claude"].concurrency, reg.adapters["codex"].concurrency), (3, 2))

    def test_effort(self):
        self.assertEqual(self.reg.adapters["claude"].check_effort("max"), "max")
        self.assertIsNone(self.reg.adapters["claude"].check_effort(None))
        with self.assertRaises(BridgeError):
            self.reg.adapters["claude"].check_effort("minimal")
        self.assertEqual(self.reg.adapters["codex"].check_effort("minimal"), "minimal")


class ClaudeEvents(unittest.TestCase):
    INIT = {"type": "system", "subtype": "init", "tools": [], "mcp_servers": []}

    def delta(self, text):
        return {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}}

    def result(self, **kw):
        r = {"type": "result", "subtype": "success", "is_error": False, "result": "Hello",
             "usage": {"input_tokens": 5, "cache_read_input_tokens": 7, "cache_creation_input_tokens": 1, "output_tokens": 2}}
        r.update(kw)
        return r

    def test_streamed_answer(self):
        p = ClaudeParser()
        ev = feed(p, self.INIT, self.delta("Hel"), self.delta("lo"),
                  {"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello"}]}}, self.result())
        self.assertEqual([e.text for e in ev if isinstance(e, Text)], ["Hel", "lo"])
        usage = [e.usage for e in ev if isinstance(e, UsageEvent)][0]
        self.assertEqual((usage.prompt_tokens, usage.completion_tokens, usage.cached_tokens), (13, 2, 7))
        self.assertEqual([e.reason for e in ev if isinstance(e, Finish)], ["stop"])
        self.assertEqual(p.finish(0, ""), [])

    def test_answer_without_partial_messages(self):
        ev = feed(ClaudeParser(), self.INIT, {"type": "assistant", "message": {"content": [{"type": "text", "text": "Hi"}]}},
                  self.result(result="Hi"))
        self.assertEqual([e.text for e in ev if isinstance(e, Text)], ["Hi"])

    def test_max_tokens_is_length(self):
        ev = feed(ClaudeParser(), self.INIT, self.delta("x"), self.result(stop_reason="max_tokens"))
        self.assertEqual(ev[-1], Finish("length"))

    def test_tools_or_mcp_at_init_stop_the_call(self):
        for init in ({**self.INIT, "tools": ["Bash"]}, {**self.INIT, "mcp_servers": [{"name": "x", "status": "connected"}]}):
            ev = feed(ClaudeParser(), init)
            self.assertEqual(ev[0].error.code, "isolation_violation")
        tool = {"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "tool_use"}}}
        self.assertEqual(feed(ClaudeParser(), self.INIT, tool)[0].error.code, "isolation_violation")

    def test_not_logged_in(self):
        ev = feed(ClaudeParser(), {"type": "assistant", "error": "authentication_failed", "message": {"content": []}},
                  self.result(is_error=True, result="Not logged in · Please run /login"))
        self.assertEqual((ev[-1].error.status, ev[-1].error.code), (401, "not_logged_in"))

    def test_usage_limit_with_retry_after(self):
        resets = int(time.time()) + 600
        ev = feed(ClaudeParser(), {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": resets}},
                  {"type": "assistant", "error": "rate_limit", "message": {"content": []}},
                  self.result(is_error=True, result="You've hit your limit"))
        err = ev[-1].error
        self.assertEqual((err.status, err.code), (429, "usage_limit_reached"))
        self.assertTrue(590 <= err.retry_after <= 600)

    def test_other_failures(self):
        cases = {"overloaded": 503, "model_not_found": 404, "billing_error": 429, "account_on_hold": 403, "unknown": 502}
        for api_error, status in cases.items():
            ev = feed(ClaudeParser(), {"type": "assistant", "error": api_error, "message": {}},
                      self.result(is_error=True, result="x"))
            self.assertEqual(ev[-1].error.status, status, api_error)

    def test_exit_without_result(self):
        ev = ClaudeParser().finish(1, "warning\nerror: unknown option '--x'\n")
        self.assertEqual(ev[0].error.status, 502)
        self.assertIn("unknown option", ev[0].error.message)
        self.assertEqual(ClaudeParser().finish(1, "Not logged in")[0].error.status, 401)

    def test_non_json_lines_are_skipped(self):
        p = ClaudeParser()
        self.assertEqual(p.feed(b"Warning: something"), [])
        self.assertEqual(p.non_json, 1)


class CodexEvents(unittest.TestCase):
    def test_answer_and_usage(self):
        ev = feed(CodexParser(), {"type": "thread.started"}, {"type": "turn.started"},
                  {"type": "item.completed", "item": {"type": "reasoning", "text": "..."}},
                  {"type": "item.completed", "item": {"type": "agent_message", "text": "One"}},
                  {"type": "item.completed", "item": {"type": "agent_message", "text": "Two"}},
                  {"type": "turn.completed", "usage": {"input_tokens": 9, "cached_input_tokens": 4,
                                                       "output_tokens": 3, "reasoning_output_tokens": 2}})
        self.assertEqual("".join(e.text for e in ev if isinstance(e, Text)), "One\n\nTwo")
        u = [e.usage for e in ev if isinstance(e, UsageEvent)][0]
        self.assertEqual((u.prompt_tokens, u.completion_tokens, u.cached_tokens, u.reasoning_tokens), (9, 3, 4, 2))
        self.assertEqual(ev[-1], Finish("stop"))

    def test_tool_items_stop_the_call(self):
        for kind in ("command_execution", "file_change", "mcp_tool_call", "web_search", "todo_list"):
            ev = feed(CodexParser(), {"type": "item.started", "item": {"type": kind}})
            self.assertEqual(ev[0].error.code, "isolation_violation", kind)

    def test_failures(self):
        cases = {
            "unexpected status 401 Unauthorized: Missing bearer": (401, "not_logged_in"),
            "You’ve hit your usage limit. Try again at 5:00 PM.": (429, "usage_limit_reached"),
            "Quota exceeded. Check your plan and billing details.": (429, "insufficient_quota"),
            "To use Codex with your ChatGPT plan, upgrade to Plus: https://chatgpt.com/explore/plus.": (403, "plan_not_included"),
            "rate limit exceeded: slow down": (429, "rate_limit_exceeded"),
            "Selected model is at capacity. Please try a different model.": (503, "overloaded"),
            "The 'x' model is not supported when using Codex with a ChatGPT account.": (404, "model_not_found"),
            "something else": (502, "cli_failed"),
        }
        for message, (status, code) in cases.items():
            ev = feed(CodexParser(), {"type": "error", "message": message}, {"type": "turn.failed", "error": {"message": message}})
            self.assertEqual((ev[-1].error.status, ev[-1].error.code), (status, code), message)

    def test_exit_without_result_uses_the_last_error(self):
        p = CodexParser()
        feed(p, {"type": "error", "message": "Reconnecting... 5/5 (unexpected status 401 Unauthorized)"})
        self.assertEqual(p.finish(1, "")[0].error.status, 401)
        self.assertEqual(CodexParser().finish(3, "")[0].error.status, 502)
        self.assertIsInstance(CodexParser().finish(1, "boom")[0], Fail)


if __name__ == "__main__":
    unittest.main()
