#!/usr/bin/env python3
"""Answers like `claude -p --output-format stream-json` or `codex exec --json`
would, without a login. Keywords in the prompt pick the behaviour; test/smoke.sh
uses them through cli-bridge:

  (none)        a short answer in several text deltas
  STUB:echo     the answer is JSON: prompt, system prompt, argv, environment, cwd
  STUB:auth     not logged in            STUB:limit   usage limit reached
  STUB:tool     a tool call appears      STUB:crash   bad option, exit 1
  STUB:big      a 300 KB answer          STUB:slow    answer after 3 s
  STUB:hang     starts a child process (sleep 987) and never answers
  STUB:write    leaves a file in the working directory, then answers
  STUB:real     runs the real CLI with the same arguments and prompt

Like the real CLIs it also writes into its config directory and TMPDIR, so
the smoke test can check that those are the bridge's own directories.
"""

import json
import os
import subprocess
import sys
import time

REAL = {
    "claude": "/opt/agentbox/node_modules/.bin/claude",
    "codex": "/opt/agentbox/node_modules/.bin/codex",
}
ANSWER = "Hello from the stub."


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def arg_after(args, flag):
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args):
            return args[i + 1]
    return None


def system_prompt(cli, args):
    path = None
    if cli == "claude":
        path = arg_after(args, "--system-prompt-file")
    else:
        for i, a in enumerate(args):
            if a == "-c" and i + 1 < len(args) and args[i + 1].startswith("model_instructions_file="):
                path = json.loads(args[i + 1].split("=", 1)[1])
    if not path:
        return None
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def leave_traces(cli):
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR" if cli == "claude" else "CODEX_HOME")
    if config_dir:
        with open(os.path.join(config_dir, ".stub-trace"), "a", encoding="utf-8") as fh:
            fh.write("x")
    tmp = os.environ.get("TMPDIR")
    if tmp:
        with open(os.path.join(tmp, f"stub-{os.getpid()}.tmp"), "w", encoding="utf-8") as fh:
            fh.write("scratch")


# --- claude stream-json ---------------------------------------------------------


def claude_answer(text, pieces=None):
    out({"type": "system", "subtype": "init", "tools": [], "mcp_servers": [], "model": "stub", "session_id": "stub"})
    for piece in pieces or [text[i:i + 6] for i in range(0, len(text), 6)]:
        out({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                               "delta": {"type": "text_delta", "text": piece}}})
        time.sleep(0.01)
    out({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
    out({"type": "result", "subtype": "success", "is_error": False, "result": text, "stop_reason": "end_turn",
         "usage": {"input_tokens": 7, "cache_read_input_tokens": 3, "cache_creation_input_tokens": 2,
                   "output_tokens": 5}, "total_cost_usd": 0.0})


def claude_error(message, api_error, resets_in=None):
    out({"type": "system", "subtype": "init", "tools": [], "mcp_servers": [], "model": "stub", "session_id": "stub"})
    if resets_in:
        out({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected",
                                                              "resetsAt": int(time.time()) + resets_in,
                                                              "rateLimitType": "five_hour"}})
    out({"type": "assistant", "message": {"content": [{"type": "text", "text": message}]}, "error": api_error})
    out({"type": "result", "subtype": "success", "is_error": True, "result": message, "usage": {}})


# --- codex exec --json -------------------------------------------------------------


def codex_answer(text):
    out({"type": "thread.started", "thread_id": "stub"})
    out({"type": "turn.started"})
    out({"type": "item.completed", "item": {"id": "item_0", "type": "reasoning", "text": "thinking"}})
    out({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": text}})
    out({"type": "turn.completed", "usage": {"input_tokens": 11, "cached_input_tokens": 3,
                                             "output_tokens": 4, "reasoning_output_tokens": 1}})


def codex_error(message):
    out({"type": "thread.started", "thread_id": "stub"})
    out({"type": "turn.started"})
    out({"type": "error", "message": message})
    out({"type": "turn.failed", "error": {"message": message}})


def main():
    cli, args = sys.argv[1], sys.argv[2:]
    prompt = sys.stdin.read()
    if "STUB:real" in prompt:
        sys.exit(subprocess.run([REAL[cli], *args], input=prompt.encode(), check=False).returncode)
    leave_traces(cli)
    answer = claude_answer if cli == "claude" else codex_answer

    if "STUB:write" in prompt:
        with open("stray.txt", "w", encoding="utf-8") as fh:
            fh.write("should not be here")
    if "STUB:hang" in prompt:
        subprocess.Popen(["sleep", "987"])  # noqa: S603,S607
        time.sleep(3600)
    if "STUB:slow" in prompt:
        time.sleep(3)
    if "STUB:crash" in prompt:
        print("error: unknown option '--frobnicate'", file=sys.stderr)
        sys.exit(1)
    if "STUB:auth" in prompt:
        if cli == "claude":
            claude_error("Not logged in · Please run /login", "authentication_failed")
        else:
            codex_error("unexpected status 401 Unauthorized: Missing bearer or basic authentication in header")
        sys.exit(1)
    if "STUB:limit" in prompt:
        if cli == "claude":
            claude_error("You've hit your limit · resets 5pm", "rate_limit", resets_in=120)
        else:
            codex_error("You’ve hit your usage limit. Try again at 5:00 PM.")
        sys.exit(1)
    if "STUB:tool" in prompt:
        if cli == "claude":
            out({"type": "system", "subtype": "init", "tools": ["Bash"], "mcp_servers": []})
        else:
            out({"type": "turn.started"})
            out({"type": "item.started", "item": {"id": "item_0", "type": "command_execution",
                                                  "command": "ls", "status": "in_progress"}})
        time.sleep(30)
        sys.exit(0)
    if "STUB:big" in prompt:
        text = "0123456789abcdef" * (300 * 64)  # 300 KB
        if cli == "claude":
            claude_answer(text, pieces=[text[i:i + 1024] for i in range(0, len(text), 1024)])
        else:
            answer(text)
        return
    if "STUB:echo" in prompt:
        names = ("HOME", "TMPDIR", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "CLAUDE_CODE_TMPDIR")
        answer(json.dumps({
            "prompt": prompt,
            "system": system_prompt(cli, args),
            "argv": args,
            "env": sorted(os.environ),
            "env_values": {k: os.environ[k] for k in names if k in os.environ},
            "cwd": os.getcwd(),
        }))
        return
    answer(ANSWER)


if __name__ == "__main__":
    main()
