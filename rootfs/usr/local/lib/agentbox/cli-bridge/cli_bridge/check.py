"""`agentbox cli-bridge check`: real calls through the running service.

For each model (default: each enabled adapter's default model) one
non-streaming and one streaming request with a tiny prompt, then the list
of every file written meanwhile under $HOME, /workspace and /tmp, with the
ones outside the bridge's own directories marked. This uses the real logins
and costs a little quota.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request

from . import adapters, config

PROMPT = "Reply with exactly one word: pong"


def _request(url: str, token: str, body=None, timeout: float = 600):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET")
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 - local URL


def _error_text(err: urllib.error.HTTPError) -> str:
    try:
        return json.loads(err.read())["error"]["message"]
    except Exception:  # noqa: BLE001
        return err.reason


def _complete(base, token, model):
    t0 = time.monotonic()
    body = {"model": model, "messages": [{"role": "user", "content": PROMPT}]}
    try:
        with _request(f"{base}/v1/chat/completions", token, body) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return f"{err.code} after {time.monotonic() - t0:.1f} s: {_error_text(err)}"
    usage = data.get("usage") or {}
    answer = (data["choices"][0]["message"]["content"] or "").strip().replace("\n", " ")
    return (f"200 in {time.monotonic() - t0:.1f} s, tokens in/out {usage.get('prompt_tokens')}/"
            f"{usage.get('completion_tokens')}, answer {answer[:60]!r}")


def _stream(base, token, model):
    t0 = time.monotonic()
    first = None
    text = []
    body = {"model": model, "stream": True, "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content": PROMPT}]}
    try:
        with _request(f"{base}/v1/chat/completions", token, body) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                event = json.loads(line[6:])
                if "error" in event:
                    return f"stream error after {time.monotonic() - t0:.1f} s: {event['error'].get('message')}"
                for choice in event.get("choices") or []:
                    piece = (choice.get("delta") or {}).get("content")
                    if piece:
                        first = first if first is not None else time.monotonic() - t0
                        text.append(piece)
    except urllib.error.HTTPError as err:
        return f"{err.code} after {time.monotonic() - t0:.1f} s: {_error_text(err)}"
    first_txt = f"first text after {first:.1f} s, " if first is not None else "no text, "
    return f"200, {first_txt}done after {time.monotonic() - t0:.1f} s, answer {''.join(text).strip()[:60]!r}"


def _written_since(marker: str, roots: list) -> list:
    roots = [r for r in roots if os.path.isdir(r)]
    if not roots:
        return []
    result = subprocess.run(
        ["find", *roots, "-xdev", "-newer", marker, "!", "-type", "d", "-print"],
        capture_output=True, text=True, check=False,
    )
    return sorted(p for p in result.stdout.splitlines() if p and p != marker)


def main(models: list) -> int:
    try:
        cfg = config.load(adapter_names=adapters.names())
    except config.ConfigError as err:
        print(f"configuration error: {err}")
        return 78
    if not cfg.enabled:
        print("cli-bridge is not enabled (AGENTBOX_CLI_BRIDGE=1)")
        return 1
    base = f"http://127.0.0.1:{cfg.port}"
    try:
        with _request(f"{base}/health", cfg.token, timeout=60) as resp:
            health = json.loads(resp.read())
    except (OSError, ValueError) as err:
        print(f"the service does not answer on {base}: {err} (agentbox status; docker logs agentbox)")
        return 1
    if not models:
        models = [name for name, a in (health.get("adapters") or {}).items() if a.get("enabled")]
    if not models:
        print("no adapter is enabled (AGENTBOX_CLI_BRIDGE_CLAUDE=1 / AGENTBOX_CLI_BRIDGE_CODEX=1)")
        return 1
    for name, a in (health.get("adapters") or {}).items():
        if a.get("enabled"):
            print(f"{name}: {a.get('cli')}, logged in: {a.get('logged_in')}")

    marker = os.path.join(cfg.runtime_dir, ".check-marker")
    with open(marker, "w", encoding="utf-8"):
        pass
    past = time.time() - 1
    os.utime(marker, (past, past))
    print(f"\nprompt: {PROMPT!r}")
    for model in models:
        print(f"{model}")
        print(f"  non-streaming: {_complete(base, cfg.token, model)}")
        print(f"  streaming:     {_stream(base, cfg.token, model)}")

    home = os.environ.get("HOME", "/home/agent")
    own = (cfg.state_dir.rstrip("/") + "/", cfg.runtime_dir.rstrip("/") + "/")
    written = _written_since(marker, [home, "/workspace", "/tmp"])
    print(f"\nfiles written or changed during the check ({len(written)}); '!' = outside the bridge's directories")
    print(f"  bridge directories: {cfg.state_dir}, {cfg.runtime_dir}")
    for path in written:
        flag = "  " if path.startswith(own) else "! "
        print(f"  {flag}{path}")
    os.unlink(marker)
    return 0
